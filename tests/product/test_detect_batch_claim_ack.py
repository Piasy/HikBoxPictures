from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from hikbox_pictures.product.db.schema_bootstrap import bootstrap_library_schema
from hikbox_pictures.product.scan.detect_stage import (
    DetectStageRepository,
    build_scan_runtime_defaults,
    rollback_unacked_batches_and_interrupt,
    split_batch,
)


def _insert_scan_session(db_path: Path, *, status: str = "running") -> int:
    with sqlite3.connect(db_path) as conn:
        cursor = conn.execute(
            """
            INSERT INTO scan_session(
                run_kind,
                status,
                triggered_by,
                resume_from_session_id,
                started_at,
                finished_at,
                last_error,
                created_at,
                updated_at
            )
            VALUES ('scan_full', ?, 'manual_cli', NULL, '2026-04-22T00:00:00+00:00', NULL, NULL, '2026-04-22T00:00:00+00:00', '2026-04-22T00:00:00+00:00')
            """,
            (status,),
        )
        conn.commit()
        return int(cursor.lastrowid)


def _insert_photo_assets(db_path: Path, *, source_id: int, count: int) -> list[int]:
    created: list[int] = []
    with sqlite3.connect(db_path) as conn:
        for index in range(count):
            cursor = conn.execute(
                """
                INSERT INTO photo_asset(
                  library_source_id,
                  primary_path,
                  primary_fingerprint,
                  fingerprint_algo,
                  file_size,
                  mtime_ns,
                  capture_datetime,
                  capture_month,
                  is_live_photo,
                  live_mov_path,
                  live_mov_size,
                  live_mov_mtime_ns,
                  asset_status,
                  created_at,
                  updated_at
                )
                VALUES (?, ?, ?, 'sha256', 123, 456, NULL, NULL, 0, NULL, NULL, NULL, 'active', '2026-04-22T00:00:00+00:00', '2026-04-22T00:00:00+00:00')
                """,
                (source_id, f"IMG_{index:04d}.HEIC", f"fp-{index}"),
            )
            created.append(int(cursor.lastrowid))
        conn.commit()
    return created


def _first_batch_token(db_path: Path, *, session_id: int) -> str:
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT claim_token FROM scan_batch WHERE scan_session_id=? ORDER BY id LIMIT 1",
            (session_id,),
        ).fetchone()
    assert row is not None
    return str(row[0])


def test_default_scan_runtime_values() -> None:
    defaults = build_scan_runtime_defaults(cpu_count=8)
    assert defaults.det_size == 640
    assert defaults.batch_size == 300
    assert defaults.workers == 4
    assert build_scan_runtime_defaults(cpu_count=1).workers == 1


def test_split_batch_evenly() -> None:
    assert split_batch(total=300, workers=3) == [100, 100, 100]
    assert split_batch(total=302, workers=3) == [101, 101, 100]


def test_claim_dispatch_ack_advances_batch_and_item_status(tmp_path: Path) -> None:
    db_path = tmp_path / "library.db"
    bootstrap_library_schema(db_path)
    session_id = _insert_scan_session(db_path)
    photo_asset_ids = _insert_photo_assets(db_path, source_id=1, count=4)

    repo = DetectStageRepository(db_path)
    created_batch_ids = repo.seed_detect_batches(
        scan_session_id=session_id,
        photo_asset_ids=photo_asset_ids,
        workers=2,
        batch_size=2,
    )

    assert len(created_batch_ids) == 2

    claim = repo.claim_detect_batch(scan_session_id=session_id, worker_slot=0)
    assert claim is not None

    repo.dispatch_batch(claim.claim_token)
    repo.ack_detect_batch(claim.claim_token)

    with sqlite3.connect(db_path) as conn:
        batch_row = conn.execute(
            "SELECT status, started_at, acked_at FROM scan_batch WHERE id=?",
            (claim.batch_id,),
        ).fetchone()
        item_statuses = conn.execute(
            "SELECT status FROM scan_batch_item WHERE scan_batch_id=? ORDER BY item_order",
            (claim.batch_id,),
        ).fetchall()

    assert batch_row is not None
    assert batch_row[0] == "acked"
    assert batch_row[1] is not None
    assert batch_row[2] is not None
    assert [row[0] for row in item_statuses] == ["done", "done"]


