import json
import sqlite3
from pathlib import Path

import pytest

from hikbox_pictures.product.config import initialize_workspace


def _read_meta(path: Path, table: str) -> dict[str, str]:
    with sqlite3.connect(path) as conn:
        rows = conn.execute(f"SELECT key, value FROM {table}").fetchall()
    return {key: value for key, value in rows}


def _journal_mode(path: Path) -> str:
    with sqlite3.connect(path) as conn:
        row = conn.execute("PRAGMA journal_mode;").fetchone()
    return str(row[0])


def test_initialize_workspace_creates_config_and_databases_when_missing(tmp_path: Path) -> None:
    workspace_root = tmp_path / "workspace"
    external_root = tmp_path / "external"

    layout = initialize_workspace(workspace_root=workspace_root, external_root=external_root)

    assert layout.hikbox_root == workspace_root / ".hikbox"
    assert layout.config_path.exists()
    assert layout.library_db_path.exists()
    assert layout.embedding_db_path.exists()

    config = json.loads(layout.config_path.read_text(encoding="utf-8"))
    assert config == {
        "version": 1,
        "external_root": str(external_root.resolve()),
    }

    library_meta = _read_meta(layout.library_db_path, "schema_meta")
    assert library_meta["schema_version"] == "1"
    assert library_meta["product_schema_name"] == "people_gallery_v1"

    embedding_meta = _read_meta(layout.embedding_db_path, "embedding_meta")
    assert embedding_meta["schema_version"] == "1"
    assert embedding_meta["vector_dim"] == "512"
    assert embedding_meta["vector_dtype"] == "float32"

    assert _journal_mode(layout.library_db_path).lower() == "wal"
    assert _journal_mode(layout.embedding_db_path).lower() == "wal"


def test_initialize_workspace_reuses_existing_workspace_without_overwrite(tmp_path: Path) -> None:
    workspace_root = tmp_path / "workspace"
    external_root = tmp_path / "external"

    first_layout = initialize_workspace(workspace_root=workspace_root, external_root=external_root)
    with sqlite3.connect(first_layout.library_db_path) as conn:
        conn.execute(
            "INSERT INTO schema_meta(key, value, updated_at) VALUES (?, ?, datetime('now'))",
            ("custom_key", "custom_value"),
        )
        conn.commit()
    first_config = json.loads(first_layout.config_path.read_text(encoding="utf-8"))

    second_layout = initialize_workspace(workspace_root=workspace_root, external_root=external_root)

    assert second_layout == first_layout
    second_config = json.loads(second_layout.config_path.read_text(encoding="utf-8"))
    assert second_config == first_config

    library_meta = _read_meta(second_layout.library_db_path, "schema_meta")
    assert library_meta["custom_key"] == "custom_value"
    assert library_meta["schema_version"] == "1"
    assert library_meta["product_schema_name"] == "people_gallery_v1"


def test_initialize_workspace_raises_when_config_external_root_conflicts(tmp_path: Path) -> None:
    workspace_root = tmp_path / "workspace"
    external_root = tmp_path / "external"
    other_external_root = tmp_path / "external-other"
    layout = initialize_workspace(workspace_root=workspace_root, external_root=external_root)
    assert layout.config_path.exists()

    with pytest.raises(ValueError, match="工作区配置不匹配"):
        initialize_workspace(workspace_root=workspace_root, external_root=other_external_root)


def test_initialize_workspace_does_not_override_existing_higher_schema_version(tmp_path: Path) -> None:
    workspace_root = tmp_path / "workspace"
    external_root = tmp_path / "external"
    layout = initialize_workspace(workspace_root=workspace_root, external_root=external_root)

    with sqlite3.connect(layout.library_db_path) as conn:
        conn.execute(
            "UPDATE schema_meta SET value=? WHERE key='schema_version'",
            ("2",),
        )
        conn.commit()
    with sqlite3.connect(layout.embedding_db_path) as conn:
        conn.execute(
            "UPDATE embedding_meta SET value=? WHERE key='schema_version'",
            ("2",),
        )
        conn.commit()

    initialize_workspace(workspace_root=workspace_root, external_root=external_root)

    library_meta = _read_meta(layout.library_db_path, "schema_meta")
    embedding_meta = _read_meta(layout.embedding_db_path, "embedding_meta")
    assert library_meta["schema_version"] == "2"
    assert embedding_meta["schema_version"] == "2"


def test_initialize_workspace_repairs_missing_business_table_in_half_initialized_db(tmp_path: Path) -> None:
    workspace_root = tmp_path / "workspace"
    external_root = tmp_path / "external"
    layout = initialize_workspace(workspace_root=workspace_root, external_root=external_root)

    with sqlite3.connect(layout.embedding_db_path) as conn:
        conn.execute("DROP TABLE face_embedding")
        conn.commit()

    with sqlite3.connect(layout.embedding_db_path) as conn:
        missing = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='face_embedding'",
        ).fetchone()
    assert missing is None

    initialize_workspace(workspace_root=workspace_root, external_root=external_root)

    with sqlite3.connect(layout.embedding_db_path) as conn:
        restored = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='face_embedding'",
        ).fetchone()
    assert restored is not None


def test_initialize_workspace_creates_external_artifacts_without_legacy_dirs(tmp_path: Path) -> None:
    workspace_root = tmp_path / "workspace"
    external_root = tmp_path / "external"

    layout = initialize_workspace(workspace_root=workspace_root, external_root=external_root)

    assert layout.crops_root.exists()
    assert layout.aligned_root.exists()
    assert layout.context_root.exists()
    assert layout.logs_root.exists()
    assert (layout.external_root / "artifacts" / "thumbs").exists() is False
    assert (layout.external_root / "artifacts" / "ann").exists() is False
