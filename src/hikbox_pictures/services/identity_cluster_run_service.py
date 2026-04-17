from __future__ import annotations

from typing import Any

try:
    import sqlite3
except ModuleNotFoundError:
    import pysqlite3 as sqlite3  # type: ignore[no-redef]

from hikbox_pictures.repositories.identity_cluster_run_repo import IdentityClusterRunRepo


class IdentityClusterRunService:
    _TERMINAL_STATUSES = {"succeeded", "failed", "cancelled"}

    def __init__(self, conn: sqlite3.Connection, *, cluster_run_repo: IdentityClusterRunRepo) -> None:
        self.conn = conn
        self.cluster_run_repo = cluster_run_repo

    def create_run(
        self,
        *,
        observation_snapshot_id: int,
        cluster_profile_id: int,
        algorithm_version: str,
        supersedes_run_id: int | None,
    ) -> dict[str, int]:
        managed_transaction = not self.conn.in_transaction
        if managed_transaction:
            self.conn.execute("BEGIN IMMEDIATE")
        try:
            run_id = self.cluster_run_repo.insert_run(
                observation_snapshot_id=observation_snapshot_id,
                cluster_profile_id=cluster_profile_id,
                algorithm_version=algorithm_version,
                run_status="created",
                supersedes_run_id=supersedes_run_id,
            )
            if managed_transaction:
                self.conn.commit()
            return {"run_id": int(run_id)}
        except Exception:
            if managed_transaction and self.conn.in_transaction:
                self.conn.rollback()
            raise

    def mark_run_running(self, *, run_id: int) -> None:
        managed_transaction = not self.conn.in_transaction
        if managed_transaction:
            self.conn.execute("BEGIN IMMEDIATE")
        try:
            run = self.cluster_run_repo.get_run_required(run_id)
            current = str(run["run_status"])
            self._assert_transition_allowed(current=current, target="running")
            if current != "running":
                updated = self.cluster_run_repo.update_run_status(
                    run_id=run_id,
                    run_status="running",
                    summary_json=self._load_json(run.get("summary_json")),
                    failure_json=self._load_json(run.get("failure_json")),
                    expected_statuses=(current,),
                )
                if not updated:
                    raise ValueError(f"run_status 并发冲突，无法转换为 running: {int(run_id)}")
            if managed_transaction:
                self.conn.commit()
        except Exception:
            if managed_transaction and self.conn.in_transaction:
                self.conn.rollback()
            raise

    def mark_run_succeeded(
        self,
        *,
        run_id: int,
        summary_json: dict[str, Any],
        select_as_review_target: bool,
    ) -> None:
        managed_transaction = not self.conn.in_transaction
        if managed_transaction:
            self.conn.execute("BEGIN IMMEDIATE")
        try:
            run = self.cluster_run_repo.get_run_required(run_id)
            current = str(run["run_status"])
            self._assert_transition_allowed(current=current, target="succeeded")
            if current != "succeeded":
                updated = self.cluster_run_repo.update_run_status(
                    run_id=run_id,
                    run_status="succeeded",
                    summary_json=summary_json,
                    failure_json={},
                    expected_statuses=(current,),
                )
                if not updated:
                    raise ValueError(f"run_status 并发冲突，无法转换为 succeeded: {int(run_id)}")
            run_row = self.cluster_run_repo.get_run_required(run_id)
            has_existing_target = self.cluster_run_repo.exists_review_target()
            should_select = bool(select_as_review_target) or (not has_existing_target)
            if should_select:
                self.cluster_run_repo.clear_review_target()
                review_selected_at = str(run_row["finished_at"]) if run_row["finished_at"] is not None else None
                self.cluster_run_repo.set_review_target(
                    run_id=run_id,
                    review_selected_at=review_selected_at,
                )
            if managed_transaction:
                self.conn.commit()
        except Exception:
            if managed_transaction and self.conn.in_transaction:
                self.conn.rollback()
            raise

    def mark_run_failed(self, *, run_id: int, reason: str) -> None:
        managed_transaction = not self.conn.in_transaction
        if managed_transaction:
            self.conn.execute("BEGIN IMMEDIATE")
        try:
            run = self.cluster_run_repo.get_run_required(run_id)
            current = str(run["run_status"])
            self._assert_transition_allowed(current=current, target="failed")
            if current != "failed":
                updated = self.cluster_run_repo.update_run_status(
                    run_id=run_id,
                    run_status="failed",
                    summary_json={},
                    failure_json={"reason": str(reason)},
                    expected_statuses=(current,),
                )
                if not updated:
                    raise ValueError(f"run_status 并发冲突，无法转换为 failed: {int(run_id)}")
            if managed_transaction:
                self.conn.commit()
        except Exception:
            if managed_transaction and self.conn.in_transaction:
                self.conn.rollback()
            raise

    def mark_run_cancelled(self, *, run_id: int, reason: str) -> None:
        managed_transaction = not self.conn.in_transaction
        if managed_transaction:
            self.conn.execute("BEGIN IMMEDIATE")
        try:
            run = self.cluster_run_repo.get_run_required(run_id)
            current = str(run["run_status"])
            self._assert_transition_allowed(current=current, target="cancelled")
            if current != "cancelled":
                updated = self.cluster_run_repo.update_run_status(
                    run_id=run_id,
                    run_status="cancelled",
                    summary_json={},
                    failure_json={"reason": str(reason)},
                    expected_statuses=(current,),
                )
                if not updated:
                    raise ValueError(f"run_status 并发冲突，无法转换为 cancelled: {int(run_id)}")
            if managed_transaction:
                self.conn.commit()
        except Exception:
            if managed_transaction and self.conn.in_transaction:
                self.conn.rollback()
            raise

    def select_review_target(self, *, run_id: int) -> None:
        managed_transaction = not self.conn.in_transaction
        if managed_transaction:
            self.conn.execute("BEGIN IMMEDIATE")
        try:
            target = self.cluster_run_repo.get_run_required(run_id)
            if str(target["run_status"]) != "succeeded":
                raise ValueError(f"只能选择 succeeded run 作为 review target: {int(run_id)}")
            self.cluster_run_repo.clear_review_target()
            self.cluster_run_repo.set_review_target(
                run_id=run_id,
                review_selected_at=None,
            )
            if managed_transaction:
                self.conn.commit()
        except Exception:
            if managed_transaction and self.conn.in_transaction:
                self.conn.rollback()
            raise

    def _assert_transition_allowed(self, *, current: str, target: str) -> None:
        if current == target:
            return
        if current in self._TERMINAL_STATUSES:
            raise ValueError(f"run_status 终态不允许转换: {current} -> {target}")
        allowed = {
            "created": {"running", "succeeded", "failed", "cancelled"},
            "running": {"succeeded", "failed", "cancelled"},
        }
        if target not in allowed.get(current, set()):
            raise ValueError(f"非法 run_status 转换: {current} -> {target}")

    def _load_json(self, value: Any) -> dict[str, Any]:
        if isinstance(value, dict):
            return value
        if not isinstance(value, str) or value.strip() == "":
            return {}
        try:
            import json

            payload = json.loads(value)
            return payload if isinstance(payload, dict) else {}
        except Exception:
            return {}