def test_claim_is_not_repeatable_for_same_batch(tmp_path: Path) -> None:
    db_path = tmp_path / "library.db"
    bootstrap_library_schema(db_path)
    session_id = _insert_scan_session(db_path)
    photo_asset_ids = _insert_photo_assets(db_path, source_id=1, count=2)

    repo = DetectStageRepository(db_path)
    repo.seed_detect_batches(
        scan_session_id=session_id,
        photo_asset_ids=photo_asset_ids,
        workers=1,
        batch_size=2,
    )

    first = repo.claim_detect_batch(scan_session_id=session_id, worker_slot=0)
    second = repo.claim_detect_batch(scan_session_id=session_id, worker_slot=0)
    assert first is not None
    assert second is None


def test_ack_before_dispatch_raises(tmp_path: Path) -> None:
    db_path = tmp_path / "library.db"
    bootstrap_library_schema(db_path)
    session_id = _insert_scan_session(db_path)
    photo_asset_ids = _insert_photo_assets(db_path, source_id=1, count=1)
    repo = DetectStageRepository(db_path)
    repo.seed_detect_batches(
        scan_session_id=session_id,
        photo_asset_ids=photo_asset_ids,
        workers=1,
        batch_size=1,
    )
    token = _first_batch_token(db_path, session_id=session_id)

    with pytest.raises(ValueError, match="running"):
        repo.ack_detect_batch(token)


def test_dispatch_after_acked_raises(tmp_path: Path) -> None:
    db_path = tmp_path / "library.db"
    bootstrap_library_schema(db_path)
    session_id = _insert_scan_session(db_path)
    photo_asset_ids = _insert_photo_assets(db_path, source_id=1, count=1)
    repo = DetectStageRepository(db_path)
    repo.seed_detect_batches(
        scan_session_id=session_id,
        photo_asset_ids=photo_asset_ids,
        workers=1,
        batch_size=1,
    )
    claim = repo.claim_detect_batch(scan_session_id=session_id, worker_slot=0)
    assert claim is not None
    repo.dispatch_batch(claim.claim_token)
    repo.ack_detect_batch(claim.claim_token)

    with pytest.raises(ValueError, match="不能 dispatch"):
        repo.dispatch_batch(claim.claim_token)


