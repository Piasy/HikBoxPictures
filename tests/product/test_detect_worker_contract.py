from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from hikbox_pictures.product.db.schema_bootstrap import bootstrap_library_schema
from hikbox_pictures.product.scan.detect_worker import DetectWorkerRequest, run_detect_worker


def _insert_photo_asset(db_path: Path) -> int:
    with sqlite3.connect(db_path) as conn:
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
            VALUES (1, 'IMG_0001.HEIC', 'fp-1', 'sha256', 123, 456, NULL, NULL, 0, NULL, NULL, NULL, 'active', '2026-04-22T00:00:00+00:00', '2026-04-22T00:00:00+00:00')
            """
        )
        conn.commit()
        return int(cursor.lastrowid)


def test_worker_never_writes_business_tables(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path = tmp_path / "library.db"
    bootstrap_library_schema(db_path)
    photo_asset_id = _insert_photo_asset(db_path)

    output_path = tmp_path / "worker-output.json"
    request = DetectWorkerRequest(
        batch_id=7,
        claim_token="token-7",
        items=[{"scan_batch_item_id": 99, "photo_asset_id": photo_asset_id}],
    )

    def _forbidden_connect(*_: object, **__: object) -> sqlite3.Connection:
        raise AssertionError("worker 不允许直接连接业务 sqlite")

    monkeypatch.setattr(sqlite3, "connect", _forbidden_connect)

    run_detect_worker(request=request, output_path=output_path)

    payload = json.loads(output_path.read_text(encoding="utf-8"))
    assert payload["batch_id"] == 7
    assert payload["claim_token"] == "token-7"
    assert payload["results"][0]["scan_batch_item_id"] == 99
    assert payload["results"][0]["status"] == "done"


def test_worker_output_uses_atomic_rename(tmp_path: Path) -> None:
    output_path = tmp_path / "worker-output.json"
    request = DetectWorkerRequest(
        batch_id=9,
        claim_token="token-9",
        items=[{"scan_batch_item_id": 101, "photo_asset_id": 202}],
    )

    run_detect_worker(request=request, output_path=output_path)

    assert output_path.exists()
    assert not list(tmp_path.glob("*.tmp"))
    payload = json.loads(output_path.read_text(encoding="utf-8"))
    assert payload["batch_id"] == 9
    assert len(payload["results"]) == 1
