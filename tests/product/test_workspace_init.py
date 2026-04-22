import json
import sqlite3
from pathlib import Path

from hikbox_pictures.product.config import initialize_workspace


def _fetch_meta_map(db_path: Path, table: str) -> dict[str, str]:
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(f"SELECT key, value FROM {table}").fetchall()
        return {str(row[0]): str(row[1]) for row in rows}
    finally:
        conn.close()


def test_init_workspace_creates_two_databases_and_config(tmp_path: Path) -> None:
    workspace_root = tmp_path / "workspace"
    external_root = tmp_path / "external"

    layout = initialize_workspace(workspace_root=workspace_root, external_root=external_root)

    assert layout.hikbox_root == workspace_root / ".hikbox"
    assert layout.library_db.exists()
    assert layout.embedding_db.exists()
    assert layout.config_json.exists()

    config = json.loads(layout.config_json.read_text(encoding="utf-8"))
    assert config["external_root"] == str(external_root)

    library_meta = _fetch_meta_map(layout.library_db, "schema_meta")
    assert library_meta["schema_version"] == "1"
    assert library_meta["product_schema_name"] == "people_gallery_v1"

    embedding_meta = _fetch_meta_map(layout.embedding_db, "embedding_meta")
    assert embedding_meta["schema_version"] == "1"
    assert embedding_meta["vector_dim"] == "512"
    assert embedding_meta["vector_dtype"] == "float32"


def test_init_workspace_reuses_existing_databases(tmp_path: Path) -> None:
    workspace_root = tmp_path / "workspace"
    external_root = tmp_path / "external"
    layout = initialize_workspace(workspace_root=workspace_root, external_root=external_root)

    library_conn = sqlite3.connect(layout.library_db)
    try:
        library_conn.execute("CREATE TABLE IF NOT EXISTS user_data(id INTEGER PRIMARY KEY, name TEXT NOT NULL)")
        library_conn.execute("INSERT INTO user_data(name) VALUES (?)", ("keep-me",))
        library_conn.commit()
    finally:
        library_conn.close()

    embedding_conn = sqlite3.connect(layout.embedding_db)
    try:
        embedding_conn.execute(
            """
            INSERT INTO face_embedding(
                face_observation_id, feature_type, model_key, variant, dim, dtype, vector_blob, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            """,
            (7, "face", "test-model", "main", 512, "float32", b"\x00\x01"),
        )
        embedding_conn.commit()
    finally:
        embedding_conn.close()

    second_layout = initialize_workspace(workspace_root=workspace_root, external_root=external_root)

    verify_conn = sqlite3.connect(second_layout.library_db)
    try:
        row = verify_conn.execute("SELECT name FROM user_data ORDER BY id LIMIT 1").fetchone()
    finally:
        verify_conn.close()

    assert row is not None
    assert row[0] == "keep-me"

    verify_embedding_conn = sqlite3.connect(second_layout.embedding_db)
    try:
        embedding_row = verify_embedding_conn.execute(
            "SELECT model_key, variant, dim, dtype FROM face_embedding WHERE face_observation_id=?",
            (7,),
        ).fetchone()
    finally:
        verify_embedding_conn.close()

    assert embedding_row is not None
    assert tuple(embedding_row) == ("test-model", "main", 512, "float32")
