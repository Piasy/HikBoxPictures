"""test_hikbox_scan_cli 子文件共享 helper 函数与常量。"""

from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import time

from tests.helpers import (
    REPO_ROOT,
    FIXTURE_DIR,
    fetch_all,
)


SUPPORTED_SCAN_SUFFIXES = {".jpg", ".jpeg", ".png", ".heic", ".heif"}

OLD_SLICE_A_LIBRARY_SQL = """
CREATE TABLE schema_meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

INSERT INTO schema_meta (key, value) VALUES ('schema_version', '1');

CREATE TABLE library_sources (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  path TEXT NOT NULL UNIQUE,
  label TEXT NOT NULL,
  active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
  created_at TEXT NOT NULL
);
""".strip()

OLD_SLICE_A_EMBEDDING_SQL = """
CREATE TABLE schema_meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

INSERT INTO schema_meta (key, value) VALUES ('schema_version', '1');
""".strip()


# ---------------------------------------------------------------------------
# DB 查询辅助
# ---------------------------------------------------------------------------


def count_rows(db_path: Path, table_name: str) -> int:
    return count_rows_matching(db_path, f"SELECT COUNT(*) FROM {table_name}")


def count_rows_matching(
    db_path: Path,
    sql: str,
    attached_db: tuple[str, Path] | None = None,
    params: tuple[object, ...] = (),
) -> int:
    conn = sqlite3.connect(db_path)
    try:
        if attached_db is not None:
            conn.execute(f"ATTACH DATABASE ? AS {attached_db[0]}", (str(attached_db[1]),))
        row = conn.execute(sql, params).fetchone()
    finally:
        conn.close()
    assert row is not None
    return int(row[0])


def fetch_one(db_path: Path, sql: str, params: tuple[object, ...] = ()) -> tuple[object, ...]:
    return fetch_all(db_path, sql, params)[0]


def wait_for_batch_status(db_path: Path, *, batch_index: int, expected_status: str) -> None:
    deadline = time.time() + 90
    while time.time() < deadline:
        conn = sqlite3.connect(db_path)
        try:
            row = conn.execute(
                "SELECT status FROM scan_batches WHERE batch_index = ?",
                (batch_index,),
            ).fetchone()
        finally:
            conn.close()
        if row is not None and str(row[0]) == expected_status:
            return
        time.sleep(0.2)
    raise AssertionError(f"等待 batch_index={batch_index} 进入 {expected_status} 超时")


def count_batch_completed_events(log_path: Path, *, batch_index: int) -> int:
    count = 0
    with log_path.open("r", encoding="utf-8") as handle:
        for line in handle:
            payload = json.loads(line)
            if payload.get("event") == "batch_completed" and payload.get("batch_index") == batch_index:
                count += 1
    return count


# ---------------------------------------------------------------------------
# 构造旧版 workspace
# ---------------------------------------------------------------------------


