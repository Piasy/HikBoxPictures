from __future__ import annotations

from pathlib import Path
from typing import Any

try:
    import sqlite3
except ModuleNotFoundError:
    import pysqlite3 as sqlite3  # type: ignore[no-redef]

from hikbox_pictures.exporter import build_delivery_destination_path, copy_with_metadata
from hikbox_pictures.metadata import format_year_month, resolve_capture_datetime
from hikbox_pictures.models import ExportBucket, ExportMatch, ExportRunResult
from hikbox_pictures.repositories import ExportRepo
from hikbox_pictures.services.export_match_service import ExportMatchService
from hikbox_pictures.services.observability_service import ObservabilityService


class ExportDeliveryService:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        self.export_repo = ExportRepo(conn)
        self.export_match_service = ExportMatchService(conn)
        self.observability = ObservabilityService(conn)

    def run_template(self, template_id: int) -> ExportRunResult:
        plan = self.export_match_service.build_template_plan(int(template_id))
        template = plan["template"]
        if not bool(template["enabled"]):
            self.observability.emit_event(
                level="warning",
                component="exporter",
                event_type="export.delivery.skipped",
                message=f"export template {template_id} 已禁用",
                run_kind="export",
                run_id=f"template-{int(template_id)}",
                detail={
                    "template_id": int(template_id),
                    "status": "disabled",
                },
            )
            raise ValueError(f"export template {template_id} 已禁用")

        spec_hash = str(plan["spec_hash"])
        matched_only_count = int(plan["matched_only_count"])
        matched_group_count = int(plan["matched_group_count"])
        include_group = bool(template["include_group"])
        export_live_mov = bool(template["export_live_mov"])
        output_root = Path(str(template["output_root"]))
        output_root.mkdir(parents=True, exist_ok=True)

        matches: list[ExportMatch] = [
            match
            for match in plan["matches"]
            if include_group or match.bucket is ExportBucket.ONLY
        ]

        exported_count = 0
        skipped_count = 0
        failed_count = 0
        expected_keys: set[tuple[int, str]] = set()
        run_id: int | None = None

        use_savepoint = bool(getattr(self.conn, "in_transaction", False))
        delivery_scope = "export_run_delivery_tx"
        self._begin_write_scope(use_savepoint=use_savepoint, scope_name=delivery_scope)
        try:
            run_id = self.export_repo.create_export_run(int(template["id"]), spec_hash, status="running")
            self.observability.emit_event(
                level="info",
                component="exporter",
                event_type="export.delivery.started",
                message="导出任务开始",
                run_kind="export",
                run_id=str(run_id),
                detail={
                    "template_id": int(template["id"]),
                    "matched_only_count": matched_only_count,
                    "matched_group_count": matched_group_count,
                    "status": "running",
                },
            )
            stale_other_spec_count = self.export_repo.mark_other_spec_deliveries_stale(
                template_id=int(template["id"]),
                spec_hash=spec_hash,
            )
            if stale_other_spec_count > 0:
                self.observability.emit_event(
                    level="info",
                    component="exporter",
                    event_type="export.delivery.stale_marked",
                    message="历史 spec 导出记录已标记为 stale",
                    run_kind="export",
                    run_id=str(run_id),
                    detail={
                        "template_id": int(template["id"]),
                        "status": "stale_marked",
                        "stale_count": int(stale_other_spec_count),
                    },
                )

            for match in matches:
                expected_keys.add((int(match.photo_asset_id), "primary"))
                primary_status = self._deliver_variant(
                    template_id=int(template["id"]),
                    spec_hash=spec_hash,
                    match=match,
                    run_id=run_id,
                    asset_variant="primary",
                    source_path=match.primary_path,
                    source_fingerprint=match.primary_fingerprint,
                    output_root=output_root,
                )
                if primary_status == "exported":
                    exported_count += 1
                elif primary_status == "skipped":
                    skipped_count += 1
                else:
                    failed_count += 1

                if export_live_mov and match.live_mov_path is not None:
                    expected_keys.add((int(match.photo_asset_id), "live_mov"))
                    live_status = self._deliver_variant(
                        template_id=int(template["id"]),
                        spec_hash=spec_hash,
                        match=match,
                        run_id=run_id,
                        asset_variant="live_mov",
                        source_path=match.live_mov_path,
                        source_fingerprint=match.live_mov_fingerprint,
                        output_root=output_root,
                    )
                    if live_status == "exported":
                        exported_count += 1
                    elif live_status == "skipped":
                        skipped_count += 1
                    else:
                        failed_count += 1

            self._mark_missing_same_spec_deliveries_stale(
                template_id=int(template["id"]),
                spec_hash=spec_hash,
                expected_keys=expected_keys,
                run_id=str(run_id),
            )
            self._commit_write_scope(use_savepoint=use_savepoint, scope_name=delivery_scope)
        except Exception as exc:
            self._rollback_write_scope(use_savepoint=use_savepoint, scope_name=delivery_scope)
            self.observability.emit_event(
                level="error",
                component="exporter",
                event_type="export.delivery.failed",
                message=str(exc),
                run_kind="export",
                run_id=str(run_id) if run_id is not None else None,
                detail={
                    "template_id": int(template["id"]),
                    "spec_hash": spec_hash,
                    "status": "failed",
                    "error_type": exc.__class__.__name__,
                    "error_message": str(exc),
                },
            )
            raise

        if run_id is None:
            raise RuntimeError("export run 未创建成功")

        run_status = "failed" if failed_count > 0 else "completed"
        finalize_scope = "export_run_finalize_tx"
        self._begin_write_scope(use_savepoint=use_savepoint, scope_name=finalize_scope)
        try:
            self.export_repo.finish_export_run(
                run_id,
                status=run_status,
                matched_only_count=matched_only_count,
                matched_group_count=matched_group_count,
                exported_count=exported_count,
                skipped_count=skipped_count,
                failed_count=failed_count,
            )
            self._commit_write_scope(use_savepoint=use_savepoint, scope_name=finalize_scope)
        except Exception as exc:
            self._rollback_write_scope(use_savepoint=use_savepoint, scope_name=finalize_scope)
            self.observability.emit_event(
                level="error",
                component="exporter",
                event_type="export.delivery.failed",
                message=str(exc),
                run_kind="export",
                run_id=str(run_id),
                detail={
                    "template_id": int(template["id"]),
                    "spec_hash": spec_hash,
                    "phase": "finalize",
                    "status": "failed",
                    "error_type": exc.__class__.__name__,
                    "error_message": str(exc),
                },
            )
            raise

        self.observability.emit_event(
            level="error" if run_status == "failed" else "info",
            component="exporter",
            event_type="export.delivery.failed" if run_status == "failed" else "export.delivery.completed",
            message="导出任务结束",
            run_kind="export",
            run_id=str(run_id),
            detail={
                "template_id": int(template["id"]),
                "exported_count": exported_count,
                "skipped_count": skipped_count,
                "failed_count": failed_count,
                "status": run_status,
            },
        )

        return ExportRunResult(
            template_id=int(template["id"]),
            run_id=int(run_id),
            spec_hash=spec_hash,
            matched_only_count=matched_only_count,
            matched_group_count=matched_group_count,
            exported_count=exported_count,
            skipped_count=skipped_count,
            failed_count=failed_count,
        )

    def _deliver_variant(
        self,
        *,
        template_id: int,
        spec_hash: str,
        match: ExportMatch,
        run_id: int,
        asset_variant: str,
        source_path: Path,
        source_fingerprint: str | None,
        output_root: Path,
    ) -> str:
        existing = self.export_repo.get_delivery(
            template_id=template_id,
            spec_hash=spec_hash,
            photo_asset_id=match.photo_asset_id,
            asset_variant=asset_variant,
        )
        target_path = self._resolve_target_path(
            existing=existing,
            match=match,
            source_path=source_path,
            output_root=output_root,
        )
        if self._can_skip_delivery(
            existing=existing,
            expected_target_path=target_path,
            expected_bucket=match.bucket.value,
            source_fingerprint=source_fingerprint,
        ):
            self.export_repo.upsert_delivery(
                template_id=template_id,
                spec_hash=spec_hash,
                photo_asset_id=match.photo_asset_id,
                asset_variant=asset_variant,
                bucket=match.bucket.value,
                target_path=str(target_path),
                source_fingerprint=source_fingerprint,
                status="skipped",
            )
            self.observability.emit_event(
                level="info",
                component="exporter",
                event_type="export.delivery.skipped",
                message="导出条目命中跳过条件",
                run_kind="export",
                run_id=str(run_id),
                detail={
                    "template_id": template_id,
                    "photo_asset_id": int(match.photo_asset_id),
                    "asset_variant": asset_variant,
                    "target_path": str(target_path),
                    "status": "skipped",
                    "phase": "delivery",
                },
            )
            return "skipped"

        status = "ok"
        try:
            copy_with_metadata(source_path, target_path)
        except Exception:
            status = "failed"

        self.export_repo.upsert_delivery(
            template_id=template_id,
            spec_hash=spec_hash,
            photo_asset_id=match.photo_asset_id,
            asset_variant=asset_variant,
            bucket=match.bucket.value,
            target_path=str(target_path),
            source_fingerprint=source_fingerprint,
            status=status,
        )
        if status == "failed":
            self.observability.emit_event(
                level="error",
                component="exporter",
                event_type="export.delivery.failed",
                message="导出条目写入失败",
                run_kind="export",
                run_id=str(run_id),
                detail={
                    "template_id": template_id,
                    "photo_asset_id": int(match.photo_asset_id),
                    "asset_variant": asset_variant,
                    "target_path": str(target_path),
                    "status": "failed",
                    "phase": "delivery",
                },
            )
        else:
            self.observability.emit_event(
                level="info",
                component="exporter",
                event_type="export.delivery.exported",
                message="导出条目已写入",
                run_kind="export",
                run_id=str(run_id),
                detail={
                    "template_id": template_id,
                    "photo_asset_id": int(match.photo_asset_id),
                    "asset_variant": asset_variant,
                    "target_path": str(target_path),
                    "status": "exported",
                    "phase": "delivery",
                },
            )
        return "exported" if status == "ok" else "failed"

    def _resolve_target_path(
        self,
        *,
        existing: dict[str, Any] | None,
        match: ExportMatch,
        source_path: Path,
        output_root: Path,
    ) -> Path:
        year_month = self._resolve_year_month(match=match, source_path=source_path)
        expected_dir = output_root / match.bucket.value / year_month
        if existing is not None and existing.get("bucket") == match.bucket.value and existing.get("target_path"):
            existing_target_path = Path(str(existing["target_path"]))
            if existing_target_path.parent == expected_dir:
                return existing_target_path
        return build_delivery_destination_path(
            source_path,
            output_root=output_root,
            bucket=match.bucket.value,
            year_month=year_month,
        )

    def _begin_write_scope(self, *, use_savepoint: bool, scope_name: str) -> None:
        if use_savepoint:
            self.conn.execute(f"SAVEPOINT {scope_name}")
        else:
            self.conn.execute("BEGIN IMMEDIATE")

    def _commit_write_scope(self, *, use_savepoint: bool, scope_name: str) -> None:
        if use_savepoint:
            self.conn.execute(f"RELEASE SAVEPOINT {scope_name}")
        else:
            self.conn.commit()

    def _rollback_write_scope(self, *, use_savepoint: bool, scope_name: str) -> None:
        if use_savepoint:
            self.conn.execute(f"ROLLBACK TO SAVEPOINT {scope_name}")
            self.conn.execute(f"RELEASE SAVEPOINT {scope_name}")
        else:
            self.conn.rollback()

    def _can_skip_delivery(
        self,
        *,
        existing: dict[str, Any] | None,
        expected_target_path: Path,
        expected_bucket: str,
        source_fingerprint: str | None,
    ) -> bool:
        if existing is None:
            return False
        if existing.get("status") not in {"ok", "skipped"}:
            return False
        if existing.get("bucket") != expected_bucket:
            return False
        existing_target_path = Path(str(existing["target_path"]))
        if existing_target_path != expected_target_path:
            return False
        if not expected_target_path.exists():
            return False
        existing_fingerprint = existing.get("source_fingerprint")
        if not existing_fingerprint or not source_fingerprint:
            return False
        return str(existing_fingerprint) == str(source_fingerprint)

    def _mark_missing_same_spec_deliveries_stale(
        self,
        *,
        template_id: int,
        spec_hash: str,
        expected_keys: set[tuple[int, str]],
        run_id: str,
    ) -> int:
        stale_count = 0
        rows = self.export_repo.list_deliveries_for_spec(
            template_id=int(template_id),
            spec_hash=spec_hash,
        )
        for row in rows:
            key = (int(row["photo_asset_id"]), str(row["asset_variant"]))
            if key in expected_keys:
                continue
            if str(row["status"]) == "stale":
                continue
            stale_count += self.export_repo.mark_delivery_status(
                delivery_id=int(row["id"]),
                status="stale",
            )
            self.observability.emit_event(
                level="info",
                component="exporter",
                event_type="export.delivery.stale_marked",
                message="导出条目已标记为 stale",
                run_kind="export",
                run_id=run_id,
                detail={
                    "template_id": int(template_id),
                    "photo_asset_id": int(row["photo_asset_id"]),
                    "asset_variant": str(row["asset_variant"]),
                    "status": "stale_marked",
                },
            )
        return stale_count

    def _resolve_year_month(self, *, match: ExportMatch, source_path: Path) -> str:
        if match.capture_month:
            return str(match.capture_month)
        try:
            capture_datetime = resolve_capture_datetime(source_path)
            return format_year_month(capture_datetime)
        except Exception:
            return "unknown"
