from pathlib import Path

import sqlite3

from hikbox_pictures.product.config import initialize_workspace
from hikbox_pictures.product.scan.discover_stage import DiscoverStageService
from hikbox_pictures.product.scan.session_service import ScanSessionRepository
from hikbox_pictures.product.source.repository import SourceRepository
from hikbox_pictures.product.source.service import SourceService


def test_discover_tracks_each_source_independently(tmp_path: Path) -> None:
    workspace_root = tmp_path / "workspace"
    external_root = tmp_path / "external"
    family_root = tmp_path / "family"
    travel_root = tmp_path / "travel"
    family_root.mkdir(parents=True)
    travel_root.mkdir(parents=True)

    (family_root / "IMG_1001.HEIC").write_bytes(b"f1")
    (travel_root / "IMG_2001.HEIC").write_bytes(b"t1")

    layout = initialize_workspace(workspace_root=workspace_root, external_root=external_root)
    source_service = SourceService(SourceRepository(layout.library_db))
    family = source_service.add_source(str(family_root), label="family")
    travel = source_service.add_source(str(travel_root), label="travel")

    session_repo = ScanSessionRepository(layout.library_db)
    session = session_repo.create_session(run_kind="scan_full", status="running", triggered_by="manual_cli")

    summary = DiscoverStageService(layout.library_db).run(scan_session_id=session.id)

    assert summary.by_source[family.id].discovered_assets == 1
    assert summary.by_source[travel.id].discovered_assets == 1
    assert summary.by_source[family.id].processed_assets >= 1
    assert summary.by_source[travel.id].processed_assets >= 1

    conn = sqlite3.connect(layout.library_db)
    try:
        rows = conn.execute(
            """
            SELECT library_source_id, processed_assets
            FROM scan_session_source
            WHERE scan_session_id = ?
            ORDER BY library_source_id ASC
            """,
            (session.id,),
        ).fetchall()
    finally:
        conn.close()

    assert rows == [(family.id, 1), (travel.id, 1)]
