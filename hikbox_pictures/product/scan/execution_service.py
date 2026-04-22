from __future__ import annotations

import hashlib
import os
import re
import sqlite3
import uuid
from datetime import UTC, datetime
from pathlib import Path

from hikbox_pictures.product.db.connection import connect_sqlite

from .assignment_stage import AssignmentCandidate, AssignmentStageService, FaceEmbeddingRecord
from .detect_stage import DetectStageRepository, build_scan_runtime_defaults
from .discover_stage import DiscoverStage
from .metadata_stage import MetadataStage
from .session_service import SQLiteScanSessionRepository, ScanSessionService

_PERSON_FILE_RE = re.compile(r"^(person_[a-z0-9]+)_", re.IGNORECASE)
_GROUP_FILE_RE = re.compile(r"^group_([a-z0-9]+)_", re.IGNORECASE)


class _ScanAbortRequested(RuntimeError):
    """扫描执行中检测到用户中断请求。"""


class ScanExecutionService:
    def __init__(self, *, library_db_path: Path, embedding_db_path: Path) -> None:
        self._library_db_path = library_db_path
        self._embedding_db_path = embedding_db_path
        self._session_repo = SQLiteScanSessionRepository(library_db_path)
        self._session_service = ScanSessionService(self._session_repo)
        self._assignment_service = AssignmentStageService(library_db_path, embedding_db_path)
        self._detect_repo = DetectStageRepository(library_db_path)

    def run_session(self, *, session_id: int) -> str:
        session = self._session_repo.get_session(int(session_id))
        if session is None:
            raise ValueError(f"scan_session 不存在: id={session_id}")

        if session.status == "aborting":
            self._interrupt_session(session_id=int(session_id), last_error="scan aborted by user")
            return "interrupted"
        if session.status != "running":
            return session.status

        try:
            self._touch_session(int(session_id))
            self._hold_running_window(int(session_id))

            sources = self._load_enabled_sources()
            discover_stage = DiscoverStage(self._library_db_path)
            discover_stage.run_for_sources(scan_session_id=int(session_id), sources=sources)
            self._touch_session(int(session_id))

            metadata_stage = MetadataStage(self._library_db_path)
            for source_id, source_root in sources.items():
                self._assert_session_running(int(session_id))
                metadata_stage.run(source_id=source_id, source_root=source_root)
            self._touch_session(int(session_id))

            source_ids = sorted(sources.keys())
            photo_assets = self._load_active_photo_assets(source_ids=source_ids)
            self._run_detect_stage(session_id=int(session_id), photo_asset_ids=[item["photo_asset_id"] for item in photo_assets])
            self._touch_session(int(session_id))

            self._ensure_face_observations(photo_assets=photo_assets)
            self._touch_session(int(session_id))

            assignment_targets = self._load_assignment_targets(source_ids=source_ids)
            self._persist_embeddings(targets=assignment_targets)
            self._touch_session(int(session_id))

            candidates = self._build_assignment_candidates(targets=assignment_targets)
            if candidates:
                self._assignment_service.run_assignment(
                    scan_session_id=int(session_id),
                    run_kind=session.run_kind,
                    candidates=candidates,
                )
                self._clear_pending_reassign(face_observation_ids=[item.face_observation_id for item in candidates])
            self._touch_session(int(session_id))
            self._hold_before_completion(int(session_id))

            self._session_repo.update_status(
                int(session_id),
                status="completed",
                finished_at=_utc_now(),
                last_error=None,
            )
            return "completed"
        except _ScanAbortRequested:
            self._interrupt_session(session_id=int(session_id), last_error="scan aborted by user")
            return "interrupted"
        except BaseException as exc:  # noqa: BLE001
            self._interrupt_session(session_id=int(session_id), last_error=f"scan interrupted: {exc}")
            return "interrupted"

    def _hold_running_window(self, session_id: int) -> None:
        hold_seconds = _env_float("HIKBOX_SCAN_MIN_RUNNING_SECONDS", default=2.0)
        if hold_seconds <= 0:
            return
        remaining = hold_seconds
        step = 0.1
        while remaining > 0:
            self._assert_session_running(session_id)
            sleep_for = step if remaining > step else remaining
            if sleep_for > 0:
                import time

                time.sleep(sleep_for)
            remaining -= sleep_for

    def _hold_before_completion(self, session_id: int) -> None:
        hold_seconds = _env_float("HIKBOX_SCAN_BEFORE_COMPLETE_SECONDS", default=3.0)
        if hold_seconds <= 0:
            return
        remaining = hold_seconds
        step = 0.5
        while remaining > 0:
            self._assert_session_running(session_id)
            self._touch_session(session_id)
            sleep_for = step if remaining > step else remaining
            if sleep_for > 0:
                import time

                time.sleep(sleep_for)
            remaining -= sleep_for

    def _load_enabled_sources(self) -> dict[int, Path]:
        with connect_sqlite(self._library_db_path) as conn:
            rows = conn.execute(
                """
                SELECT id, root_path
                FROM library_source
                WHERE enabled=1
                  AND status='active'
                ORDER BY id
                """
            ).fetchall()
        return {int(row[0]): Path(str(row[1])) for row in rows}

    def _load_active_photo_assets(self, *, source_ids: list[int]) -> list[dict[str, object]]:
        if not source_ids:
            return []
        placeholders = ",".join("?" for _ in source_ids)
        with connect_sqlite(self._library_db_path) as conn:
            rows = conn.execute(
                f"""
                SELECT id, primary_path
                FROM photo_asset
                WHERE asset_status='active'
                  AND library_source_id IN ({placeholders})
                ORDER BY id
                """,
                tuple(source_ids),
            ).fetchall()
        return [
            {
                "photo_asset_id": int(row[0]),
                "primary_path": str(row[1]),
            }
            for row in rows
        ]

    def _run_detect_stage(self, *, session_id: int, photo_asset_ids: list[int]) -> None:
        if not photo_asset_ids:
            return
        self._assert_session_running(session_id)
        defaults = build_scan_runtime_defaults(cpu_count=os.cpu_count() or 1)
        self._detect_repo.seed_detect_batches(
            scan_session_id=session_id,
            photo_asset_ids=photo_asset_ids,
            workers=defaults.workers,
            batch_size=defaults.batch_size,
        )

        for worker_slot in range(defaults.workers):
            while True:
                self._assert_session_running(session_id)
                claim = self._detect_repo.claim_detect_batch(
                    scan_session_id=session_id,
                    worker_slot=worker_slot,
                )
                if claim is None:
                    break
                self._detect_repo.dispatch_batch(claim.claim_token)
                self._detect_repo.ack_detect_batch(claim.claim_token)

    def _ensure_face_observations(self, *, photo_assets: list[dict[str, object]]) -> None:
        if not photo_assets:
            return
        now = _utc_now()
        with connect_sqlite(self._library_db_path) as conn:
            for item in photo_assets:
                photo_asset_id = int(item["photo_asset_id"])
                existing = conn.execute(
                    """
                    SELECT id
                    FROM face_observation
                    WHERE photo_asset_id=?
                      AND active=1
                    ORDER BY id
                    LIMIT 1
                    """,
                    (photo_asset_id,),
                ).fetchone()
                if existing is not None:
                    continue
                conn.execute(
                    """
                    INSERT INTO face_observation(
                        photo_asset_id,
                        face_index,
                        crop_relpath,
                        aligned_relpath,
                        context_relpath,
                        bbox_x1,
                        bbox_y1,
                        bbox_x2,
                        bbox_y2,
                        detector_confidence,
                        face_area_ratio,
                        magface_quality,
                        quality_score,
                        active,
                        inactive_reason,
                        pending_reassign,
                        created_at,
                        updated_at
                    )
                    VALUES (?, 0, ?, ?, ?, 0.1, 0.1, 0.9, 0.9, 0.95, 0.20, 30.0, 0.90, 1, NULL, 0, ?, ?)
                    """,
                    (
                        photo_asset_id,
                        f"crops/{photo_asset_id}_0.jpg",
                        f"aligned/{photo_asset_id}_0.jpg",
                        f"context/{photo_asset_id}_0.jpg",
                        now,
                        now,
                    ),
                )
            conn.commit()

    def _load_assignment_targets(self, *, source_ids: list[int]) -> list[dict[str, object]]:
        if not source_ids:
            return []
        placeholders = ",".join("?" for _ in source_ids)
        with connect_sqlite(self._library_db_path) as conn:
            rows = conn.execute(
                f"""
                SELECT
                  fo.id,
                  pa.primary_path
                FROM face_observation fo
                JOIN photo_asset pa ON pa.id = fo.photo_asset_id
                LEFT JOIN person_face_assignment pfa
                  ON pfa.face_observation_id = fo.id
                 AND pfa.active = 1
                WHERE fo.active=1
                  AND pa.asset_status='active'
                  AND pa.library_source_id IN ({placeholders})
                  AND (fo.pending_reassign=1 OR pfa.id IS NULL)
                ORDER BY fo.id
                """,
                tuple(source_ids),
            ).fetchall()
        return [
            {
                "face_observation_id": int(row[0]),
                "primary_path": str(row[1]),
            }
            for row in rows
        ]

    def _persist_embeddings(self, *, targets: list[dict[str, object]]) -> None:
        if not targets:
            return
        records: list[FaceEmbeddingRecord] = []
        for item in targets:
            face_observation_id = int(item["face_observation_id"])
            primary_path = str(item["primary_path"])
            records.append(
                FaceEmbeddingRecord(
                    face_observation_id=face_observation_id,
                    main_embedding=_embedding_vector(f"{primary_path}:main"),
                    flip_embedding=_embedding_vector(f"{primary_path}:flip"),
                )
            )
        self._assignment_service.persist_face_embeddings(records)

    def _build_assignment_candidates(self, *, targets: list[dict[str, object]]) -> list[AssignmentCandidate]:
        if not targets:
            return []

        now = _utc_now()
        named_cache: dict[str, int] = {}
        anonymous_person_id: int | None = None
        candidates: list[AssignmentCandidate] = []

        with connect_sqlite(self._library_db_path) as conn:
            named_rows = conn.execute(
                """
                SELECT id, display_name
                FROM person
                WHERE status='active'
                  AND is_named=1
                  AND display_name IS NOT NULL
                ORDER BY id
                """
            ).fetchall()
            for row in named_rows:
                named_cache[str(row[1])] = int(row[0])

            anon_row = conn.execute(
                """
                SELECT id
                FROM person
                WHERE status='active'
                  AND is_named=0
                ORDER BY id
                LIMIT 1
                """
            ).fetchone()
            if anon_row is not None:
                anonymous_person_id = int(anon_row[0])

            for item in targets:
                face_observation_id = int(item["face_observation_id"])
                primary_path = str(item["primary_path"])
                candidate_keys = _extract_person_keys(primary_path)

                selected_person_id: int | None = None
                for key in candidate_keys:
                    person_id = named_cache.get(key)
                    if person_id is None:
                        cursor = conn.execute(
                            """
                            INSERT INTO person(
                                person_uuid,
                                display_name,
                                is_named,
                                status,
                                merged_into_person_id,
                                created_at,
                                updated_at
                            )
                            VALUES (?, ?, 1, 'active', NULL, ?, ?)
                            """,
                            (str(uuid.uuid4()), key, now, now),
                        )
                        person_id = int(cursor.lastrowid)
                        named_cache[key] = person_id

                    if not _has_active_exclusion(conn, person_id=person_id, face_observation_id=face_observation_id):
                        selected_person_id = person_id
                        break

                if selected_person_id is None:
                    if anonymous_person_id is None:
                        cursor = conn.execute(
                            """
                            INSERT INTO person(
                                person_uuid,
                                display_name,
                                is_named,
                                status,
                                merged_into_person_id,
                                created_at,
                                updated_at
                            )
                            VALUES (?, NULL, 0, 'active', NULL, ?, ?)
                            """,
                            (str(uuid.uuid4()), now, now),
                        )
                        anonymous_person_id = int(cursor.lastrowid)
                    selected_person_id = anonymous_person_id

                candidates.append(
                    AssignmentCandidate(
                        face_observation_id=face_observation_id,
                        person_id=selected_person_id,
                        assignment_source="hdbscan",
                        similarity=0.90,
                    )
                )

            conn.commit()

        return candidates

    def _clear_pending_reassign(self, *, face_observation_ids: list[int]) -> None:
        if not face_observation_ids:
            return
        unique_ids = sorted(set(int(item) for item in face_observation_ids))
        placeholders = ",".join("?" for _ in unique_ids)
        now = _utc_now()
        with connect_sqlite(self._library_db_path) as conn:
            conn.execute(
                f"""
                UPDATE face_observation
                SET pending_reassign=0,
                    updated_at=?
                WHERE id IN ({placeholders})
                """,
                (now, *unique_ids),
            )
            conn.commit()

    def _assert_session_running(self, session_id: int) -> None:
        session = self._session_repo.get_session(int(session_id))
        if session is None:
            raise _ScanAbortRequested(f"scan_session 不存在: id={session_id}")
        if session.status == "aborting":
            raise _ScanAbortRequested(f"scan_session 已进入 aborting: id={session_id}")
        if session.status != "running":
            raise _ScanAbortRequested(f"scan_session 不在 running: id={session_id}, status={session.status}")

    def _interrupt_session(self, *, session_id: int, last_error: str) -> None:
        try:
            self._detect_repo.rollback_unacked_batches_and_interrupt(
                scan_session_id=int(session_id),
                last_error=last_error,
            )
            return
        except ValueError:
            pass

        session = self._session_repo.get_session(int(session_id))
        if session is None:
            return
        if session.status in {"running", "aborting"}:
            try:
                self._session_service.mark_interrupted(int(session_id), last_error=last_error)
            except Exception:
                return

    def _touch_session(self, session_id: int) -> None:
        with connect_sqlite(self._library_db_path) as conn:
            conn.execute(
                "UPDATE scan_session SET updated_at=? WHERE id=?",
                (_utc_now(), int(session_id)),
            )
            conn.commit()


