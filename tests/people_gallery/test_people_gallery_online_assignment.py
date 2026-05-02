from __future__ import annotations

from collections import Counter
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys

import numpy as np
import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures" / "people_gallery_scan"
MANIFEST_PATH = FIXTURE_DIR / "manifest.json"
SUPPORTED_SCAN_SUFFIXES = {".jpg", ".jpeg", ".png", ".heic", ".heif"}


def _run_hikbox(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    pythonpath_parts = [str(REPO_ROOT)]
    existing_pythonpath = env.get("PYTHONPATH")
    if existing_pythonpath:
        pythonpath_parts.append(existing_pythonpath)
    env["PYTHONPATH"] = os.pathsep.join(pythonpath_parts)
    return subprocess.run(
        [sys.executable, "-m", "hikbox_pictures", *args],
        cwd=cwd or REPO_ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
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


def _prepare_workspace_models(workspace: Path) -> None:
    source_root = _find_model_root()
    target_root = workspace / ".hikbox" / "models" / "insightface"
    if target_root.exists():
        shutil.rmtree(target_root)
    shutil.copytree(source_root, target_root)


def _find_model_root() -> Path:
    candidates = [REPO_ROOT / ".insightface", Path.home() / ".insightface"]
    candidates.extend(parent / ".insightface" for parent in REPO_ROOT.parents)
    for candidate in candidates:
        if (candidate / "models" / "buffalo_l" / "det_10g.onnx").exists():
            return candidate
    raise AssertionError("缺少 InsightFace buffalo_l 模型目录，无法执行在线人物归属真实集成测试")


def _fetch_all(db_path: Path, sql: str, params: tuple[object, ...] = ()) -> list[tuple[object, ...]]:
    connection = sqlite3.connect(db_path)
    try:
        return [tuple(row) for row in connection.execute(sql, params).fetchall()]
    finally:
        connection.close()


def _fetch_one(db_path: Path, sql: str, params: tuple[object, ...] = ()) -> tuple[object, ...]:
    rows = _fetch_all(db_path, sql, params)
    assert rows
    return rows[0]


def _count_rows_matching(db_path: Path, sql: str, params: tuple[object, ...] = ()) -> int:
    return int(_fetch_one(db_path, sql, params)[0])


def _read_jsonl(path: Path) -> list[dict[str, object]]:
    with path.open("r", encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def _copy_fixture_assets(target_dir: Path, file_names: list[str]) -> None:
    target_dir.mkdir(parents=True, exist_ok=True)
    for file_name in file_names:
        shutil.copy2(FIXTURE_DIR / file_name, target_dir / file_name)


def _asset_assignment_rows(library_db: Path) -> dict[str, list[tuple[int, str, str]]]:
    rows = _fetch_all(
        library_db,
        """
        SELECT
          assets.file_name,
          face_observations.face_index,
          person_face_assignments.person_id,
          person_face_assignments.assignment_source
        FROM person_face_assignments
        INNER JOIN face_observations
          ON face_observations.id = person_face_assignments.face_observation_id
        INNER JOIN assets
          ON assets.id = face_observations.asset_id
        WHERE person_face_assignments.active = 1
        ORDER BY assets.file_name ASC, face_observations.face_index ASC
        """,
    )
    result: dict[str, list[tuple[int, str, str]]] = {}
    for file_name, face_index, person_id, assignment_source in rows:
        result.setdefault(str(file_name), []).append((int(face_index), str(person_id), str(assignment_source)))
    return result


def _assert_target_group_assignment_rows(
    *,
    rows: list[tuple[int, str, str]],
    expected_person_ids: list[str],
) -> None:
    assert rows
    assert {source for _, _, source in rows} == {"online_v6"}
    assert len(rows) == len(expected_person_ids)
    face_indices = [face_index for face_index, _, _ in rows]
    assert len(set(face_indices)) == len(rows)
    assert Counter(person_id for _, person_id, _ in rows) == Counter(expected_person_ids)


def _expected_target_mapping(library_db: Path, manifest: dict[str, object]) -> dict[str, str]:
    assignment_rows = _asset_assignment_rows(library_db)
    mapping: dict[str, str] = {}
    for label in manifest["expected_person_groups"]:
        observed_person_ids: set[str] = set()
        observed_asset_files: list[str] = []
        for asset in manifest["assets"]:
            if asset["expected_target_people"] != [label]:
                continue
            file_name = str(asset["file"])
            assigned_rows = assignment_rows.get(file_name, [])
            if not assigned_rows:
                continue
            assert {source for _, _, source in assigned_rows} == {"online_v6"}
            assigned = {person_id for _, person_id, _ in assigned_rows}
            assert len(assigned) == 1, f"{label} 的单人目标照片出现多个 active person: {file_name} -> {sorted(assigned)}"
            observed_asset_files.append(file_name)
            observed_person_ids.update(assigned)
        assert observed_asset_files, f"{label} 缺少可用于建立人物映射的实际 target assignment"
        assert len(observed_person_ids) == 1, (
            f"{label} 的实际 target assignment 未稳定映射到唯一 person: {sorted(observed_person_ids)}"
        )
        mapping[str(label)] = next(iter(observed_person_ids))
    assert len(set(mapping.values())) == len(mapping)
    return mapping


def _deactivate_active_assignments(library_db: Path) -> None:
    connection = sqlite3.connect(library_db)
    try:
        with connection:
            connection.execute(
                """
                UPDATE person_face_assignments
                SET active = 0,
                    updated_at = '2026-04-25T00:00:00Z'
                WHERE active = 1
                """
            )
    finally:
        connection.close()


def _read_embedding_vector(embedding_db: Path, *, face_observation_id: int) -> np.ndarray:
    row = _fetch_one(
        embedding_db,
        """
        SELECT vector_blob
        FROM face_embeddings
        WHERE face_observation_id = ? AND variant = 'main'
        """,
        (face_observation_id,),
    )
    return np.frombuffer(row[0], dtype=np.float32).copy()


def _corrupt_candidate_embedding(
    *,
    embedding_db: Path,
    face_observation_id: int,
    mode: str,
) -> np.ndarray:
    original_vector = _read_embedding_vector(embedding_db, face_observation_id=face_observation_id)
    connection = sqlite3.connect(embedding_db)
    try:
        with connection:
            if mode == "missing":
                connection.execute(
                    """
                    DELETE FROM face_embeddings
                    WHERE face_observation_id = ? AND variant = 'main'
                    """,
                    (face_observation_id,),
                )
            elif mode == "wrong-dimension":
                connection.execute(
                    """
                    UPDATE face_embeddings
                    SET dimension = 128,
                        vector_blob = ?
                    WHERE face_observation_id = ? AND variant = 'main'
                    """,
                    (original_vector[:128].astype(np.float32).tobytes(), face_observation_id),
                )
            elif mode == "undecodable":
                connection.execute(
                    """
                    UPDATE face_embeddings
                    SET dimension = 512,
                        vector_blob = ?
                    WHERE face_observation_id = ? AND variant = 'main'
                    """,
                    (b"\x00", face_observation_id),
                )
            else:  # pragma: no cover
                raise AssertionError(f"未知损坏模式: {mode}")
    finally:
        connection.close()
    return original_vector


def _repair_candidate_embedding(
    *,
    embedding_db: Path,
    face_observation_id: int,
    original_vector: np.ndarray,
    mode: str,
) -> None:
    connection = sqlite3.connect(embedding_db)
    try:
        with connection:
            if mode == "missing":
                connection.execute(
                    """
                    INSERT INTO face_embeddings (
                      face_observation_id,
                      variant,
                      dimension,
                      l2_norm,
                      vector_blob,
                      created_at
                    )
                    VALUES (?, 'main', 512, 1.0, ?, '2026-04-25T00:00:00Z')
                    """,
                    (face_observation_id, original_vector.astype(np.float32).tobytes()),
                )
            else:
                connection.execute(
                    """
                    UPDATE face_embeddings
                    SET dimension = 512,
                        l2_norm = 1.0,
                        vector_blob = ?
                    WHERE face_observation_id = ? AND variant = 'main'
                    """,
                    (original_vector.astype(np.float32).tobytes(), face_observation_id),
                )
    finally:
        connection.close()


def test_target_group_assignment_helper_rejects_duplicate_person_when_target_count_matches() -> None:
    with pytest.raises(AssertionError):
        _assert_target_group_assignment_rows(
            rows=[
                (0, "person-alex", "online_v6"),
                (1, "person-blair", "online_v6"),
                (2, "person-blair", "online_v6"),
            ],
            expected_person_ids=["person-alex", "person-blair"],
        )


def test_scan_start_creates_expected_online_assignments_and_is_idempotent(
    scanned_workspace: tuple[Path, Path, Path, dict[str, object], dict[str, str]],
) -> None:
    workspace, external_root, library_db, manifest, mapping = scanned_workspace
    assignment_rows = _asset_assignment_rows(library_db)
    target_person_ids = set(mapping.values())

    for asset in manifest["assets"]:
        file_name = str(asset["file"])
        rows = assignment_rows.get(file_name, [])
        assigned_people = {person_id for _, person_id, _ in rows}
        expected_targets = [str(label) for label in asset["expected_target_people"]]
        if asset["category"] == "single_target":
            assert rows
            assert {source for _, _, source in rows} == {"online_v6"}
            assert len(rows) == 1
            assert assigned_people == {mapping[expected_targets[0]]}
        if asset["category"] == "target_group":
            _assert_target_group_assignment_rows(
                rows=rows,
                expected_person_ids=[mapping[label] for label in expected_targets],
            )
        if asset["category"] == "non_target_person":
            assert not (target_person_ids & assigned_people)
        if asset["tolerance"] and not expected_targets:
            assert not (target_person_ids & assigned_people)
        if asset["is_faceless"] or asset["is_corrupt"] or asset["is_unsupported_extension"]:
            assert assigned_people == set()

    run_row = _fetch_one(
        library_db,
        """
        SELECT
          status,
          algorithm_version,
          param_snapshot_json,
          candidate_count,
          assigned_count,
          new_person_count,
          deferred_count,
          skipped_count,
          failed_count
        FROM assignment_runs
        ORDER BY id DESC
        LIMIT 1
        """,
    )
    assert run_row[0] == "completed"
    assert run_row[1] == "immich_v6_online_v1"
    params = json.loads(str(run_row[2]))
    assert params == {
        "max_distance": 0.5,
        "min_faces": 3,
        "num_results": 3,
        "embedding_variant": "main",
        "distance_metric": "cosine_distance",
        "self_match_included": True,
        "two_pass_deferred": True,
    }
    assert int(run_row[3]) > 0
    assert int(run_row[4]) > 0
    assert int(run_row[5]) >= 3
    assert int(run_row[8]) == 0

    log_rows = _read_jsonl(external_root / "logs" / "scan.log.jsonl")
    assert any(item["event"] == "assignment_started" for item in log_rows)
    assert any(item["event"] == "assignment_completed" for item in log_rows)

    before_summary = (
        _count_rows_matching(library_db, "SELECT COUNT(*) FROM person WHERE status = 'active'"),
        _count_rows_matching(library_db, "SELECT COUNT(*) FROM person_face_assignments WHERE active = 1"),
        mapping,
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
    after_mapping = _expected_target_mapping(library_db, manifest)
    after_summary = (
        _count_rows_matching(library_db, "SELECT COUNT(*) FROM person WHERE status = 'active'"),
        _count_rows_matching(library_db, "SELECT COUNT(*) FROM person_face_assignments WHERE active = 1"),
        after_mapping,
    )
    assert after_summary == before_summary
    rerun = _fetch_one(
        library_db,
        """
        SELECT status, assigned_count, new_person_count, failed_count
        FROM assignment_runs
        ORDER BY id DESC
        LIMIT 1
        """,
    )
    assert rerun[0] == "completed"
    assert rerun[1] == 0
    assert rerun[2] == 0
    assert rerun[3] == 0
    rerun_logs = _read_jsonl(external_root / "logs" / "scan.log.jsonl")
    assert any(item["event"] == "assignment_skipped" for item in rerun_logs)


def test_scan_start_fails_assignment_for_corrupted_candidate_embedding_and_recovers(tmp_path: Path) -> None:
    source_dir = tmp_path / "source-corrupted-embedding"
    _copy_fixture_assets(
        source_dir,
        [
            "pg_001_single_alex_01.jpg",
            "pg_002_single_alex_02.jpg",
            "pg_003_single_alex_03.jpg",
        ],
    )
    workspace = tmp_path / "workspace-corrupted-embedding"
    external_root = tmp_path / "external-root-corrupted-embedding"

    init_result = _init_workspace(workspace, external_root)
    assert init_result.returncode == 0
    _prepare_workspace_models(workspace)
    add_result = _add_source(workspace, source_dir)
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
    face_count_before = _count_rows_matching(library_db, "SELECT COUNT(*) FROM face_observations")
    embedding_count_before = _count_rows_matching(embedding_db, "SELECT COUNT(*) FROM face_embeddings")
    face_observation_id = int(
        _fetch_one(
            library_db,
            """
            SELECT face_observations.id
            FROM face_observations
            ORDER BY face_observations.id ASC
            LIMIT 1
            """,
        )[0]
    )

    for mode, expected_reason in [
        ("missing", "缺少 main embedding"),
        ("wrong-dimension", "embedding 维度错误"),
        ("undecodable", "embedding 不可解码"),
    ]:
        _deactivate_active_assignments(library_db)
        person_count_before_failure = _count_rows_matching(
            library_db,
            "SELECT COUNT(*) FROM person WHERE status = 'active'",
        )
        original_vector = _corrupt_candidate_embedding(
            embedding_db=embedding_db,
            face_observation_id=face_observation_id,
            mode=mode,
        )

        failed_result = _run_hikbox(
            "scan",
            "start",
            "--workspace",
            str(workspace),
            "--batch-size",
            "10",
        )

        assert failed_result.returncode != 0
        assert expected_reason in failed_result.stderr
        assert _fetch_one(
            library_db,
            """
            SELECT status
            FROM scan_sessions
            ORDER BY id DESC
            LIMIT 1
            """,
        )[0] == "failed"
        failed_run = _fetch_one(
            library_db,
            """
            SELECT status, failure_reason
            FROM assignment_runs
            ORDER BY id DESC
            LIMIT 1
            """,
        )
        assert failed_run[0] == "failed"
        assert expected_reason in str(failed_run[1])
        assert _count_rows_matching(library_db, "SELECT COUNT(*) FROM person_face_assignments WHERE active = 1") == 0
        assert (
            _count_rows_matching(library_db, "SELECT COUNT(*) FROM person WHERE status = 'active'")
            == person_count_before_failure
        )
        assert _count_rows_matching(library_db, "SELECT COUNT(*) FROM face_observations") == face_count_before

        _repair_candidate_embedding(
            embedding_db=embedding_db,
            face_observation_id=face_observation_id,
            original_vector=original_vector,
            mode=mode,
        )
        recovered_result = _run_hikbox(
            "scan",
            "start",
            "--workspace",
            str(workspace),
            "--batch-size",
            "10",
        )
        assert recovered_result.returncode == 0, recovered_result.stderr
        assert _fetch_one(
            library_db,
            """
            SELECT status
            FROM assignment_runs
            ORDER BY id DESC
            LIMIT 1
            """,
        )[0] == "completed"
        assert _fetch_one(
            library_db,
            """
            SELECT status
            FROM scan_sessions
            ORDER BY id DESC
            LIMIT 1
            """,
        )[0] == "completed"
        assert _count_rows_matching(library_db, "SELECT COUNT(*) FROM person_face_assignments WHERE active = 1") == 3
        assert _count_rows_matching(library_db, "SELECT COUNT(*) FROM face_observations") == face_count_before
        assert _count_rows_matching(embedding_db, "SELECT COUNT(*) FROM face_embeddings") == embedding_count_before


def test_scan_start_ignores_orphan_embedding_and_records_warning(tmp_path: Path) -> None:
    source_dir = tmp_path / "source-orphan-embedding"
    _copy_fixture_assets(
        source_dir,
        [
            "pg_001_single_alex_01.jpg",
            "pg_002_single_alex_02.jpg",
        ],
    )
    workspace = tmp_path / "workspace-orphan-embedding"
    external_root = tmp_path / "external-root-orphan-embedding"

    init_result = _init_workspace(workspace, external_root)
    assert init_result.returncode == 0
    _prepare_workspace_models(workspace)
    add_result = _add_source(workspace, source_dir)
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
    assert _count_rows_matching(library_db, "SELECT COUNT(*) FROM person WHERE status = 'active'") == 0
    assert _count_rows_matching(library_db, "SELECT COUNT(*) FROM person_face_assignments WHERE active = 1") == 0

    sample_face_id = int(_fetch_one(library_db, "SELECT id FROM face_observations ORDER BY id ASC LIMIT 1")[0])
    sample_vector = _read_embedding_vector(embedding_db, face_observation_id=sample_face_id)
    orphan_face_id = sample_face_id + 10_000
    connection = sqlite3.connect(embedding_db)
    try:
        with connection:
            connection.execute(
                """
                INSERT INTO face_embeddings (
                  face_observation_id,
                  variant,
                  dimension,
                  l2_norm,
                  vector_blob,
                  created_at
                )
                VALUES (?, 'main', 512, 1.0, ?, '2026-04-25T00:00:00Z')
                """,
                (orphan_face_id, sample_vector.astype(np.float32).tobytes()),
            )
    finally:
        connection.close()

    second_result = _run_hikbox(
        "scan",
        "start",
        "--workspace",
        str(workspace),
        "--batch-size",
        "10",
    )

    assert second_result.returncode == 0, second_result.stderr
    assert _count_rows_matching(library_db, "SELECT COUNT(*) FROM person WHERE status = 'active'") == 0
    assert _count_rows_matching(library_db, "SELECT COUNT(*) FROM person_face_assignments WHERE active = 1") == 0
    latest_run = _fetch_one(
        library_db,
        """
        SELECT status, orphan_embedding_count, orphan_embedding_keys_json, assigned_count
        FROM assignment_runs
        ORDER BY id DESC
        LIMIT 1
        """,
    )
    assert latest_run[0] == "completed"
    assert latest_run[1] == 1
    assert f"face_observation_id={orphan_face_id}:main" in str(latest_run[2])
    assert latest_run[3] == 0
    log_rows = _read_jsonl(external_root / "logs" / "scan.log.jsonl")
    assert any(
        item["event"] == "assignment_warning"
        and item.get("orphan_embedding_count") == 1
        and f"face_observation_id={orphan_face_id}:main" in str(item.get("orphan_embedding_keys"))
        for item in log_rows
    )