def test_ack_payload_missing_item_results_raises_and_keeps_running(tmp_path: Path) -> None:
    db_path = tmp_path / "library.db"
    bootstrap_library_schema(db_path)
    session_id = _insert_scan_session(db_path)
    photo_asset_ids = _insert_photo_assets(db_path, source_id=1, count=2)
    repo = DetectStageRepository(db_path)
    repo.seed_detect_batches(
        scan_session_id=session_id,
        photo_asset_ids=photo_asset_ids,
        workers=1,
        batch_size=2,
    )
    claim = repo.claim_detect_batch(scan_session_id=session_id, worker_slot=0)
    assert claim is not None
    repo.dispatch_batch(claim.claim_token)

    payload_path = tmp_path / "worker-payload.json"
    payload_path.write_text(
        json.dumps(
            {
                "batch_id": claim.batch_id,
                "claim_token": claim.claim_token,
                "results": [
                    {
                        "scan_batch_item_id": claim.items[0]["scan_batch_item_id"],
                        "photo_asset_id": claim.items[0]["photo_asset_id"],
                        "status": "done",
                        "error_message": None,
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="未覆盖全部 batch item"):
        repo.ack_detect_batch(claim.claim_token, worker_payload_path=payload_path)

    with sqlite3.connect(db_path) as conn:
        batch_row = conn.execute(
            "SELECT status, acked_at FROM scan_batch WHERE id=?",
            (claim.batch_id,),
        ).fetchone()
        item_rows = conn.execute(
            "SELECT status FROM scan_batch_item WHERE scan_batch_id=? ORDER BY item_order",
            (claim.batch_id,),
        ).fetchall()
    assert batch_row is not None
    assert batch_row[0] == "running"
    assert batch_row[1] is None
    assert [row[0] for row in item_rows] == ["running", "running"]


def test_rollback_does_not_touch_failed_or_acked_batches(tmp_path: Path) -> None:
    db_path = tmp_path / "library.db"
    bootstrap_library_schema(db_path)
    session_id = _insert_scan_session(db_path)
    photo_asset_ids = _insert_photo_assets(db_path, source_id=1, count=3)
    repo = DetectStageRepository(db_path)
    batch_ids = repo.seed_detect_batches(
        scan_session_id=session_id,
        photo_asset_ids=photo_asset_ids,
        workers=1,
        batch_size=1,
    )

    claim_acked = repo.claim_detect_batch(scan_session_id=session_id, worker_slot=0)
    assert claim_acked is not None
    repo.dispatch_batch(claim_acked.claim_token)
    repo.ack_detect_batch(claim_acked.claim_token)

    claim_failed = repo.claim_detect_batch(scan_session_id=session_id, worker_slot=0)
    assert claim_failed is not None
    repo.dispatch_batch(claim_failed.claim_token)
    failed_payload = tmp_path / "failed-payload.json"
    failed_payload.write_text(
        json.dumps(
            {
                "batch_id": claim_failed.batch_id,
                "claim_token": claim_failed.claim_token,
                "results": [
                    {
                        "scan_batch_item_id": claim_failed.items[0]["scan_batch_item_id"],
                        "photo_asset_id": claim_failed.items[0]["photo_asset_id"],
                        "status": "failed",
                        "error_message": "boom",
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    repo.ack_detect_batch(claim_failed.claim_token, worker_payload_path=failed_payload)

    claim_running = repo.claim_detect_batch(scan_session_id=session_id, worker_slot=0)
    assert claim_running is not None
    repo.dispatch_batch(claim_running.claim_token)

    touched = repo.rollback_unacked_batches(scan_session_id=session_id)
    assert touched == 1

    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            "SELECT id, status FROM scan_batch WHERE id IN (?, ?, ?) ORDER BY id",
            tuple(batch_ids),
        ).fetchall()
    assert rows == [
        (batch_ids[0], "acked"),
        (batch_ids[1], "failed"),
        (batch_ids[2], "claimed"),
    ]


def test_abort_rolls_back_unacked_batches(tmp_path: Path) -> None:
    db_path = tmp_path / "library.db"
    bootstrap_library_schema(db_path)
    session_id = _insert_scan_session(db_path)
    photo_asset_ids = _insert_photo_assets(db_path, source_id=1, count=3)

    detect_repo = DetectStageRepository(db_path)
    detect_repo.seed_detect_batches(
        scan_session_id=session_id,
        photo_asset_ids=photo_asset_ids,
        workers=1,
        batch_size=3,
    )
    claim = detect_repo.claim_detect_batch(scan_session_id=session_id, worker_slot=0)
    assert claim is not None
    detect_repo.dispatch_batch(claim.claim_token)

    rollback_unacked_batches_and_interrupt(
        detect_repo=detect_repo,
        session_id=session_id,
        last_error="manual abort",
    )

    with sqlite3.connect(db_path) as conn:
        session_row = conn.execute(
            "SELECT status, finished_at, last_error FROM scan_session WHERE id=?",
            (session_id,),
        ).fetchone()
        batch_row = conn.execute(
            "SELECT status, started_at, acked_at, retry_count FROM scan_batch WHERE id=?",
            (claim.batch_id,),
        ).fetchone()
        item_statuses = conn.execute(
            "SELECT status FROM scan_batch_item WHERE scan_batch_id=? ORDER BY item_order",
            (claim.batch_id,),
        ).fetchall()

    assert session_row is not None
    assert session_row[0] == "interrupted"
    assert session_row[1] is not None
    assert session_row[2] == "manual abort"
    assert batch_row is not None
    assert batch_row[0] == "claimed"
    assert batch_row[1] is None
    assert batch_row[2] is None
    assert batch_row[3] == 1
    assert [row[0] for row in item_statuses] == ["pending", "pending", "pending"]


def test_rollback_and_interrupt_rolls_back_all_when_session_status_illegal(tmp_path: Path) -> None:
    db_path = tmp_path / "library.db"
    bootstrap_library_schema(db_path)
    session_id = _insert_scan_session(db_path, status="completed")
    photo_asset_ids = _insert_photo_assets(db_path, source_id=1, count=2)
    repo = DetectStageRepository(db_path)
    repo.seed_detect_batches(
        scan_session_id=session_id,
        photo_asset_ids=photo_asset_ids,
        workers=1,
        batch_size=2,
    )
    claim = repo.claim_detect_batch(scan_session_id=session_id, worker_slot=0)
    assert claim is not None
    repo.dispatch_batch(claim.claim_token)

    with sqlite3.connect(db_path) as conn:
        before = conn.execute(
            "SELECT status, retry_count, started_at FROM scan_batch WHERE id=?",
            (claim.batch_id,),
        ).fetchone()
    assert before is not None
    assert before[0] == "running"
    assert before[1] == 0
    assert before[2] is not None

    with pytest.raises(ValueError, match="不是 running/aborting"):
        repo.rollback_unacked_batches_and_interrupt(
            scan_session_id=session_id,
            last_error="should rollback",
        )

    with sqlite3.connect(db_path) as conn:
        after_batch = conn.execute(
            "SELECT status, retry_count, started_at FROM scan_batch WHERE id=?",
            (claim.batch_id,),
        ).fetchone()
        after_session = conn.execute(
            "SELECT status, last_error FROM scan_session WHERE id=?",
            (session_id,),
        ).fetchone()
    assert after_batch == before
    assert after_session is not None
    assert after_session[0] == "completed"
    assert after_session[1] is None


def test_ack_payload_claim_token_mismatch_after_rollback_fails_without_state_change(tmp_path: Path) -> None:
    db_path = tmp_path / "library.db"
    bootstrap_library_schema(db_path)
    session_id = _insert_scan_session(db_path)
    photo_asset_ids = _insert_photo_assets(db_path, source_id=1, count=1)
    repo = DetectStageRepository(db_path)
    repo.seed_detect_batches(
        scan_session_id=session_id,
        photo_asset_ids=photo_asset_ids,
        workers=1,
        batch_size=1,
    )

    first_claim = repo.claim_detect_batch(scan_session_id=session_id, worker_slot=0)
    assert first_claim is not None
    repo.dispatch_batch(first_claim.claim_token)
    repo.rollback_unacked_batches(scan_session_id=session_id)

    second_claim = repo.claim_detect_batch(scan_session_id=session_id, worker_slot=0)
    assert second_claim is not None
    assert second_claim.batch_id == first_claim.batch_id
    assert second_claim.claim_token != first_claim.claim_token
    repo.dispatch_batch(second_claim.claim_token)

    payload_path = tmp_path / "stale-token-payload.json"
    payload_path.write_text(
        json.dumps(
            {
                "scan_batch_id": second_claim.batch_id,
                "claim_token": first_claim.claim_token,
                "results": [
                    {
                        "scan_batch_item_id": second_claim.items[0]["scan_batch_item_id"],
                        "photo_asset_id": second_claim.items[0]["photo_asset_id"],
                        "status": "done",
                        "error_message": None,
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="claim_token"):
        repo.ack_detect_batch(second_claim.claim_token, worker_payload_path=payload_path)

    with sqlite3.connect(db_path) as conn:
        batch_row = conn.execute(
            "SELECT status, acked_at, retry_count FROM scan_batch WHERE id=?",
            (second_claim.batch_id,),
        ).fetchone()
        item_rows = conn.execute(
            "SELECT status FROM scan_batch_item WHERE scan_batch_id=?",
            (second_claim.batch_id,),
        ).fetchall()
    assert batch_row is not None
    assert batch_row[0] == "running"
    assert batch_row[1] is None
    assert batch_row[2] == 1
    assert [row[0] for row in item_rows] == ["running"]


def test_ack_payload_missing_batch_id_raises_and_keeps_running(tmp_path: Path) -> None:
    db_path = tmp_path / "library.db"
    bootstrap_library_schema(db_path)
    session_id = _insert_scan_session(db_path)
    photo_asset_ids = _insert_photo_assets(db_path, source_id=1, count=1)
    repo = DetectStageRepository(db_path)
    repo.seed_detect_batches(
        scan_session_id=session_id,
        photo_asset_ids=photo_asset_ids,
        workers=1,
        batch_size=1,
    )
    claim = repo.claim_detect_batch(scan_session_id=session_id, worker_slot=0)
    assert claim is not None
    repo.dispatch_batch(claim.claim_token)

    payload_path = tmp_path / "missing-batch-id-payload.json"
    payload_path.write_text(
        json.dumps(
            {
                "claim_token": claim.claim_token,
                "results": [
                    {
                        "scan_batch_item_id": claim.items[0]["scan_batch_item_id"],
                        "photo_asset_id": claim.items[0]["photo_asset_id"],
                        "status": "done",
                        "error_message": None,
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="必须包含 batch 字段"):
        repo.ack_detect_batch(claim.claim_token, worker_payload_path=payload_path)

    with sqlite3.connect(db_path) as conn:
        batch_row = conn.execute(
            "SELECT status, acked_at FROM scan_batch WHERE id=?",
            (claim.batch_id,),
        ).fetchone()
        item_rows = conn.execute(
            "SELECT status FROM scan_batch_item WHERE scan_batch_id=?",
            (claim.batch_id,),
        ).fetchall()
    assert batch_row is not None
    assert batch_row[0] == "running"
    assert batch_row[1] is None
    assert [row[0] for row in item_rows] == ["running"]


def test_ack_payload_batch_id_mismatch_raises_and_keeps_running(tmp_path: Path) -> None:
    db_path = tmp_path / "library.db"
    bootstrap_library_schema(db_path)
    session_id = _insert_scan_session(db_path)
    photo_asset_ids = _insert_photo_assets(db_path, source_id=1, count=1)
    repo = DetectStageRepository(db_path)
    repo.seed_detect_batches(
        scan_session_id=session_id,
        photo_asset_ids=photo_asset_ids,
        workers=1,
        batch_size=1,
    )
    claim = repo.claim_detect_batch(scan_session_id=session_id, worker_slot=0)
    assert claim is not None
    repo.dispatch_batch(claim.claim_token)

    payload_path = tmp_path / "mismatch-batch-id-payload.json"
    payload_path.write_text(
        json.dumps(
            {
                "scan_batch_id": claim.batch_id + 1,
                "claim_token": claim.claim_token,
                "results": [
                    {
                        "scan_batch_item_id": claim.items[0]["scan_batch_item_id"],
                        "photo_asset_id": claim.items[0]["photo_asset_id"],
                        "status": "done",
                        "error_message": None,
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="scan_batch_id"):
        repo.ack_detect_batch(claim.claim_token, worker_payload_path=payload_path)

    with sqlite3.connect(db_path) as conn:
        batch_row = conn.execute(
            "SELECT status, acked_at FROM scan_batch WHERE id=?",
            (claim.batch_id,),
        ).fetchone()
        item_rows = conn.execute(
            "SELECT status FROM scan_batch_item WHERE scan_batch_id=?",
            (claim.batch_id,),
        ).fetchall()
    assert batch_row is not None
    assert batch_row[0] == "running"
    assert batch_row[1] is None
    assert [row[0] for row in item_rows] == ["running"]
