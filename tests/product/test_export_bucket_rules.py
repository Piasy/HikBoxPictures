from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from hikbox_pictures.product.config import initialize_workspace
from hikbox_pictures.product.export.bucket_rules import ExportFaceSample, bucket_for_photo
from hikbox_pictures.product.export.template_service import ExportTemplateService, ExportValidationError


def _insert_person(
    db_path: Path,
    *,
    person_uuid: str,
    display_name: str | None,
    is_named: int,
    status: str = "active",
) -> int:
    with sqlite3.connect(db_path) as conn:
        cursor = conn.execute(
            """
            INSERT INTO person(
                person_uuid,
                display_name,
                is_named,
                status,
                merged_into_person_id,
                created_at,
                updated_at
            )
            VALUES (?, ?, ?, ?, NULL, '2026-04-22T00:00:00+00:00', '2026-04-22T00:00:00+00:00')
            """,
            (person_uuid, display_name, is_named, status),
        )
        conn.commit()
        return int(cursor.lastrowid)


def test_template_persons_must_be_named_and_active(tmp_path: Path) -> None:
    layout = initialize_workspace(tmp_path / "workspace", tmp_path / "external")
    service = ExportTemplateService(layout.library_db_path)
    named_id = _insert_person(
        layout.library_db_path,
        person_uuid="00000000-0000-0000-0000-000000000111",
        display_name="Alice",
        is_named=1,
    )
    anonymous_id = _insert_person(
        layout.library_db_path,
        person_uuid="00000000-0000-0000-0000-000000000112",
        display_name=None,
        is_named=0,
    )
    inactive_id = _insert_person(
        layout.library_db_path,
        person_uuid="00000000-0000-0000-0000-000000000113",
        display_name="Bob",
        is_named=1,
        status="merged",
    )

    template = service.create_template(
        name="家庭合照",
        output_root=(tmp_path / "exports").resolve(),
        person_ids=[named_id],
    )

    with pytest.raises(ExportValidationError, match="is_named=1 且 status='active'"):
        service.update_template(template_id=template.id, person_ids=[anonymous_id])

    with pytest.raises(ExportValidationError, match="is_named=1 且 status='active'"):
        service.update_template(template_id=template.id, person_ids=[inactive_id])


def test_group_bucket_threshold_rule() -> None:
    selected_person_ids = {101, 102}
    faces = [
        ExportFaceSample(face_observation_id=1, person_id=101, area=120.0),
        ExportFaceSample(face_observation_id=2, person_id=102, area=80.0),
        ExportFaceSample(face_observation_id=3, person_id=999, area=20.0),
    ]
    decision = bucket_for_photo(selected_person_ids=selected_person_ids, faces=faces)
    assert decision.selected_min_area == 80.0
    assert decision.threshold == 20.0
    assert decision.bucket == "group"

    only_faces = [
        ExportFaceSample(face_observation_id=1, person_id=101, area=120.0),
        ExportFaceSample(face_observation_id=2, person_id=102, area=80.0),
        ExportFaceSample(face_observation_id=3, person_id=999, area=19.99),
    ]
    only_decision = bucket_for_photo(selected_person_ids=selected_person_ids, faces=only_faces)
    assert only_decision.bucket == "only"

    missing_area_faces = [
        ExportFaceSample(face_observation_id=1, person_id=101, area=120.0),
        ExportFaceSample(face_observation_id=2, person_id=102, area=80.0),
        ExportFaceSample(face_observation_id=3, person_id=None, area=None),
    ]
    missing_area_decision = bucket_for_photo(selected_person_ids=selected_person_ids, faces=missing_area_faces)
    assert missing_area_decision.bucket == "group"


def test_template_create_list_update_without_delete(tmp_path: Path) -> None:
    layout = initialize_workspace(tmp_path / "workspace", tmp_path / "external")
    service = ExportTemplateService(layout.library_db_path)
    person_a = _insert_person(
        layout.library_db_path,
        person_uuid="00000000-0000-0000-0000-000000000121",
        display_name="A",
        is_named=1,
    )
    person_b = _insert_person(
        layout.library_db_path,
        person_uuid="00000000-0000-0000-0000-000000000122",
        display_name="B",
        is_named=1,
    )

    template = service.create_template(
        name="模板-1",
        output_root=(tmp_path / "exports-a").resolve(),
        person_ids=[person_a],
    )
    listed = service.list_templates()
    assert len(listed) == 1
    assert listed[0].name == "模板-1"
    assert listed[0].person_ids == [person_a]

    updated = service.update_template(
        template_id=template.id,
        name="模板-2",
        output_root=(tmp_path / "exports-b").resolve(),
        person_ids=[person_a, person_b],
    )
    assert updated.name == "模板-2"
    assert updated.person_ids == [person_a, person_b]
    assert "delete_template" not in ExportTemplateService.__dict__
