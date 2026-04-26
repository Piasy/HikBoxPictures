from __future__ import annotations

import io
import json
import os
from pathlib import Path
import signal
import stat
import shutil
import sqlite3
import subprocess
import sys
import time

import numpy as np
from PIL import Image
import pytest

import hikbox_pictures.product.scan as scan_module
import hikbox_pictures.product.scan_worker as scan_worker_module


REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures" / "people_gallery_scan"
MANIFEST_PATH = FIXTURE_DIR / "manifest.json"
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


def _run_hikbox(
    *args: str,
    cwd: Path | None = None,
    env_updates: dict[str, str] | None = None,
    pythonpath_prepend: list[Path] | None = None,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    pythonpath_parts = [str(path) for path in (pythonpath_prepend or [])]
    pythonpath_parts.append(str(REPO_ROOT))
    existing_pythonpath = env.get("PYTHONPATH")
    if existing_pythonpath:
        pythonpath_parts.append(existing_pythonpath)
    env["PYTHONPATH"] = os.pathsep.join(pythonpath_parts)
    if env_updates:
        env.update(env_updates)
    return subprocess.run(
        [sys.executable, "-m", "hikbox_pictures", *args],
        cwd=cwd or REPO_ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def _spawn_hikbox(
    *args: str,
    cwd: Path | None = None,
    env_updates: dict[str, str] | None = None,
    pythonpath_prepend: list[Path] | None = None,
) -> subprocess.Popen[str]:
    env = os.environ.copy()
    pythonpath_parts = [str(path) for path in (pythonpath_prepend or [])]
    pythonpath_parts.append(str(REPO_ROOT))
    existing_pythonpath = env.get("PYTHONPATH")
    if existing_pythonpath:
        pythonpath_parts.append(existing_pythonpath)
    env["PYTHONPATH"] = os.pathsep.join(pythonpath_parts)
    if env_updates:
        env.update(env_updates)
    return subprocess.Popen(
        [sys.executable, "-m", "hikbox_pictures", *args],
        cwd=cwd or REPO_ROOT,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def _init_workspace(workspace: Path, external_root: Path) -> subprocess.CompletedProcess[str]:
    return _run_hikbox(
        "init",
        "--workspace",
        str(workspace),
        "--external-root",
        str(external_root),
    )


def _add_source(workspace: Path, source_dir: Path) -> subprocess.CompletedProcess[str]:
    return _run_hikbox(
        "source",
        "add",
        "--workspace",
        str(workspace),
        str(source_dir),
    )


def _prepare_workspace_models(workspace: Path) -> Path:
    source_root = _find_model_root()
    target_root = workspace / ".hikbox" / "models" / "insightface"
    if target_root.exists():
        shutil.rmtree(target_root)
    shutil.copytree(source_root, target_root)
    return target_root


def _find_model_root() -> Path:
    candidates = [REPO_ROOT / ".insightface", Path.home() / ".insightface"]
    candidates.extend(parent / ".insightface" for parent in REPO_ROOT.parents)
    for candidate in candidates:
        if (candidate / "models" / "buffalo_l" / "det_10g.onnx").exists():
            return candidate
    raise AssertionError("缺少 InsightFace buffalo_l 模型目录，无法执行 scan CLI 集成测试")


def _read_manifest() -> dict[str, object]:
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


def test_scan_start_fails_without_initialized_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"

    result = _run_hikbox(
        "scan",
        "start",
        "--workspace",
        str(workspace),
    )

    assert result.returncode != 0
    assert "工作区" in result.stderr
    assert not (workspace / ".hikbox").exists()


def test_scan_start_rejects_invalid_batch_size(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    external_root = tmp_path / "external-root"
    init_result = _init_workspace(workspace, external_root)
    assert init_result.returncode == 0

    invalid_results = [
        _run_hikbox("scan", "start", "--workspace", str(workspace), "--batch-size", "0"),
        _run_hikbox("scan", "start", "--workspace", str(workspace), "--batch-size", "-1"),
        _run_hikbox("scan", "start", "--workspace", str(workspace), "--batch-size", "abc"),
    ]

    for result in invalid_results:
        assert result.returncode != 0
        assert "batch-size" in result.stderr
        assert "正整数" in result.stderr


def test_scan_start_fails_when_no_active_source_exists(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    external_root = tmp_path / "external-root"
    init_result = _init_workspace(workspace, external_root)
    assert init_result.returncode == 0
    _prepare_workspace_models(workspace)

    result = _run_hikbox(
        "scan",
        "start",
        "--workspace",
        str(workspace),
    )

    assert result.returncode != 0
    assert "active source" in result.stderr or "没有可用 source" in result.stderr
    assert _count_rows(workspace / ".hikbox" / "library.db", "library_sources") == 0


def test_scan_start_fails_cleanly_for_slice_a_only_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace-slice-a-only"
    external_root = tmp_path / "external-root-slice-a-only"
    source_dir = tmp_path / "source-slice-a-only"
    source_dir.mkdir()
    (source_dir / "sample.jpg").write_bytes((FIXTURE_DIR / "pg_001_single_alex_01.jpg").read_bytes())
    _create_slice_a_only_workspace(workspace, external_root, source_dir)

    result = _run_hikbox(
        "scan",
        "start",
        "--workspace",
        str(workspace),
    )

    assert result.returncode != 0
    assert "Traceback" not in result.stderr
    normalized_stderr = _normalized_stderr(result.stderr)
    assert normalized_stderr.startswith("scan start 失败:")
    assert "缺少扫描表" in normalized_stderr
    assert "不支持自动升级" in normalized_stderr
    assert "hikbox-pictures init" in normalized_stderr
    assert "source add" in normalized_stderr
    assert "scan start" in normalized_stderr
    assert "no such table" not in normalized_stderr
    assert "scan session 初始化失败" not in normalized_stderr


def test_scan_start_downgrades_unreadable_supported_file_to_asset_failure(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace-unreadable-file"
    external_root = tmp_path / "external-root-unreadable-file"
    source_dir = tmp_path / "source-unreadable-file"
    source_dir.mkdir()
    readable_path = source_dir / "readable.jpg"
    unreadable_path = source_dir / "unreadable.jpg"
    readable_path.write_bytes((FIXTURE_DIR / "pg_001_single_alex_01.jpg").read_bytes())
    unreadable_path.write_bytes((FIXTURE_DIR / "pg_002_single_alex_02.jpg").read_bytes())

    init_result = _init_workspace(workspace, external_root)
    assert init_result.returncode == 0
    _prepare_workspace_models(workspace)
    add_result = _add_source(workspace, source_dir)
    assert add_result.returncode == 0

    original_mode = stat.S_IMODE(unreadable_path.stat().st_mode)
    unreadable_path.chmod(0)
    try:
        result = _run_hikbox(
            "scan",
            "start",
            "--workspace",
            str(workspace),
            "--batch-size",
            "10",
        )
    finally:
        unreadable_path.chmod(original_mode)

    assert result.returncode == 0, result.stderr
    assert "Traceback" not in result.stderr
    assert _normalized_stderr(result.stderr) == ""

    library_db = workspace / ".hikbox" / "library.db"
    embedding_db = workspace / ".hikbox" / "embedding.db"
    assert _fetch_one(
        library_db,
        """
        SELECT processing_status, failure_reason
        FROM assets
        WHERE file_name = 'unreadable.jpg'
        """,
    )[0] == "failed"
    unreadable_reason = _fetch_one(
        library_db,
        """
        SELECT failure_reason
        FROM assets
        WHERE file_name = 'unreadable.jpg'
        """,
    )[0]
    assert unreadable_reason
    assert _fetch_one(
        library_db,
        """
        SELECT processing_status
        FROM assets
        WHERE file_name = 'readable.jpg'
        """,
    )[0] == "succeeded"
    assert _count_rows(library_db, "face_observations") > 0
    assert _count_rows(embedding_db, "face_embeddings") > 0
    assert _fetch_one(
        library_db,
        """
        SELECT status, failed_assets
        FROM scan_sessions
        ORDER BY id DESC
        LIMIT 1
        """,
    ) == ("completed", 1)
    scan_events = _read_jsonl(external_root / "logs" / "scan.log.jsonl")
    assert any(
        event["event"] == "asset_failed" and event.get("asset_path") == str(unreadable_path.resolve())
        for event in scan_events
    )


def test_scan_start_handles_duplicate_content_assets_in_same_batch_without_artifact_name_collision(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace-duplicate-content"
    external_root = tmp_path / "external-root-duplicate-content"
    source_dir = tmp_path / "source-duplicate-content"
    source_dir.mkdir()
    duplicate_bytes = (FIXTURE_DIR / "pg_001_single_alex_01.jpg").read_bytes()
    first_path = source_dir / "duplicate_a.jpg"
    second_path = source_dir / "duplicate_b.jpg"
    first_path.write_bytes(duplicate_bytes)
    second_path.write_bytes(duplicate_bytes)

    init_result = _init_workspace(workspace, external_root)
    assert init_result.returncode == 0
    _prepare_workspace_models(workspace)
    add_result = _add_source(workspace, source_dir)
    assert add_result.returncode == 0

    result = _run_hikbox(
        "scan",
        "start",
        "--workspace",
        str(workspace),
        "--batch-size",
        "10",
    )

    assert result.returncode == 0, result.stderr
    assert "Traceback" not in result.stderr
    library_db = workspace / ".hikbox" / "library.db"
    embedding_db = workspace / ".hikbox" / "embedding.db"
    assert _fetch_one(
        library_db,
        """
        SELECT status
        FROM scan_sessions
        ORDER BY id DESC
        LIMIT 1
        """,
    )[0] == "completed"
    assert _count_rows_matching(
        library_db,
        """
        SELECT COUNT(*)
        FROM assets
        WHERE file_name IN ('duplicate_a.jpg', 'duplicate_b.jpg')
        """,
    ) == 2
    face_rows = _fetch_all(
        library_db,
        """
        SELECT assets.file_name, face_observations.crop_path, face_observations.context_path
        FROM face_observations
        INNER JOIN assets ON assets.id = face_observations.asset_id
        WHERE assets.file_name IN ('duplicate_a.jpg', 'duplicate_b.jpg')
        ORDER BY assets.file_name ASC, face_observations.face_index ASC
        """,
    )
    assert len(face_rows) >= 2
    crop_paths = [Path(str(row[1])) for row in face_rows]
    context_paths = [Path(str(row[2])) for row in face_rows]
    assert len({str(path) for path in crop_paths}) == len(crop_paths)
    assert len({str(path) for path in context_paths}) == len(context_paths)
    for path in [*crop_paths, *context_paths]:
        assert path.is_file()
        with Image.open(path) as image:
            image.load()
    assert _count_rows(embedding_db, "face_embeddings") == _count_rows(library_db, "face_observations")


def test_scan_start_reports_log_path_io_failure_without_traceback(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace-log-path-failure"
    external_root = tmp_path / "external-root-log-path-failure"
    init_result = _init_workspace(workspace, external_root)
    assert init_result.returncode == 0
    _prepare_workspace_models(workspace)
    add_result = _add_source(workspace, FIXTURE_DIR)
    assert add_result.returncode == 0

    logs_dir = external_root / "logs"
    shutil.rmtree(logs_dir)
    logs_dir.write_text("occupied-by-file", encoding="utf-8")

    result = _run_hikbox(
        "scan",
        "start",
        "--workspace",
        str(workspace),
        "--batch-size",
        "10",
    )

    assert result.returncode != 0
    assert "Traceback" not in result.stderr
    normalized_stderr = _normalized_stderr(result.stderr)
    assert normalized_stderr.startswith("scan start 失败:")
    assert "日志" in normalized_stderr
    assert "logs" in normalized_stderr or "scan.log.jsonl" in normalized_stderr


@pytest.mark.parametrize(
    ("source_state", "expected_message"),
    [
        ("missing", "source 路径不存在"),
        ("file", "source 不是目录"),
        ("unreadable", "source 不可读"),
    ],
)
def test_scan_start_fails_when_source_becomes_invalid(
    tmp_path: Path,
    source_state: str,
    expected_message: str,
) -> None:
    workspace = tmp_path / f"workspace-{source_state}"
    external_root = tmp_path / f"external-root-{source_state}"
    source_dir = tmp_path / f"source-{source_state}"
    source_dir.mkdir()
    (source_dir / "sample.jpg").write_bytes((FIXTURE_DIR / "pg_001_single_alex_01.jpg").read_bytes())

    init_result = _init_workspace(workspace, external_root)
    assert init_result.returncode == 0
    _prepare_workspace_models(workspace)
    add_result = _add_source(workspace, source_dir)
    assert add_result.returncode == 0

    original_mode = None
    if source_state == "missing":
        shutil.rmtree(source_dir)
    elif source_state == "file":
        shutil.rmtree(source_dir)
        source_dir.write_text("not-a-directory", encoding="utf-8")
    elif source_state == "unreadable":
        original_mode = stat.S_IMODE(source_dir.stat().st_mode)
        source_dir.chmod(0)

    try:
        result = _run_hikbox(
            "scan",
            "start",
            "--workspace",
            str(workspace),
        )
    finally:
        if source_state == "unreadable" and source_dir.exists():
            source_dir.chmod(original_mode if original_mode is not None else 0o755)

    assert result.returncode != 0
    assert expected_message in result.stderr
    library_db = workspace / ".hikbox" / "library.db"
    assert _count_rows_matching(library_db, "SELECT COUNT(*) FROM scan_batches WHERE status = 'completed'") == 0


def test_scan_start_runs_fixture_pipeline_and_persists_outputs(tmp_path: Path) -> None:
    manifest = _read_manifest()
    scan_candidate_assets = [
        asset for asset in manifest["assets"] if Path(asset["file"]).suffix.lower() in SUPPORTED_SCAN_SUFFIXES
    ]
    workspace = tmp_path / "workspace"
    external_root = tmp_path / "external-root"

    init_result = _init_workspace(workspace, external_root)
    assert init_result.returncode == 0
    _prepare_workspace_models(workspace)
    home_without_models = tmp_path / "home-without-insightface"
    home_without_models.mkdir()
    spy_dir, spy_log_path = _prepare_faceanalysis_spy(tmp_path / "spy-success")

    add_result = _add_source(workspace, FIXTURE_DIR)
    assert add_result.returncode == 0

    result = _run_hikbox(
        "scan",
        "start",
        "--workspace",
        str(workspace),
        "--batch-size",
        "10",
        env_updates={
            "HOME": str(home_without_models),
            "HIKBOX_TEST_FACEANALYSIS_SPY_LOG": str(spy_log_path),
        },
        pythonpath_prepend=[spy_dir],
    )

    assert result.returncode == 0, result.stderr
    assert _normalized_stderr(result.stderr) == ""

    library_db = workspace / ".hikbox" / "library.db"
    embedding_db = workspace / ".hikbox" / "embedding.db"
    crops_dir = external_root / "artifacts" / "crops"
    context_dir = external_root / "artifacts" / "context"
    logs_dir = external_root / "logs"

    assert _count_rows(library_db, "scan_sessions") == 1
    assert _count_rows(library_db, "scan_batches") == 6
    assert _count_rows(library_db, "scan_batch_items") == len(scan_candidate_assets)
    assert _count_rows(library_db, "assets") == len(scan_candidate_assets)
    assert _count_rows(library_db, "face_observations") > 0
    assert _count_rows(embedding_db, "face_embeddings") == _count_rows(library_db, "face_observations")
    assert any(crops_dir.iterdir())
    assert any(context_dir.iterdir())
    assert any(logs_dir.iterdir())

    scan_summary = _fetch_one(
        library_db,
        """
        SELECT total_batches, completed_batches, failed_assets, success_faces, artifact_files
        FROM scan_sessions
        ORDER BY id DESC
        LIMIT 1
        """,
    )
    assert scan_summary == (6, 6, 1, scan_summary[3], scan_summary[4])
    assert scan_summary[3] > 0
    assert scan_summary[4] == scan_summary[3] * 2
    assert _count_rows_matching(
        embedding_db,
        "SELECT COUNT(*) FROM face_embeddings WHERE variant != 'main'",
    ) == 0

    embedding_dimension, embedding_norm = _fetch_one(
        embedding_db,
        """
        SELECT dimension, l2_norm
        FROM face_embeddings
        ORDER BY id ASC
        LIMIT 1
        """,
    )
    assert embedding_dimension == 512
    assert abs(float(embedding_norm) - 1.0) < 1e-3
    orphan_embeddings = _count_rows_matching(
        workspace / ".hikbox" / "library.db",
        """
        SELECT COUNT(*)
        FROM (
          SELECT embedding.face_embeddings.id
          FROM embedding.face_embeddings
          LEFT JOIN main.face_observations
            ON main.face_observations.id = embedding.face_embeddings.face_observation_id
          WHERE main.face_observations.id IS NULL
        )
        """,
        attached_db=("embedding", embedding_db),
    )
    assert orphan_embeddings == 0

    positive_pairs = _fetch_one(
        library_db,
        """
        SELECT COUNT(*)
        FROM assets
        WHERE live_photo_mov_path IS NOT NULL
        """,
    )[0]
    assert positive_pairs == 2
    negative_pairs = _fetch_one(
        library_db,
        """
        SELECT COUNT(*)
        FROM assets
        WHERE file_name IN ('pg_049_live_negative_01.jpg', 'pg_050_live_negative_02.png')
          AND live_photo_mov_path IS NOT NULL
        """,
    )[0]
    assert negative_pairs == 0
    positive_pair_rows = _fetch_all(
        library_db,
        """
        SELECT file_name, live_photo_mov_path
        FROM assets
        WHERE file_name IN ('pg_047_live_positive_01.HEIC', 'pg_048_live_positive_02.heif')
        ORDER BY file_name ASC
        """,
    )
    assert positive_pair_rows == [
        ("pg_047_live_positive_01.HEIC", str((FIXTURE_DIR / ".pg_047_live_positive_01.MOV").resolve())),
        ("pg_048_live_positive_02.heif", str((FIXTURE_DIR / ".pg_048_live_positive_02.mov").resolve())),
    ]
    corrupt_row = _fetch_one(
        library_db,
        """
        SELECT processing_status, failure_reason
        FROM assets
        WHERE file_name = 'pg_902_corrupt.jpg'
        """,
    )
    assert corrupt_row[0] == "failed"
    assert corrupt_row[1]
    unsupported_count = _count_rows_matching(
        library_db,
        "SELECT COUNT(*) FROM assets WHERE file_name = 'pg_901_unsupported.txt'",
    )
    assert unsupported_count == 0

    crop_path, context_path = _fetch_one(
        library_db,
        """
        SELECT crop_path, context_path
        FROM face_observations
        ORDER BY id ASC
        LIMIT 1
        """,
    )
    assert Path(crop_path).is_file()
    assert Path(context_path).is_file()

    with Image.open(context_path) as context_image:
        assert max(context_image.size) <= 480
        pixels = np.asarray(context_image.convert("RGB"), dtype=np.uint8)
    red_box_pixels = np.count_nonzero(
        (pixels[:, :, 0] >= 180) & (pixels[:, :, 1] <= 90) & (pixels[:, :, 2] <= 90)
    )
    assert red_box_pixels > 0

    scan_log_text = (logs_dir / "scan.log.jsonl").read_text(encoding="utf-8")
    assert str(workspace / ".hikbox" / "models" / "insightface") in scan_log_text
    assert str(home_without_models / ".insightface") not in scan_log_text
    spy_records = _read_jsonl(spy_log_path)
    assert spy_records
    assert {record["event"] for record in spy_records} == {"faceanalysis_init"}
    assert {record["name"] for record in spy_records} == {"buffalo_l"}
    assert {record["root"] for record in spy_records} == {str((workspace / ".hikbox" / "models" / "insightface").resolve())}


def test_scan_worker_emits_batch_progress_events(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    input_path = tmp_path / "input.json"
    output_path = tmp_path / "output.json"
    input_path.write_text(
        json.dumps(
            {
                "model_root": str(tmp_path / "models"),
                "staging_dir": str(tmp_path / "staging"),
                "items": [
                    {
                        "absolute_path": str((tmp_path / "photo-1.jpg").resolve()),
                        "file_fingerprint": "fingerprint-1",
                        "item_index": 1,
                    },
                    {
                        "absolute_path": str((tmp_path / "photo-2.jpg").resolve()),
                        "file_fingerprint": "fingerprint-2",
                        "item_index": 2,
                    },
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    class _FakeBackend:
        def __init__(self, *, model_root: Path) -> None:
            self.model_root = model_root

        def detect(self, image_path: Path) -> tuple[int, int, list[dict[str, object]]]:
            time.sleep(0.12)
            return (320, 240, [])

    monkeypatch.setattr(scan_worker_module, "_InsightFaceWorkerBackend", _FakeBackend)
    monkeypatch.setattr(scan_worker_module, "_SCAN_WORKER_PROGRESS_INTERVAL_SECONDS", 0.05, raising=False)

    exit_code = scan_worker_module.main(
        [
            "--input-json",
            str(input_path),
            "--output-json",
            str(output_path),
        ]
    )

    assert exit_code == 0
    stdout_lines = [line for line in capsys.readouterr().out.splitlines() if line.strip()]
    progress_events = [json.loads(line) for line in stdout_lines]
    assert any(
        event.get("event") == "batch_progress" and event.get("total_items") == 2
        for event in progress_events
    )
    assert output_path.is_file()


def test_start_scan_prints_batch_and_assignment_progress(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys,
) -> None:
    workspace = tmp_path / "workspace-progress"
    external_root = tmp_path / "external-root-progress"
    source_dir = tmp_path / "source-progress"
    _write_named_source_copies(source_dir, ["photo_01.jpg", "photo_02.jpg", "photo_03.jpg"])

    init_result = _init_workspace(workspace, external_root)
    assert init_result.returncode == 0
    add_result = _add_source(workspace, source_dir)
    assert add_result.returncode == 0

    def _build_worker_payload(command: list[str]) -> tuple[dict[str, object], Path]:
        input_json = Path(command[command.index("--input-json") + 1])
        output_json = Path(command[command.index("--output-json") + 1])
        payload = json.loads(input_json.read_text(encoding="utf-8"))
        output_json.write_text(
            json.dumps(
                {
                    "model_root": str(payload["model_root"]),
                    "processed_at": "2026-04-26T00:00:00Z",
                    "items": [
                        {
                            "absolute_path": str(item["absolute_path"]),
                            "status": "succeeded",
                            "image_width": 320,
                            "image_height": 240,
                            "face_count": 0,
                            "detections": [],
                            "artifacts": [],
                        }
                        for item in payload["items"]
                    ],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        return payload, output_json

    def _fake_run(command: list[str], **_kwargs) -> subprocess.CompletedProcess[str]:
        _build_worker_payload(command)
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    class _FakePopen:
        def __init__(self, command: list[str], **_kwargs) -> None:
            payload, _output_json = _build_worker_payload(command)
            total_items = len(payload["items"])
            self.args = command
            self.returncode = 0
            self.stdout = io.StringIO(
                "".join(
                    json.dumps(
                        {
                            "event": "batch_progress",
                            "completed_items": completed_items,
                            "total_items": total_items,
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                    for completed_items in range(1, total_items + 1)
                )
            )
            self.stderr = io.StringIO("")

        def wait(self) -> int:
            return self.returncode

        def poll(self) -> int:
            return self.returncode

        def communicate(self) -> tuple[str, str]:
            return (self.stdout.read(), self.stderr.read())

    def _fake_run_online_assignment(
        *,
        workspace_context,
        scan_session_id: int,
        append_log,
        progress_callback=None,
    ) -> None:
        append_log(
            {
                "timestamp": "2026-04-26T00:00:00Z",
                "event": "assignment_started",
                "session_id": scan_session_id,
                "assignment_run_id": 1,
            }
        )
        if progress_callback is not None:
            progress_callback("started")
            progress_callback("completed")

    monkeypatch.setattr(scan_module.subprocess, "run", _fake_run)
    monkeypatch.setattr(scan_module.subprocess, "Popen", _FakePopen)
    monkeypatch.setattr(scan_module, "run_online_assignment", _fake_run_online_assignment)
    monkeypatch.setattr(scan_module, "_SCAN_PROGRESS_INTERVAL_SECONDS", 0.5, raising=False)

    scan_module.start_scan(
        workspace=workspace,
        batch_size=2,
        command_args=["scan", "start", "--workspace", str(workspace), "--batch-size", "2"],
    )

    progress_lines = _scan_progress_lines(capsys.readouterr().err)
    assert "scan 进度: 阶段=批处理，批次 0/2，照片 1/3" in progress_lines
    assert "scan 进度: 阶段=在线归属，批次 2/2，照片 3/3" in progress_lines


def test_scan_start_fails_when_embedding_dimension_is_not_512_and_logs_failure(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace-bad-embedding"
    external_root = tmp_path / "external-root-bad-embedding"
    init_result = _init_workspace(workspace, external_root)
    assert init_result.returncode == 0
    _prepare_workspace_models(workspace)
    add_result = _add_source(workspace, FIXTURE_DIR)
    assert add_result.returncode == 0
    spy_dir, spy_log_path = _prepare_faceanalysis_spy(tmp_path / "spy-bad-embedding")

    result = _run_hikbox(
        "scan",
        "start",
        "--workspace",
        str(workspace),
        "--batch-size",
        "10",
        env_updates={
            "HIKBOX_TEST_FACEANALYSIS_SPY_LOG": str(spy_log_path),
            "HIKBOX_TEST_FACEANALYSIS_FORCE_BAD_EMBEDDING": "1",
        },
        pythonpath_prepend=[spy_dir],
    )

    assert result.returncode != 0
    assert "embedding 维度错误" in result.stderr
    library_db = workspace / ".hikbox" / "library.db"
    embedding_db = workspace / ".hikbox" / "embedding.db"
    assert _fetch_one(
        library_db,
        """
        SELECT status, completed_batches
        FROM scan_sessions
        ORDER BY id DESC
        LIMIT 1
        """,
    ) == ("failed", 0)
    assert _count_rows_matching(library_db, "SELECT COUNT(*) FROM scan_batches WHERE status = 'completed'") == 0
    assert _count_rows(library_db, "face_observations") == 0
    assert _count_rows(embedding_db, "face_embeddings") == 0
    scan_events = _read_jsonl(external_root / "logs" / "scan.log.jsonl")
    assert any(event["event"] == "scan_failed" for event in scan_events)
    assert any("embedding 维度错误" in str(event.get("reason", "")) for event in scan_events if event.get("event") == "scan_failed")


def test_scan_start_marks_batch_and_session_failed_and_leaves_no_artifacts_when_main_process_commit_fails(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace-commit-failure"
    external_root = tmp_path / "external-root-commit-failure"
    init_result = _init_workspace(workspace, external_root)
    assert init_result.returncode == 0
    _prepare_workspace_models(workspace)
    add_result = _add_source(workspace, FIXTURE_DIR)
    assert add_result.returncode == 0
    spy_dir, spy_log_path = _prepare_faceanalysis_spy(tmp_path / "spy-commit-failure")

    result = _run_hikbox(
        "scan",
        "start",
        "--workspace",
        str(workspace),
        "--batch-size",
        "10",
        env_updates={
            "HIKBOX_TEST_FACEANALYSIS_SPY_LOG": str(spy_log_path),
            "HIKBOX_TEST_CORRUPT_WORKER_OUTPUT": "1",
        },
        pythonpath_prepend=[spy_dir],
    )

    assert result.returncode != 0
    assert "embedding 维度错误" in result.stderr
    library_db = workspace / ".hikbox" / "library.db"
    embedding_db = workspace / ".hikbox" / "embedding.db"
    assert _fetch_one(
        library_db,
        """
        SELECT status, completed_batches
        FROM scan_sessions
        ORDER BY id DESC
        LIMIT 1
        """,
    ) == ("failed", 0)
    failed_batch = _fetch_one(
        library_db,
        """
        SELECT status, failure_message
        FROM scan_batches
        WHERE batch_index = 1
        """,
    )
    assert failed_batch[0] == "failed"
    assert failed_batch[1]
    assert "embedding 维度错误" in str(failed_batch[1])
    assert _count_rows(library_db, "face_observations") == 0
    assert _count_rows(embedding_db, "face_embeddings") == 0
    assert list((external_root / "artifacts" / "crops").iterdir()) == []
    assert list((external_root / "artifacts" / "context").iterdir()) == []
    scan_events = _read_jsonl(external_root / "logs" / "scan.log.jsonl")
    assert any(event["event"] == "scan_failed" for event in scan_events)
    assert any("embedding 维度错误" in str(event.get("reason", "")) for event in scan_events if event.get("event") == "scan_failed")


def test_scan_start_rolls_back_partial_artifact_move_when_second_move_fails(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace-move-failure"
    external_root = tmp_path / "external-root-move-failure"
    init_result = _init_workspace(workspace, external_root)
    assert init_result.returncode == 0
    _prepare_workspace_models(workspace)
    add_result = _add_source(workspace, FIXTURE_DIR)
    assert add_result.returncode == 0
    spy_dir, spy_log_path = _prepare_faceanalysis_spy(tmp_path / "spy-move-failure")

    result = _run_hikbox(
        "scan",
        "start",
        "--workspace",
        str(workspace),
        "--batch-size",
        "10",
        env_updates={
            "HIKBOX_TEST_FACEANALYSIS_SPY_LOG": str(spy_log_path),
            "HIKBOX_TEST_FAIL_SECOND_ARTIFACT_MOVE": "1",
        },
        pythonpath_prepend=[spy_dir],
    )

    assert result.returncode != 0
    library_db = workspace / ".hikbox" / "library.db"
    embedding_db = workspace / ".hikbox" / "embedding.db"
    assert _fetch_one(
        library_db,
        """
        SELECT status, completed_batches
        FROM scan_sessions
        ORDER BY id DESC
        LIMIT 1
        """,
    ) == ("failed", 0)
    assert _fetch_one(
        library_db,
        """
        SELECT status
        FROM scan_batches
        WHERE batch_index = 1
        """,
    )[0] == "failed"
    assert _count_rows(library_db, "face_observations") == 0
    assert _count_rows(embedding_db, "face_embeddings") == 0
    assert list((external_root / "artifacts" / "crops").iterdir()) == []
    assert list((external_root / "artifacts" / "context").iterdir()) == []
    scan_events = _read_jsonl(external_root / "logs" / "scan.log.jsonl")
    assert any(event["event"] == "scan_failed" for event in scan_events)


def test_scan_start_failed_rescan_keeps_previously_committed_artifacts(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace-rescan-move-failure"
    external_root = tmp_path / "external-root-rescan-move-failure"
    init_result = _init_workspace(workspace, external_root)
    assert init_result.returncode == 0
    _prepare_workspace_models(workspace)
    add_result = _add_source(workspace, FIXTURE_DIR)
    assert add_result.returncode == 0

    first_result = _run_hikbox(
        "scan",
        "start",
        "--workspace",
        str(workspace),
        "--batch-size",
        "10",
    )
    assert first_result.returncode == 0, first_result.stderr

    library_db = workspace / ".hikbox" / "library.db"
    embedding_db = workspace / ".hikbox" / "embedding.db"
    original_crop_path, original_context_path = _fetch_one(
        library_db,
        """
        SELECT crop_path, context_path
        FROM face_observations
        ORDER BY id ASC
        LIMIT 1
        """,
    )
    original_crop = Path(str(original_crop_path))
    original_context = Path(str(original_context_path))
    assert original_crop.is_file()
    assert original_context.is_file()
    with Image.open(original_crop) as image:
        image.load()
    with Image.open(original_context) as image:
        image.load()
    crops_dir = external_root / "artifacts" / "crops"
    context_dir = external_root / "artifacts" / "context"
    crop_names_before = {path.name for path in crops_dir.iterdir()}
    context_names_before = {path.name for path in context_dir.iterdir()}
    face_count_before = _count_rows(library_db, "face_observations")
    embedding_count_before = _count_rows(embedding_db, "face_embeddings")

    spy_dir, spy_log_path = _prepare_faceanalysis_spy(tmp_path / "spy-rescan-move-failure")
    second_result = _run_hikbox(
        "scan",
        "start",
        "--workspace",
        str(workspace),
        "--batch-size",
        "7",
        env_updates={
            "HIKBOX_TEST_FACEANALYSIS_SPY_LOG": str(spy_log_path),
            "HIKBOX_TEST_FAIL_SECOND_ARTIFACT_MOVE": "1",
        },
        pythonpath_prepend=[spy_dir],
    )

    assert second_result.returncode != 0
    assert _fetch_one(
        library_db,
        """
        SELECT status
        FROM scan_sessions
        ORDER BY id DESC
        LIMIT 1
        """,
    )[0] == "failed"
    assert _fetch_one(
        library_db,
        """
        SELECT status
        FROM scan_batches
        WHERE session_id = (
          SELECT id FROM scan_sessions ORDER BY id DESC LIMIT 1
        )
        ORDER BY batch_index ASC
        LIMIT 1
        """,
    )[0] == "failed"
    assert _count_rows(library_db, "face_observations") == face_count_before
    assert _count_rows(embedding_db, "face_embeddings") == embedding_count_before
    assert original_crop.is_file()
    assert original_context.is_file()
    with Image.open(original_crop) as image:
        image.load()
    with Image.open(original_context) as image:
        image.load()
    assert {path.name for path in crops_dir.iterdir()} == crop_names_before
    assert {path.name for path in context_dir.iterdir()} == context_names_before
    scan_events = _read_jsonl(external_root / "logs" / "scan.log.jsonl")
    assert any(event["event"] == "scan_failed" for event in scan_events)


def test_scan_start_keeps_committed_new_artifacts_when_old_cleanup_fails_after_commit(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace-old-cleanup-failure"
    external_root = tmp_path / "external-root-old-cleanup-failure"
    init_result = _init_workspace(workspace, external_root)
    assert init_result.returncode == 0
    _prepare_workspace_models(workspace)
    add_result = _add_source(workspace, FIXTURE_DIR)
    assert add_result.returncode == 0

    first_result = _run_hikbox(
        "scan",
        "start",
        "--workspace",
        str(workspace),
        "--batch-size",
        "10",
    )
    assert first_result.returncode == 0, first_result.stderr

    library_db = workspace / ".hikbox" / "library.db"
    original_crop_path, original_context_path = _fetch_one(
        library_db,
        """
        SELECT crop_path, context_path
        FROM face_observations
        ORDER BY id ASC
        LIMIT 1
        """,
    )
    original_crop = Path(str(original_crop_path))
    original_context = Path(str(original_context_path))
    assert original_crop.is_file()
    assert original_context.is_file()

    spy_dir, spy_log_path = _prepare_faceanalysis_spy(tmp_path / "spy-old-cleanup-failure")
    second_result = _run_hikbox(
        "scan",
        "start",
        "--workspace",
        str(workspace),
        "--batch-size",
        "7",
        env_updates={
            "HIKBOX_TEST_FACEANALYSIS_SPY_LOG": str(spy_log_path),
            "HIKBOX_TEST_FAIL_OLD_ARTIFACT_CLEANUP": "1",
        },
        pythonpath_prepend=[spy_dir],
    )

    assert second_result.returncode == 0, second_result.stderr
    latest_session = _fetch_one(
        library_db,
        """
        SELECT id, status, completed_batches, total_batches
        FROM scan_sessions
        ORDER BY id DESC
        LIMIT 1
        """,
    )
    assert latest_session[1:] == ("completed", latest_session[3], latest_session[3])
    new_crop_path, new_context_path = _fetch_one(
        library_db,
        """
        SELECT crop_path, context_path
        FROM face_observations
        ORDER BY id ASC
        LIMIT 1
        """,
    )
    new_crop = Path(str(new_crop_path))
    new_context = Path(str(new_context_path))
    assert new_crop != original_crop
    assert new_context != original_context
    assert new_crop.is_file()
    assert new_context.is_file()
    assert original_crop.is_file()
    assert original_context.is_file()
    with Image.open(new_crop) as image:
        image.load()
    with Image.open(new_context) as image:
        image.load()
    scan_events = _read_jsonl(external_root / "logs" / "scan.log.jsonl")
    assert any(
        event["event"] == "artifact_cleanup_warning"
        and event.get("session_id") == latest_session[0]
        for event in scan_events
    )
    assert not any(
        event["event"] == "scan_failed"
        and event.get("session_id") == latest_session[0]
        for event in scan_events
    )


def test_scan_start_is_idempotent_after_completed_scan(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    external_root = tmp_path / "external-root"
    init_result = _init_workspace(workspace, external_root)
    assert init_result.returncode == 0
    _prepare_workspace_models(workspace)
    add_result = _add_source(workspace, FIXTURE_DIR)
    assert add_result.returncode == 0

    first_result = _run_hikbox(
        "scan",
        "start",
        "--workspace",
        str(workspace),
        "--batch-size",
        "10",
    )
    assert first_result.returncode == 0, first_result.stderr
    library_db = workspace / ".hikbox" / "library.db"
    embedding_db = workspace / ".hikbox" / "embedding.db"
    before_counts = (
        _count_rows(library_db, "assets"),
        _count_rows(library_db, "face_observations"),
        _count_rows(embedding_db, "face_embeddings"),
        len(list((external_root / "artifacts" / "crops").iterdir())),
        len(list((external_root / "artifacts" / "context").iterdir())),
    )

    second_result = _run_hikbox(
        "scan",
        "start",
        "--workspace",
        str(workspace),
        "--batch-size",
        "10",
    )
    assert second_result.returncode == 0, second_result.stderr
    after_counts = (
        _count_rows(library_db, "assets"),
        _count_rows(library_db, "face_observations"),
        _count_rows(embedding_db, "face_embeddings"),
        len(list((external_root / "artifacts" / "crops").iterdir())),
        len(list((external_root / "artifacts" / "context").iterdir())),
    )
    assert after_counts == before_counts
    scan_log_text = (external_root / "logs" / "scan.log.jsonl").read_text(encoding="utf-8")
    assert "scan_skipped" in scan_log_text or "无新增待处理批次" in scan_log_text


def test_scan_start_refreshes_stale_running_session_when_all_batches_are_already_completed(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace-stale-session-summary"
    external_root = tmp_path / "external-root-stale-session-summary"
    init_result = _init_workspace(workspace, external_root)
    assert init_result.returncode == 0
    _prepare_workspace_models(workspace)
    add_result = _add_source(workspace, FIXTURE_DIR)
    assert add_result.returncode == 0

    first_result = _run_hikbox(
        "scan",
        "start",
        "--workspace",
        str(workspace),
        "--batch-size",
        "10",
    )
    assert first_result.returncode == 0, first_result.stderr

    library_db = workspace / ".hikbox" / "library.db"
    original_summary = _fetch_one(
        library_db,
        """
        SELECT id, status, total_batches, completed_batches, failed_assets, success_faces, artifact_files
        FROM scan_sessions
        ORDER BY id DESC
        LIMIT 1
        """,
    )
    session_id = int(original_summary[0])
    total_batches = int(original_summary[2])
    conn = sqlite3.connect(library_db)
    try:
        with conn:
            conn.execute(
                """
                UPDATE scan_sessions
                SET status = 'running',
                    completed_batches = 0,
                    failed_assets = 0,
                    success_faces = 0,
                    artifact_files = 0,
                    completed_at = NULL
                WHERE id = ?
                """,
                (session_id,),
            )
    finally:
        conn.close()

    rerun_result = _run_hikbox(
        "scan",
        "start",
        "--workspace",
        str(workspace),
        "--batch-size",
        "10",
    )

    assert rerun_result.returncode == 0, rerun_result.stderr
    refreshed_summary = _fetch_one(
        library_db,
        """
        SELECT id, status, total_batches, completed_batches, failed_assets, success_faces, artifact_files
        FROM scan_sessions
        ORDER BY id DESC
        LIMIT 1
        """,
    )
    assert refreshed_summary[0] == session_id
    assert refreshed_summary[1] == "completed"
    assert refreshed_summary[2] == total_batches
    assert refreshed_summary[3] == total_batches
    assert refreshed_summary[4:] == original_summary[4:]
    assert _count_rows_matching(
        library_db,
        "SELECT COUNT(*) FROM scan_sessions WHERE status = 'running'",
    ) == 0
    scan_events = _read_jsonl(external_root / "logs" / "scan.log.jsonl")
    assert any(
        event["event"] == "scan_skipped" and event.get("session_id") == session_id
        for event in scan_events
    )


@pytest.mark.parametrize("stop_signal", [signal.SIGTERM, signal.SIGINT, signal.SIGKILL])
def test_scan_start_recovers_from_killed_process_without_rerunning_completed_batches(
    tmp_path: Path,
    stop_signal: signal.Signals,
) -> None:
    workspace = tmp_path / f"workspace-{stop_signal.name.lower()}"
    external_root = tmp_path / f"external-root-{stop_signal.name.lower()}"
    init_result = _init_workspace(workspace, external_root)
    assert init_result.returncode == 0
    _prepare_workspace_models(workspace)
    add_result = _add_source(workspace, FIXTURE_DIR)
    assert add_result.returncode == 0

    process = _spawn_hikbox(
        "scan",
        "start",
        "--workspace",
        str(workspace),
        "--batch-size",
        "10",
    )
    library_db = workspace / ".hikbox" / "library.db"
    try:
        _wait_for_batch_status(library_db, batch_index=2, expected_status="running")
        assert _fetch_one(
            library_db,
            "SELECT status FROM scan_batches WHERE batch_index = 1",
        )[0] == "completed"
        process.send_signal(stop_signal)
        stdout_text, stderr_text = process.communicate(timeout=30)
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=30)

    assert process.returncode != 0, (stdout_text, stderr_text)
    assert _fetch_one(
        library_db,
        "SELECT status FROM scan_batches WHERE batch_index = 1",
    )[0] == "completed"
    assert _fetch_one(
        library_db,
        "SELECT status FROM scan_batches WHERE batch_index = 2",
    )[0] != "completed"

    rerun_result = _run_hikbox(
        "scan",
        "start",
        "--workspace",
        str(workspace),
        "--batch-size",
        "10",
    )
    assert rerun_result.returncode == 0, rerun_result.stderr
    assert _fetch_one(
        library_db,
        """
        SELECT total_batches, completed_batches
        FROM scan_sessions
        ORDER BY id DESC
        LIMIT 1
        """,
    ) == (6, 6)
    assert _count_rows(workspace / ".hikbox" / "embedding.db", "face_embeddings") == _count_rows(
        library_db,
        "face_observations",
    )
    batch_completed_events = _count_batch_completed_events(
        external_root / "logs" / "scan.log.jsonl",
        batch_index=1,
    )
    assert batch_completed_events == 1


def test_scan_start_recovers_killed_batch_and_downgrades_missing_file_to_asset_failure(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace-missing-file-recovery"
    external_root = tmp_path / "external-root-missing-file-recovery"
    source_dir = tmp_path / "source-missing-file-recovery"
    _write_named_source_copies(
        source_dir,
        [f"photo_{index:02d}.jpg" for index in range(1, 13)],
    )

    init_result = _init_workspace(workspace, external_root)
    assert init_result.returncode == 0
    _prepare_workspace_models(workspace)
    add_result = _add_source(workspace, source_dir)
    assert add_result.returncode == 0

    process = _spawn_hikbox(
        "scan",
        "start",
        "--workspace",
        str(workspace),
        "--batch-size",
        "10",
    )
    library_db = workspace / ".hikbox" / "library.db"
    deleted_path = (source_dir / "photo_11.jpg").resolve()
    try:
        _wait_for_batch_status(library_db, batch_index=2, expected_status="running")
        deleted_path.unlink()
        process.send_signal(signal.SIGKILL)
        stdout_text, stderr_text = process.communicate(timeout=30)
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=30)

    assert process.returncode != 0, (stdout_text, stderr_text)
    rerun_result = _run_hikbox(
        "scan",
        "start",
        "--workspace",
        str(workspace),
        "--batch-size",
        "10",
    )

    assert rerun_result.returncode == 0, rerun_result.stderr
    assert "Traceback" not in rerun_result.stderr
    embedding_db = workspace / ".hikbox" / "embedding.db"
    assert _fetch_one(
        library_db,
        """
        SELECT status, completed_batches, failed_assets
        FROM scan_sessions
        ORDER BY id DESC
        LIMIT 1
        """,
    ) == ("completed", 2, 1)
    failed_asset = _fetch_one(
        library_db,
        """
        SELECT processing_status, failure_reason
        FROM assets
        WHERE absolute_path = ?
        """,
        (str(deleted_path),),
    )
    assert failed_asset[0] == "failed"
    assert failed_asset[1]
    assert _fetch_one(
        library_db,
        """
        SELECT status, failure_reason
        FROM scan_batch_items
        WHERE absolute_path = ?
        """,
        (str(deleted_path),),
    )[0] == "failed"
    assert _count_rows_matching(
        library_db,
        "SELECT COUNT(*) FROM assets WHERE processing_status = 'succeeded'",
    ) == 11
    assert _count_rows(library_db, "face_observations") > 0
    assert _count_rows(embedding_db, "face_embeddings") == _count_rows(library_db, "face_observations")
    scan_events = _read_jsonl(external_root / "logs" / "scan.log.jsonl")
    assert any(
        event["event"] == "asset_failed" and event.get("asset_path") == str(deleted_path)
        for event in scan_events
    )


def _count_rows(db_path: Path, table_name: str) -> int:
    return _count_rows_matching(db_path, f"SELECT COUNT(*) FROM {table_name}")


def _create_slice_a_only_workspace(workspace: Path, external_root: Path, source_dir: Path) -> None:
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


def _count_rows_matching(
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


def _fetch_one(db_path: Path, sql: str, params: tuple[object, ...] = ()) -> tuple[object, ...]:
    return _fetch_all(db_path, sql, params)[0]


def _fetch_all(db_path: Path, sql: str, params: tuple[object, ...] = ()) -> list[tuple[object, ...]]:
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(sql, params).fetchall()
    finally:
        conn.close()
    assert rows
    return [tuple(row) for row in rows]


def _wait_for_batch_status(db_path: Path, *, batch_index: int, expected_status: str) -> None:
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


def _count_batch_completed_events(log_path: Path, *, batch_index: int) -> int:
    count = 0
    with log_path.open("r", encoding="utf-8") as handle:
        for line in handle:
            payload = json.loads(line)
            if payload.get("event") == "batch_completed" and payload.get("batch_index") == batch_index:
                count += 1
    return count


def _prepare_faceanalysis_spy(root_dir: Path) -> tuple[Path, Path]:
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


def _read_jsonl(path: Path) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            records.append(json.loads(line))
    return records


def _normalized_stderr(stderr_text: str) -> str:
    lines = [
        line
        for line in stderr_text.splitlines()
        if line.strip() != "Matplotlib is building the font cache; this may take a moment."
        and not line.strip().startswith("scan 进度:")
    ]
    return "\n".join(lines).strip()


def _scan_progress_lines(stderr_text: str) -> list[str]:
    return [line.strip() for line in stderr_text.splitlines() if line.strip().startswith("scan 进度:")]


def _write_named_source_copies(source_dir: Path, file_names: list[str]) -> None:
    source_dir.mkdir(parents=True, exist_ok=True)
    sample_bytes = (FIXTURE_DIR / "pg_001_single_alex_01.jpg").read_bytes()
    for file_name in file_names:
        (source_dir / file_name).write_bytes(sample_bytes)
