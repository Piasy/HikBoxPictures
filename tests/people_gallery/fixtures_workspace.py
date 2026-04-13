from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

try:
    import sqlite3
except ModuleNotFoundError:
    import pysqlite3 as sqlite3  # type: ignore[no-redef]

from hikbox_pictures.db.connection import connect_db
from hikbox_pictures.db.migrator import apply_migrations
from hikbox_pictures.repositories import (
    AssetRepo,
    ExportRepo,
    OpsEventRepo,
    PersonRepo,
    ReviewRepo,
    ScanRepo,
    SourceRepo,
)
from hikbox_pictures.workspace import WorkspacePaths, ensure_workspace_layout


@dataclass
class SeedWorkspace:
    root: Path
    paths: WorkspacePaths
    conn: sqlite3.Connection
    source_repo: SourceRepo
    scan_repo: ScanRepo
    asset_repo: AssetRepo
    person_repo: PersonRepo
    review_repo: ReviewRepo
    export_repo: ExportRepo
    ops_event_repo: OpsEventRepo

    def counts(self) -> dict[str, int]:
        tables = (
            "library_source",
            "person",
            "review_item",
            "export_template",
        )
        result: dict[str, int] = {}
        for table in tables:
            row = self.conn.execute(f"SELECT COUNT(*) AS c FROM {table}").fetchone()
            result[table] = int(row["c"])
        return result

    def close(self) -> None:
        self.conn.close()


def build_seed_workspace(root: Path) -> SeedWorkspace:
    paths = ensure_workspace_layout(root)
    conn = connect_db(paths.db_path)
    apply_migrations(conn)

    source_repo = SourceRepo(conn)
    scan_repo = ScanRepo(conn)
    asset_repo = AssetRepo(conn)
    person_repo = PersonRepo(conn)
    review_repo = ReviewRepo(conn)
    export_repo = ExportRepo(conn)
    ops_event_repo = OpsEventRepo(conn)

    source_a = source_repo.add_source("iCloud", "/data/a", root_fingerprint="fp-a", active=True)
    source_b = source_repo.add_source("NAS", "/data/b", root_fingerprint="fp-b", active=True)

    _ = scan_repo.create_session(mode="initial", status="completed", started=True)
    resumable_session = scan_repo.create_session(mode="incremental", status="paused", started=True)
    scan_repo.create_session_source(resumable_session, source_a, status="paused")
    scan_repo.create_session_source(resumable_session, source_b, status="pending")

    person_a = person_repo.create_person("人物A", status="active", confirmed=True, ignored=False)
    person_b = person_repo.create_person("人物B", status="active", confirmed=True, ignored=False)
    person_c = person_repo.create_person("人物C", status="active", confirmed=False, ignored=False)

    review_repo.create_review_item("new_person", payload_json="{}", priority=30, status="open", primary_person_id=person_a)
    review_repo.create_review_item(
        "possible_merge",
        payload_json="{}",
        priority=20,
        status="open",
        primary_person_id=person_a,
        secondary_person_id=person_b,
    )
    review_repo.create_review_item(
        "possible_split",
        payload_json="{}",
        priority=10,
        status="open",
        primary_person_id=person_c,
    )
    review_repo.create_review_item(
        "low_confidence_assignment",
        payload_json="{}",
        priority=5,
        status="open",
        primary_person_id=person_b,
    )

    template_id = export_repo.create_template(
        name="家庭模板",
        output_root=str(paths.exports_dir / "family"),
        include_group=True,
        export_live_mov=True,
        enabled=True,
    )
    export_repo.add_template_person(template_id=template_id, person_id=person_a, position=0)
    export_repo.add_template_person(template_id=template_id, person_id=person_b, position=1)

    ops_event_repo.append_event(
        level="info",
        component="seed",
        event_type="seed_ready",
        message="seed 数据已初始化",
        run_kind="scan",
        run_id=str(resumable_session),
    )

    conn.commit()

    return SeedWorkspace(
        root=paths.root,
        paths=paths,
        conn=conn,
        source_repo=source_repo,
        scan_repo=scan_repo,
        asset_repo=asset_repo,
        person_repo=person_repo,
        review_repo=review_repo,
        export_repo=export_repo,
        ops_event_repo=ops_event_repo,
    )
