import os
import time
from datetime import datetime, timezone

from hikbox_pictures.metadata import format_year_month, resolve_capture_datetime


def test_resolve_capture_datetime_prefers_content_creation_date(monkeypatch, tmp_path) -> None:
    photo = tmp_path / "photo.jpg"
    photo.write_bytes(b"image")
    expected = datetime(2025, 4, 3, 10, 30, tzinfo=timezone.utc)

    monkeypatch.setattr("hikbox_pictures.metadata.read_content_creation_datetime", lambda _: expected)
    monkeypatch.setattr("hikbox_pictures.metadata.read_birthtime_datetime", lambda _: None)
    monkeypatch.setattr(
        "hikbox_pictures.metadata.read_modification_datetime",
        lambda _: datetime(2025, 4, 4, 10, 30, tzinfo=timezone.utc),
    )

    assert resolve_capture_datetime(photo) == expected


def test_resolve_capture_datetime_falls_back_to_birthtime_then_mtime(monkeypatch, tmp_path) -> None:
    photo = tmp_path / "photo.jpg"
    photo.write_bytes(b"image")
    birthtime = datetime(2025, 2, 1, 8, 0, tzinfo=timezone.utc)
    mtime = datetime(2025, 2, 2, 8, 0, tzinfo=timezone.utc)

    monkeypatch.setattr("hikbox_pictures.metadata.read_content_creation_datetime", lambda _: None)
    monkeypatch.setattr("hikbox_pictures.metadata.read_birthtime_datetime", lambda _: birthtime)
    monkeypatch.setattr("hikbox_pictures.metadata.read_modification_datetime", lambda _: mtime)
    assert resolve_capture_datetime(photo) == birthtime

    monkeypatch.setattr("hikbox_pictures.metadata.read_birthtime_datetime", lambda _: None)
    assert resolve_capture_datetime(photo) == mtime


def test_resolve_capture_datetime_falls_back_when_content_creation_date_is_invalid(
    monkeypatch, tmp_path
) -> None:
    photo = tmp_path / "photo.jpg"
    photo.write_bytes(b"image")
    birthtime = datetime(2025, 3, 1, 8, 0, tzinfo=timezone.utc)

    def raise_invalid_datetime(_):
        raise ValueError("invalid date")

    monkeypatch.setattr("hikbox_pictures.metadata.read_content_creation_datetime", raise_invalid_datetime)
    monkeypatch.setattr("hikbox_pictures.metadata.read_birthtime_datetime", lambda _: birthtime)
    monkeypatch.setattr(
        "hikbox_pictures.metadata.read_modification_datetime",
        lambda _: datetime(2025, 3, 2, 8, 0, tzinfo=timezone.utc),
    )

    assert resolve_capture_datetime(photo) == birthtime


def test_format_year_month_returns_directory_name_using_local_timezone(monkeypatch) -> None:
    original_tz = os.environ.get("TZ")
    had_tz = "TZ" in os.environ

    monkeypatch.setenv("TZ", "Asia/Shanghai")
    time.tzset()
    try:
        moment = datetime(2025, 12, 31, 23, 59, tzinfo=timezone.utc)
        assert format_year_month(moment) == "2026-01"
    finally:
        if had_tz:
            os.environ["TZ"] = original_tz if original_tz is not None else ""
        else:
            os.environ.pop("TZ", None)
        time.tzset()
