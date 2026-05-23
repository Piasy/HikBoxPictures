from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess

import numpy as np
from PIL import Image
import pytest

from tests.helpers import (
    REPO_ROOT,
    FIXTURE_DIR,
    add_source,
    fetch_all,
    init_workspace,
    load_manifest,
    run_hikbox,
)
from tests.scan_cli_helpers import (
    SUPPORTED_SCAN_SUFFIXES,
    count_rows,
    count_rows_matching,
    fetch_one,
    normalized_stderr,
    prepare_faceanalysis_spy,
    read_jsonl,
)


def _copy_fixture_with_live_photo_xattrs(tmp_path: Path) -> Path:
    source_dir = tmp_path / "people_gallery_scan"
    shutil.copytree(FIXTURE_DIR, source_dir)
    _write_xattr(
        source_dir / "pg_047_live_positive_01.HEIC",
        "livephoto",
        b".pg_047_live_positive_01.MOV\0",
    )
    _write_xattr(
        source_dir / "pg_048_live_positive_02.heif",
        "livephoto",
        b".pg_048_live_positive_02.mov\0",
    )
    return source_dir


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


def test_scan_start_runs_fixture_pipeline_and_persists_outputs(tmp_path: Path) -> None:
    manifest = load_manifest()
    scan_candidate_assets = [
        asset for asset in manifest["assets"] if Path(asset["file"]).suffix.lower() in SUPPORTED_SCAN_SUFFIXES
    ]
    workspace = tmp_path / "workspace"
    external_root = tmp_path / "external-root"
    source_dir = _copy_fixture_with_live_photo_xattrs(tmp_path)

    init_result = init_workspace(workspace, external_root)
    assert init_result.returncode == 0

    home_without_models = tmp_path / "home-without-insightface"
    home_without_models.mkdir()
    spy_dir, spy_log_path = prepare_faceanalysis_spy(tmp_path / "spy-success")

    add_result = add_source(workspace, source_dir)
    assert add_result.returncode == 0

    result = run_hikbox(
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
    assert normalized_stderr(result.stderr) == ""

    library_db = workspace / ".hikbox" / "library.db"
    embedding_db = workspace / ".hikbox" / "embedding.db"
    crops_dir = external_root / "artifacts" / "crops"
    context_dir = external_root / "artifacts" / "context"
    logs_dir = external_root / "logs"

    assert count_rows(library_db, "scan_sessions") == 1
    assert count_rows(library_db, "scan_batches") == 6
    assert count_rows(library_db, "scan_batch_items") == len(scan_candidate_assets)
    assert count_rows(library_db, "assets") == len(scan_candidate_assets)
    assert count_rows(library_db, "face_observations") > 0
    assert count_rows(embedding_db, "face_embeddings") == count_rows(library_db, "face_observations")
    assert any(crops_dir.iterdir())
    assert any(context_dir.iterdir())
    assert any(logs_dir.iterdir())

    scan_summary = fetch_one(
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
    assert count_rows_matching(
        embedding_db,
        "SELECT COUNT(*) FROM face_embeddings WHERE variant != 'main'",
    ) == 0

    embedding_dimension, embedding_norm = fetch_one(
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
    orphan_embeddings = count_rows_matching(
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

    positive_pairs = fetch_one(
        library_db,
        """
        SELECT COUNT(*)
        FROM assets
        WHERE live_photo_mov_path IS NOT NULL
        """,
    )[0]
    assert positive_pairs == 2
    negative_pairs = fetch_one(
        library_db,
        """
        SELECT COUNT(*)
        FROM assets
        WHERE file_name IN ('pg_049_live_negative_01.jpg', 'pg_050_live_negative_02.png')
          AND live_photo_mov_path IS NOT NULL
        """,
    )[0]
    assert negative_pairs == 0
    positive_pair_rows = fetch_all(
        library_db,
        """
        SELECT file_name, live_photo_mov_path
        FROM assets
        WHERE file_name IN ('pg_047_live_positive_01.HEIC', 'pg_048_live_positive_02.heif')
        ORDER BY file_name ASC
        """,
    )
    assert positive_pair_rows == [
        ("pg_047_live_positive_01.HEIC", str((source_dir / ".pg_047_live_positive_01.MOV").resolve())),
        ("pg_048_live_positive_02.heif", str((source_dir / ".pg_048_live_positive_02.mov").resolve())),
    ]
    corrupt_row = fetch_one(
        library_db,
        """
        SELECT processing_status, failure_reason
        FROM assets
        WHERE file_name = 'pg_902_corrupt.jpg'
        """,
    )
    assert corrupt_row[0] == "failed"
    assert corrupt_row[1]
    unsupported_count = count_rows_matching(
        library_db,
        "SELECT COUNT(*) FROM assets WHERE file_name = 'pg_901_unsupported.txt'",
    )
    assert unsupported_count == 0

    crop_path, context_path = fetch_one(
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
    assert str(REPO_ROOT / ".tmp" / "insightface_model") in scan_log_text
    assert str(home_without_models / ".insightface") not in scan_log_text
    spy_records = read_jsonl(spy_log_path)
    assert spy_records
    assert {record["event"] for record in spy_records} == {"faceanalysis_init"}
    assert {record["name"] for record in spy_records} == {"buffalo_l"}
    assert {record["root"] for record in spy_records} == {str(REPO_ROOT / ".tmp" / "insightface_model")}