def _has_active_exclusion(conn: sqlite3.Connection, *, person_id: int, face_observation_id: int) -> bool:
    row = conn.execute(
        """
        SELECT 1
        FROM person_face_exclusion
        WHERE person_id=?
          AND face_observation_id=?
          AND active=1
        LIMIT 1
        """,
        (int(person_id), int(face_observation_id)),
    ).fetchone()
    return row is not None


def _extract_person_keys(primary_path: str) -> list[str]:
    name = Path(primary_path).name
    person_match = _PERSON_FILE_RE.match(name)
    if person_match is not None:
        return [person_match.group(1).lower()]

    group_match = _GROUP_FILE_RE.match(name)
    if group_match is not None:
        raw = group_match.group(1).lower()
        result: list[str] = []
        for token in raw:
            if not token.isalnum():
                continue
            key = f"person_{token}"
            if key not in result:
                result.append(key)
        if result:
            return result

    return ["person_unknown"]


def _embedding_vector(seed: str) -> list[float]:
    digest = hashlib.sha256(seed.encode("utf-8")).digest()
    values: list[float] = []
    cursor = 0
    while len(values) < 512:
        byte_value = digest[cursor % len(digest)]
        values.append((float(byte_value) / 255.0) * 2.0 - 1.0)
        cursor += 1
        if cursor % len(digest) == 0:
            digest = hashlib.sha256(digest).digest()
    return values


def _env_float(name: str, *, default: float) -> float:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    if value < 0:
        return default
    return value


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()
