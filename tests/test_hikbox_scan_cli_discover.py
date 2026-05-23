from __future__ import annotations

import io
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import time
import uuid

import pytest

import hikbox_pictures.product.scan as scan_module
import hikbox_pictures.product.scan_worker as scan_worker_module
from tests.helpers import (
    FIXTURE_DIR,
    add_source,
    init_workspace,
    load_manifest,
    run_hikbox,
)
from tests.scan_cli_helpers import (
    SUPPORTED_SCAN_SUFFIXES,
    count_rows,
    create_scan_batches_for_paths,
    fetch_one,
    scan_progress_lines,
    write_named_source_copies,
)


LIVE_PHOTO_FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "live-photo"


def _read_xattr(path: Path, name: str) -> bytes:
    result = subprocess.run(
        ["xattr", "-px", name, str(path)],
        check=True,
        stdout=subprocess.PIPE,
        text=True,
    )
    return bytes.fromhex(result.stdout)


def _content_identifier(path: Path) -> str:
    result = subprocess.run(
        ["exiftool", "-s3", "-ContentIdentifier", str(path)],
        check=True,
        stdout=subprocess.PIPE,
        text=True,
    )
    return result.stdout.strip()


def _patch_live_photo_capture_days(
    monkeypatch: pytest.MonkeyPatch,
    values: dict[str, str | None],
) -> None:
    monkeypatch.setattr(
        scan_module,
        "capture_day_for_live_photo",
        lambda path: values.get(Path(path).name),
        raising=False,
    )


