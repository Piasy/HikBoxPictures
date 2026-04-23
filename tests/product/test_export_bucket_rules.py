import sqlite3
import uuid
from pathlib import Path

import pytest

from hikbox_pictures.product.config import initialize_workspace
from hikbox_pictures.product.export.bucket_rules import FaceBucketInput, classify_bucket
from hikbox_pictures.product.export.template_service import ExportTemplateService, ExportValidationError


def test_template_service_rejects_non_named_or_merged_people(tmp_path: Path) -> None:
    layout = initialize_workspace(
        workspace_root=tmp_path / "workspace",
        external_root=tmp_path / "external",
    )
    active_named_person_id = _insert_person(layout.library_db, is_named=True, status="active", display_name="Alice")
    anonymous_person_id = _insert_person(layout.library_db, is_named=False, status="active", display_name=None)
    merged_named_person_id = _insert_person(
        layout.library_db,
        is_named=True,
        status="merged",
        display_name="Merged",
        merged_into_person_id=active_named_person_id,
    )

    service = ExportTemplateService(layout.library_db)

    with pytest.raises(ExportValidationError, match="已命名且 active"):
        service.create_template(
            name="invalid-anonymous",
            output_root=str(tmp_path / "output-a"),
            person_ids=[active_named_person_id, anonymous_person_id],
        )

    template = service.create_template(
        name="valid-template",
        output_root=str(tmp_path / "output-b"),
        person_ids=[active_named_person_id],
    )

    with pytest.raises(ExportValidationError, match="已命名且 active"):
        service.update_template(
            template.id,
            person_ids=[active_named_person_id, merged_named_person_id],
        )


def test_group_bucket_threshold_rule() -> None:
    selected_faces = [
        FaceBucketInput(face_observation_id=1, area=400.0, assigned_person_id=11, is_selected_person=True),
        FaceBucketInput(face_observation_id=2, area=900.0, assigned_person_id=11, is_selected_person=True),
    ]

    assert classify_bucket(
        selected_faces
        + [FaceBucketInput(face_observation_id=3, area=99.0, assigned_person_id=22, is_selected_person=False)]
    ) == "only"

    assert classify_bucket(
        selected_faces
        + [FaceBucketInput(face_observation_id=4, area=100.0, assigned_person_id=22, is_selected_person=False)]
    ) == "group"

    assert classify_bucket(
        selected_faces
        + [FaceBucketInput(face_observation_id=5, area=100.0, assigned_person_id=None, is_selected_person=False)]
    ) == "group"

    assert classify_bucket(
        selected_faces
        + [FaceBucketInput(face_observation_id=6, area=None, assigned_person_id=22, is_selected_person=False)]
    ) == "group"


def test_template_service_supports_create_list_update_without_delete(tmp_path: Path) -> None:
    layout = initialize_workspace(
        workspace_root=tmp_path / "workspace",
        external_root=tmp_path / "external",
    )
    person_id = _insert_person(layout.library_db, is_named=True, status="active", display_name="Alice")
    service = ExportTemplateService(layout.library_db)

    created = service.create_template(
        name="family",
        output_root=str(tmp_path / "exports-a"),
        person_ids=[person_id],
    )
    updated = service.update_template(
        created.id,
        name="family-v2",
        output_root=str(tmp_path / "exports-b"),
        enabled=False,
        person_ids=[person_id],
    )
    listed = service.list_templates()

    assert "delete_template" not in ExportTemplateService.__dict__
    assert updated.id == created.id
    assert updated.name == "family-v2"
    assert updated.output_root == str((tmp_path / "exports-b").resolve())
    assert updated.enabled is False
    assert [item.id for item in listed] == [created.id]
    assert listed[0].person_ids == [person_id]


def test_template_service_rejects_relative_output_root(tmp_path: Path) -> None:
    layout = initialize_workspace(
        workspace_root=tmp_path / "workspace",
        external_root=tmp_path / "external",
    )
    person_id = _insert_person(layout.library_db, is_named=True, status="active", display_name="Alice")
    service = ExportTemplateService(layout.library_db)

    with pytest.raises(ExportValidationError, match="绝对路径"):
        service.create_template(
            name="relative-root",
            output_root="relative/exports",
            person_ids=[person_id],
        )


def _insert_person(
    library_db: Path,
    *,
    is_named: bool,
    status: str,
    display_name: str | None,
    merged_into_person_id: int | None = None,
) -> int:
    conn = sqlite3.connect(library_db)
    try:
        cursor = conn.execute(
            """
            INSERT INTO person(
              person_uuid, display_name, is_named, status, merged_into_person_id, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            """,
            (str(uuid.uuid4()), display_name, int(is_named), status, merged_into_person_id),
        )
        conn.commit()
        return int(cursor.lastrowid)
    finally:
        conn.close()