def create_slice_a_only_workspace(workspace: Path, external_root: Path, source_dir: Path) -> None:
    workspace.mkdir(parents=True, exist_ok=True)
    hikbox_dir = workspace / ".hikbox"
    hikbox_dir.mkdir(parents=True, exist_ok=True)
    (external_root / "artifacts" / "crops").mkdir(parents=True, exist_ok=True)
    (external_root / "artifacts" / "context").mkdir(parents=True, exist_ok=True)
    (external_root / "logs").mkdir(parents=True, exist_ok=True)
    (hikbox_dir / "config.json").write_text(
        json.dumps(
            {
                "config_version": 1,
                "external_root": str(external_root.resolve()),
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    library_db = hikbox_dir / "library.db"
    embedding_db = hikbox_dir / "embedding.db"
    library_conn = sqlite3.connect(library_db)
    try:
        with library_conn:
            library_conn.executescript(OLD_SLICE_A_LIBRARY_SQL)
            library_conn.execute(
                """
                INSERT INTO library_sources (path, label, active, created_at)
                VALUES (?, 'legacy-source', 1, '2026-04-24T00:00:00Z')
                """,
                (str(source_dir.resolve()),),
            )
    finally:
        library_conn.close()

    embedding_conn = sqlite3.connect(embedding_db)
    try:
        with embedding_conn:
            embedding_conn.executescript(OLD_SLICE_A_EMBEDDING_SQL)
    finally:
        embedding_conn.close()


# ---------------------------------------------------------------------------
# spy / counter 模块构造
# ---------------------------------------------------------------------------


def prepare_faceanalysis_spy(root_dir: Path) -> tuple[Path, Path]:
    root_dir.mkdir(parents=True, exist_ok=True)
    spy_log_path = root_dir / "faceanalysis_spy.jsonl"
    sitecustomize_path = root_dir / "sitecustomize.py"
    sitecustomize_path.write_text(
        """
from __future__ import annotations

import json
import os
from pathlib import Path

import numpy as np
from insightface.app import FaceAnalysis as _FaceAnalysis

_ORIGINAL_INIT = _FaceAnalysis.__init__
_ORIGINAL_GET = _FaceAnalysis.get
_SPY_LOG = os.environ.get("HIKBOX_TEST_FACEANALYSIS_SPY_LOG")
_FORCE_BAD_EMBEDDING = os.environ.get("HIKBOX_TEST_FACEANALYSIS_FORCE_BAD_EMBEDDING") == "1"
_CORRUPT_WORKER_OUTPUT = os.environ.get("HIKBOX_TEST_CORRUPT_WORKER_OUTPUT") == "1"
_FAIL_SECOND_ARTIFACT_MOVE = os.environ.get("HIKBOX_TEST_FAIL_SECOND_ARTIFACT_MOVE") == "1"
_FAIL_OLD_ARTIFACT_CLEANUP = os.environ.get("HIKBOX_TEST_FAIL_OLD_ARTIFACT_CLEANUP") == "1"


def _append(payload: dict[str, object]) -> None:
    if not _SPY_LOG:
        return
    path = Path(_SPY_LOG)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False) + "\\n")


def _spy_init(self, *args, **kwargs):
    _append(
        {
            "event": "faceanalysis_init",
            "name": kwargs.get("name", args[0] if args else None),
            "root": str(kwargs.get("root")),
        }
    )
    return _ORIGINAL_INIT(self, *args, **kwargs)


def _spy_get(self, *args, **kwargs):
    faces = _ORIGINAL_GET(self, *args, **kwargs)
    if _FORCE_BAD_EMBEDDING and faces:
        bad_embedding = np.asarray(faces[0].normed_embedding, dtype=np.float32)[:128]

        class _BadFace:
            def __init__(self, wrapped_face, forced_embedding):
                self._wrapped_face = wrapped_face
                self._forced_embedding = forced_embedding

            @property
            def bbox(self):
                return self._wrapped_face.bbox

            @property
            def det_score(self):
                return self._wrapped_face.det_score

            @property
            def normed_embedding(self):
                return self._forced_embedding

            def __getattr__(self, name):
                return getattr(self._wrapped_face, name)

        faces = [_BadFace(faces[0], bad_embedding), *faces[1:]]
    return faces


def _spy_subprocess_run(*args, **kwargs):
    result = _ORIGINAL_SUBPROCESS_RUN(*args, **kwargs)
    command = args[0] if args else kwargs.get("args")
    if (
        _CORRUPT_WORKER_OUTPUT
        and isinstance(command, list)
        and "hikbox_pictures.product.scan_worker" in command
        and result.returncode == 0
        and "--output-json" in command
    ):
        output_json = Path(command[command.index("--output-json") + 1])
        payload = json.loads(output_json.read_text(encoding="utf-8"))
        first_item = payload["items"][0]
        if first_item["status"] == "succeeded" and first_item["detections"]:
            first_item["detections"][0]["embedding"] = first_item["detections"][0]["embedding"][:128]
            output_json.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return result


_FaceAnalysis.__init__ = _spy_init
_FaceAnalysis.get = _spy_get
import subprocess as _subprocess
_ORIGINAL_SUBPROCESS_RUN = _subprocess.run
_subprocess.run = _spy_subprocess_run

import hikbox_pictures.product.scan as _scan
_ORIGINAL_RUN_SCAN_WORKER = getattr(_scan, "_run_scan_worker", None)


def _corrupt_worker_result(worker_result):
    first_item = worker_result["items"][0]
    if first_item["status"] == "succeeded" and first_item["detections"]:
        first_item["detections"][0]["embedding"] = first_item["detections"][0]["embedding"][:128]
    return worker_result


def _spy_run_scan_worker(*args, **kwargs):
    worker_result = _ORIGINAL_RUN_SCAN_WORKER(*args, **kwargs)
    if _CORRUPT_WORKER_OUTPUT:
        worker_result = _corrupt_worker_result(worker_result)
    return worker_result


if _ORIGINAL_RUN_SCAN_WORKER is not None:
    _scan._run_scan_worker = _spy_run_scan_worker

import shutil as _shutil
_ORIGINAL_SHUTIL_MOVE = _shutil.move
_ARTIFACT_MOVE_COUNT = 0


def _spy_shutil_move(src, dst, *args, **kwargs):
    global _ARTIFACT_MOVE_COUNT
    if _FAIL_SECOND_ARTIFACT_MOVE and "artifacts" in str(dst):
        _ARTIFACT_MOVE_COUNT += 1
        if _ARTIFACT_MOVE_COUNT == 2:
            raise OSError("测试注入：第二次 artifact move 失败")
    return _ORIGINAL_SHUTIL_MOVE(src, dst, *args, **kwargs)


_shutil.move = _spy_shutil_move

_ORIGINAL_SCAN_CLEANUP = _scan._cleanup_final_artifacts
_OLD_ARTIFACT_CLEANUP_FAILED = False


def _spy_cleanup_final_artifacts(paths):
    global _OLD_ARTIFACT_CLEANUP_FAILED
    if _FAIL_OLD_ARTIFACT_CLEANUP and paths and not _OLD_ARTIFACT_CLEANUP_FAILED:
        _OLD_ARTIFACT_CLEANUP_FAILED = True
        raise OSError("测试注入：旧 artifact 清理失败")
    return _ORIGINAL_SCAN_CLEANUP(paths)


_scan._cleanup_final_artifacts = _spy_cleanup_final_artifacts
""".strip()
        + "\n",
        encoding="utf-8",
    )
    return root_dir, spy_log_path


def read_jsonl(path: Path) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            records.append(json.loads(line))
    return records


def normalized_stderr(stderr_text: str) -> str:
    lines = [
        line
        for line in stderr_text.splitlines()
        if line.strip() != "Matplotlib is building the font cache; this may take a moment."
        and not line.strip().startswith("scan 进度:")
    ]
    return "\n".join(lines).strip()


def scan_progress_lines(stderr_text: str) -> list[str]:
    return [line.strip() for line in stderr_text.splitlines() if line.strip().startswith("scan 进度:")]


def write_named_source_copies(source_dir: Path, file_names: list[str]) -> None:
    source_dir.mkdir(parents=True, exist_ok=True)
    sample_bytes = (FIXTURE_DIR / "pg_001_single_alex_01.jpg").read_bytes()
    for file_name in file_names:
        (source_dir / file_name).write_bytes(sample_bytes)


def create_scan_batches_for_paths(
    *,
    tmp_path: Path,
    source_dir: Path,
    batches: list[list[Path]],
) -> tuple[Path, list[int]]:
    library_db = tmp_path / "library.db"
    library_sql = (REPO_ROOT / "hikbox_pictures" / "product" / "db" / "sql" / "library_v1.sql").read_text(
        encoding="utf-8"
    )
    connection = sqlite3.connect(library_db)
    try:
        with connection:
            connection.executescript(library_sql)
            source_id = int(
                connection.execute(
                    """
                    INSERT INTO library_sources (path, label, active, scan_state, created_at)
                    VALUES (?, 'test', 1, 'pending', '2026-05-05T00:00:00Z')
                    """,
                    (str(source_dir.resolve()),),
                ).lastrowid
            )
            session_id = int(
                connection.execute(
                    """
                    INSERT INTO scan_sessions (batch_size, status, command, total_batches, started_at)
                    VALUES (?, 'running', 'hikbox-pictures scan start', ?, '2026-05-05T00:00:00Z')
                    """,
                    (max(len(batch) for batch in batches), len(batches)),
                ).lastrowid
            )
            batch_ids = []
            for batch_index, image_paths in enumerate(batches, start=1):
                batch_id = int(
                    connection.execute(
                        """
                        INSERT INTO scan_batches (session_id, batch_index, status, item_count)
                        VALUES (?, ?, 'pending', ?)
                        """,
                        (session_id, batch_index, len(image_paths)),
                    ).lastrowid
                )
                batch_ids.append(batch_id)
                for item_index, image_path in enumerate(image_paths, start=1):
                    connection.execute(
                        """
                        INSERT INTO scan_batch_items (batch_id, item_index, source_id, absolute_path, status)
                        VALUES (?, ?, ?, ?, 'pending')
                        """,
                        (batch_id, item_index, source_id, str(image_path.resolve())),
                    )
    finally:
        connection.close()
    return library_db, batch_ids


def prepare_discover_counter(tmp_path: Path) -> tuple[Path, Path]:
    """生成用于子进程的文件系统调用计数模块。

    返回 (counter_dir, count_file)。counter_dir 应通过 pythonpath_prepend
    传入 run_hikbox，count_file 用于读取最终计数结果。
    """
    counter_dir = tmp_path / "discover_counter"
    counter_dir.mkdir()
    count_file = counter_dir / "counts.json"
    sitecustomize = counter_dir / "sitecustomize.py"
    sitecustomize.write_text(
        f"""
import atexit
import inspect
import json
import os
from pathlib import Path

_iterdir_count = 0
_scandir_count = 0

def _is_inside_discover_candidates():
    for frame_info in inspect.stack():
        if frame_info.function == "_discover_candidates":
            return True
    return False

_original_iterdir = Path.iterdir
def _patched_iterdir(self):
    if _is_inside_discover_candidates():
        global _iterdir_count
        _iterdir_count += 1
    return _original_iterdir(self)

_original_scandir = os.scandir
def _patched_scandir(path):
    if _is_inside_discover_candidates():
        global _scandir_count
        _scandir_count += 1
    return _original_scandir(path)

Path.iterdir = _patched_iterdir
os.scandir = _patched_scandir

_count_file = {str(count_file)!r}

def _write_counts():
    with open(_count_file, "w", encoding="utf-8") as f:
        json.dump({{"iterdir": _iterdir_count, "scandir": _scandir_count}}, f)

atexit.register(_write_counts)
""",
        encoding="utf-8",
    )
    return counter_dir, count_file