def _write_xattr(path: Path, name: str, value: bytes) -> None:
    setxattr = getattr(os, "setxattr", None)
    if setxattr is not None:
        try:
            setxattr(path, name, value)
            return
        except OSError:
            pass
    if shutil.which("xattr") is None:
        pytest.skip("当前环境不支持写 xattr")
    try:
        subprocess.run(
            ["xattr", "-wx", name, value.hex(), str(path)],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        pytest.skip(f"当前环境不支持写 xattr: {exc}")


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
                        "item_index": 1,
                    },
                    {
                        "absolute_path": str((tmp_path / "photo-2.jpg").resolve()),
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
    write_named_source_copies(source_dir, ["photo_01.jpg", "photo_02.jpg", "photo_03.jpg"])

    init_result = init_workspace(workspace, external_root)
    assert init_result.returncode == 0
    add_result = add_source(workspace, source_dir)
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

    progress = scan_progress_lines(capsys.readouterr().err)
    assert "scan 进度: 阶段=批处理，批次 0/2，照片 1/3" in progress
    assert "scan 进度: 阶段=在线归属，批次 2/2，照片 3/3" in progress


def test_scan_start_records_jpg_live_photo_match_when_worker_detects_no_faces(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace-jpg-live-no-face"
    external_root = tmp_path / "external-root-jpg-live-no-face"
    source_dir = tmp_path / "source-jpg-live-no-face"
    source_dir.mkdir()
    image_path = source_dir / "IMG_3910.JPG"
    mov_path = source_dir / ".IMG_3910_1750508738977975.MOV"
    image_path.write_bytes(b"not real jpg")
    mov_path.write_bytes(b"mov")
    _patch_live_photo_capture_days(
        monkeypatch,
        {
            "IMG_3910.JPG": "2025-06-21",
            ".IMG_3910_1750508738977975.MOV": "2025-06-21",
        },
    )

    init_result = init_workspace(workspace, external_root)
    assert init_result.returncode == 0
    add_result = add_source(workspace, source_dir)
    assert add_result.returncode == 0

    def _fake_run_scan_worker(**kwargs) -> dict[str, object]:
        payload = json.loads(Path(kwargs["input_path"]).read_text(encoding="utf-8"))
        return {
            "model_root": str(payload["model_root"]),
            "processed_at": "2026-05-12T00:00:00Z",
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
        }

    monkeypatch.setattr(scan_module, "_run_scan_worker", _fake_run_scan_worker)

    scan_module.start_scan(
        workspace=workspace,
        batch_size=10,
        command_args=["scan", "start", "--workspace", str(workspace), "--batch-size", "10"],
    )

    library_db = workspace / ".hikbox" / "library.db"
    assert fetch_one(
        library_db,
        """
        SELECT live_photo_mov_path, processing_status
        FROM assets
        WHERE file_name = 'IMG_3910.JPG'
        """,
    ) == (str(mov_path.resolve()), "succeeded")
    assert count_rows(library_db, "face_observations") == 0


def test_discover_candidates_progress_shows_ready_and_total(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    fixture_path = FIXTURE_DIR / "pg_001_single_alex_01.jpg"
    for i in range(5):
        shutil.copy(fixture_path, source_dir / f"img_{i:02d}.jpg")

    monkeypatch.setattr(scan_module, "_SCAN_PROGRESS_INTERVAL_SECONDS", 0.01, raising=False)

    _original_capture_month = scan_module.compute_capture_month

    def _slow_capture_month(path: Path) -> str:
        time.sleep(0.02)
        return _original_capture_month(path)

    monkeypatch.setattr(scan_module, "compute_capture_month", _slow_capture_month)

    from hikbox_pictures.product.sources import WorkspaceContext

    workspace_context = WorkspaceContext(
        workspace_path=tmp_path,
        external_root_path=tmp_path,
        library_db_path=tmp_path / "library.db",
        embedding_db_path=tmp_path / "embedding.db",
        model_root_path=tmp_path,
    )

    active_sources = [
        {"id": 1, "path": str(source_dir), "label": "test", "scan_state": "pending"}
    ]

    scan_module._discover_candidates(active_sources, workspace_context)

    progress = scan_progress_lines(capsys.readouterr().err)
    candidate_lines = [line for line in progress if "阶段=候选发现" in line]

    assert len(candidate_lines) > 0
    lines_with_total = [line for line in candidate_lines if "/5" in line]
    assert len(lines_with_total) > 0
    for line in candidate_lines:
        if "已准备好" in line:
            assert "/5" in line


def test_discover_candidates_does_not_compute_file_fingerprint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    shutil.copy(FIXTURE_DIR / "pg_001_single_alex_01.jpg", source_dir / "img_01.jpg")

    def _unexpected_fingerprint(_path: Path) -> str:
        raise AssertionError("discover 不应计算文件指纹")

    monkeypatch.setattr(scan_module, "compute_file_fingerprint", _unexpected_fingerprint, raising=False)

    from hikbox_pictures.product.sources import WorkspaceContext

    workspace_context = WorkspaceContext(
        workspace_path=tmp_path,
        external_root_path=tmp_path,
        library_db_path=tmp_path / "library.db",
        embedding_db_path=tmp_path / "embedding.db",
        model_root_path=tmp_path,
    )

    candidates = scan_module._discover_candidates(
        [{"id": 1, "path": str(source_dir), "label": "test", "scan_state": "pending"}],
        workspace_context,
    )

    assert len(candidates) == 1
    assert "file_fingerprint" not in candidates[0]


def test_discover_candidates_defers_photo_metadata_until_batch_load(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    shutil.copy(FIXTURE_DIR / "pg_001_single_alex_01.jpg", source_dir / "img_01.jpg")
    shutil.copy(FIXTURE_DIR / "pg_001_single_alex_01.jpg", source_dir / "img_02.heic")

    def _unexpected_capture_month(_path: Path) -> str:
        raise AssertionError("discover 不应读取图片 EXIF")

    def _unexpected_live_photo_mov(_path: Path) -> str | None:
        raise AssertionError("discover 不应查找 Live Photo MOV")

    monkeypatch.setattr(scan_module, "compute_capture_month", _unexpected_capture_month)
    monkeypatch.setattr(scan_module, "find_live_photo_mov", _unexpected_live_photo_mov)

    from hikbox_pictures.product.sources import WorkspaceContext

    workspace_context = WorkspaceContext(
        workspace_path=tmp_path,
        external_root_path=tmp_path,
        library_db_path=tmp_path / "library.db",
        embedding_db_path=tmp_path / "embedding.db",
        model_root_path=tmp_path,
    )

    candidates = scan_module._discover_candidates(
        [{"id": 1, "path": str(source_dir), "label": "test", "scan_state": "pending"}],
        workspace_context,
    )

    assert [candidate["file_name"] for candidate in candidates] == ["img_01.jpg", "img_02.heic"]
    assert all("capture_month" not in candidate for candidate in candidates)
    assert all("live_photo_mov_path" not in candidate for candidate in candidates)


def test_discover_candidates_converts_jpg_mp4_pair_to_live_photo_in_place(
    tmp_path: Path,
) -> None:
    if sys.platform != "darwin":
        pytest.skip("JPG/MP4 Live Photo 转换只在 macOS 上运行")
    if shutil.which("swift") is None or shutil.which("exiftool") is None:
        pytest.skip("缺少 swift 或 exiftool")

    source_dir = tmp_path / "source"
    source_dir.mkdir()
    still_path = source_dir / "CaseMix.JPG"
    mp4_path = source_dir / "CaseMix.MP4"
    shutil.copy(LIVE_PHOTO_FIXTURE_DIR / "input.jpg", still_path)
    shutil.copy(LIVE_PHOTO_FIXTURE_DIR / "input.mp4", mp4_path)

    from hikbox_pictures.product.sources import WorkspaceContext

    workspace_context = WorkspaceContext(
        workspace_path=tmp_path,
        external_root_path=tmp_path,
        library_db_path=tmp_path / "library.db",
        embedding_db_path=tmp_path / "embedding.db",
        model_root_path=tmp_path,
    )

    candidates = scan_module._discover_candidates(
        [{"id": 1, "path": str(source_dir), "label": "test", "scan_state": "pending"}],
        workspace_context,
    )

    mov_path = source_dir / ".CaseMix.MOV"
    assert [candidate["file_name"] for candidate in candidates] == ["CaseMix.JPG"]
    assert still_path.is_file()
    assert mov_path.is_file()
    assert not mp4_path.exists()
    assert scan_module._recoverable_live_photo_mov(still_path) == str(mov_path.resolve())
    still_content_identifier = _content_identifier(still_path)
    mov_content_identifier = _content_identifier(mov_path)
    parsed = uuid.UUID(still_content_identifier)
    assert still_content_identifier == mov_content_identifier
    assert parsed.variant == uuid.RFC_4122
    assert _read_xattr(still_path, "livephoto") == b".CaseMix.MOV\0"
    assert _read_xattr(still_path, "fileattr") == b"\x10\x00\x00\x00"
    assert _read_xattr(mov_path, "fileattr") == b"\x01\x00\x00\x00"


def test_discover_candidates_does_not_resolve_each_candidate_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    for index in range(3):
        shutil.copy(FIXTURE_DIR / "pg_001_single_alex_01.jpg", source_dir / f"img_{index:02d}.jpg")

    original_resolve = Path.resolve
    candidate_resolve_count = 0

    def _count_candidate_resolve(path: Path, *args, **kwargs):
        nonlocal candidate_resolve_count
        if path.parent == source_dir and path.suffix.lower() in SUPPORTED_SCAN_SUFFIXES:
            candidate_resolve_count += 1
        return original_resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", _count_candidate_resolve)

    from hikbox_pictures.product.sources import WorkspaceContext

    workspace_context = WorkspaceContext(
        workspace_path=tmp_path,
        external_root_path=tmp_path,
        library_db_path=tmp_path / "library.db",
        embedding_db_path=tmp_path / "embedding.db",
        model_root_path=tmp_path,
    )

    candidates = scan_module._discover_candidates(
        [{"id": 1, "path": str(source_dir), "label": "test", "scan_state": "pending"}],
        workspace_context,
    )

    assert candidate_resolve_count == 0
    assert len(candidates) == 3


def test_load_batch_candidates_does_not_compute_file_fingerprint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    image_path = source_dir / "img_01.jpg"
    shutil.copy(FIXTURE_DIR / "pg_001_single_alex_01.jpg", image_path)
    library_db, batch_ids = create_scan_batches_for_paths(
        tmp_path=tmp_path,
        source_dir=source_dir,
        batches=[[image_path]],
    )

    def _unexpected_fingerprint(_path: Path) -> str:
        raise AssertionError("batch 候选加载不应计算文件指纹")

    monkeypatch.setattr(scan_module, "compute_file_fingerprint", _unexpected_fingerprint, raising=False)

    from hikbox_pictures.product.sources import WorkspaceContext

    candidates = scan_module._load_batch_candidates(
        WorkspaceContext(
            workspace_path=tmp_path,
            external_root_path=tmp_path,
            library_db_path=library_db,
            embedding_db_path=tmp_path / "embedding.db",
            model_root_path=tmp_path,
        ),
        batch_id=batch_ids[0],
    )

    assert len(candidates) == 1
    assert candidates[0]["artifact_token"] == "item000001"
    assert "file_fingerprint" not in candidates[0]


def test_load_batch_candidates_indexes_live_photo_mov_once_per_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    image_paths = [source_dir / f"IMG_{index:04d}.heic" for index in range(3)]
    for image_path in image_paths:
        image_path.write_bytes(b"not real heic")
    (source_dir / ".IMG_0001.MOV").write_bytes(b"mov")
    _patch_live_photo_capture_days(
        monkeypatch,
        {
            "IMG_0000.heic": "2025-01-01",
            "IMG_0001.heic": "2025-01-01",
            ".IMG_0001.MOV": "2025-01-01",
            "IMG_0002.heic": "2025-01-01",
        },
    )

    library_db, batch_ids = create_scan_batches_for_paths(
        tmp_path=tmp_path,
        source_dir=source_dir,
        batches=[image_paths],
    )

    monkeypatch.setattr(scan_module, "compute_capture_month", lambda _path: "2025-01")

    original_iterdir = Path.iterdir
    source_iterdir_count = 0

    def _count_source_iterdir(path: Path):
        nonlocal source_iterdir_count
        if path == source_dir:
            source_iterdir_count += 1
        return original_iterdir(path)

    monkeypatch.setattr(Path, "iterdir", _count_source_iterdir)

    from hikbox_pictures.product.sources import WorkspaceContext

    candidates = scan_module._load_batch_candidates(
        WorkspaceContext(
            workspace_path=tmp_path,
            external_root_path=tmp_path,
            library_db_path=library_db,
            embedding_db_path=tmp_path / "embedding.db",
            model_root_path=tmp_path,
        ),
        batch_id=batch_ids[0],
    )

    assert source_iterdir_count == 1
    assert candidates[0]["live_photo_mov_path"] is None
    assert candidates[1]["live_photo_mov_path"] == str((source_dir / ".IMG_0001.MOV").resolve())
    assert candidates[2]["live_photo_mov_path"] is None


def test_load_batch_candidates_rejects_same_stem_mov_from_different_capture_day(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    image_path = source_dir / "IMG_4721.heic"
    image_path.write_bytes(b"not real heic")
    mov_path = source_dir / ".IMG_4721_1744532070331526.MOV"
    mov_path.write_bytes(b"mov")
    _write_xattr(image_path, "livephoto", b".IMG_4721_1744532070331526.MOV\0")
    _patch_live_photo_capture_days(
        monkeypatch,
        {
            "IMG_4721.heic": "2022-12-31",
            ".IMG_4721_1744532070331526.MOV": "2024-05-02",
        },
    )

    library_db, batch_ids = create_scan_batches_for_paths(
        tmp_path=tmp_path,
        source_dir=source_dir,
        batches=[[image_path]],
    )

    monkeypatch.setattr(scan_module, "compute_capture_month", lambda _path: "2022-12")

    from hikbox_pictures.product.sources import WorkspaceContext

    candidates = scan_module._load_batch_candidates(
        WorkspaceContext(
            workspace_path=tmp_path,
            external_root_path=tmp_path,
            library_db_path=library_db,
            embedding_db_path=tmp_path / "embedding.db",
            model_root_path=tmp_path,
        ),
        batch_id=batch_ids[0],
    )

    assert candidates[0]["live_photo_mov_path"] is None


def test_load_batch_candidates_matches_jpg_mov_on_same_capture_day(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    image_path = source_dir / "IMG_0001.JPG"
    image_path.write_bytes(b"not real jpg")
    mov_path = source_dir / ".IMG_0001.MOV"
    mov_path.write_bytes(b"mov")
    _patch_live_photo_capture_days(
        monkeypatch,
        {
            "IMG_0001.JPG": "2025-05-17",
            ".IMG_0001.MOV": "2025-05-17",
        },
    )

    library_db, batch_ids = create_scan_batches_for_paths(
        tmp_path=tmp_path,
        source_dir=source_dir,
        batches=[[image_path]],
    )

    monkeypatch.setattr(scan_module, "compute_capture_month", lambda _path: "2025-05")

    from hikbox_pictures.product.sources import WorkspaceContext

    candidates = scan_module._load_batch_candidates(
        WorkspaceContext(
            workspace_path=tmp_path,
            external_root_path=tmp_path,
            library_db_path=library_db,
            embedding_db_path=tmp_path / "embedding.db",
            model_root_path=tmp_path,
        ),
        batch_id=batch_ids[0],
    )

    assert candidates[0]["live_photo_mov_path"] == str(mov_path.resolve())


def test_load_batch_candidates_reuses_live_photo_mov_index_across_batches(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    first_image = source_dir / "IMG_0001.heic"
    second_image = source_dir / "IMG_0002.heic"
    first_image.write_bytes(b"not real heic")
    second_image.write_bytes(b"not real heic")
    (source_dir / ".IMG_0001.MOV").write_bytes(b"mov")
    (source_dir / ".IMG_0002.mov").write_bytes(b"mov")
    _patch_live_photo_capture_days(
        monkeypatch,
        {
            "IMG_0001.heic": "2025-01-01",
            ".IMG_0001.MOV": "2025-01-01",
            "IMG_0002.heic": "2025-01-02",
            ".IMG_0002.mov": "2025-01-02",
        },
    )

    library_db, batch_ids = create_scan_batches_for_paths(
        tmp_path=tmp_path,
        source_dir=source_dir,
        batches=[[first_image], [second_image]],
    )

    monkeypatch.setattr(scan_module, "compute_capture_month", lambda _path: "2025-01")

    original_iterdir = Path.iterdir
    source_iterdir_count = 0

    def _count_source_iterdir(path: Path):
        nonlocal source_iterdir_count
        if path == source_dir:
            source_iterdir_count += 1
        return original_iterdir(path)

    monkeypatch.setattr(Path, "iterdir", _count_source_iterdir)

    from hikbox_pictures.product.sources import WorkspaceContext

    workspace_context = WorkspaceContext(
        workspace_path=tmp_path,
        external_root_path=tmp_path,
        library_db_path=library_db,
        embedding_db_path=tmp_path / "embedding.db",
        model_root_path=tmp_path,
    )
    live_photo_mov_resolver = scan_module._LivePhotoMovResolver()

    first_candidates = scan_module._load_batch_candidates(
        workspace_context,
        batch_id=batch_ids[0],
        live_photo_mov_resolver=live_photo_mov_resolver,
    )
    second_candidates = scan_module._load_batch_candidates(
        workspace_context,
        batch_id=batch_ids[1],
        live_photo_mov_resolver=live_photo_mov_resolver,
    )

    assert source_iterdir_count == 1
    assert first_candidates[0]["live_photo_mov_path"] == str((source_dir / ".IMG_0001.MOV").resolve())
    assert second_candidates[0]["live_photo_mov_path"] == str((source_dir / ".IMG_0002.mov").resolve())
