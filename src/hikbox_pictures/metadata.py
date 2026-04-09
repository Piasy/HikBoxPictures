from __future__ import annotations

from datetime import datetime
from pathlib import Path
import subprocess

MDLS_DATE_FORMAT = "%Y-%m-%d %H:%M:%S %z"


def _read_mdls_value(path: Path, attribute: str) -> str | None:
    result = subprocess.run(
        ["mdls", "-raw", "-name", attribute, str(path)],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return None

    value = result.stdout.strip()
    if value in {"", "(null)", "<nil>"}:
        return None
    return value


def read_content_creation_datetime(path: Path) -> datetime | None:
    value = _read_mdls_value(path, "kMDItemContentCreationDate")
    return datetime.strptime(value, MDLS_DATE_FORMAT) if value else None


def read_birthtime_datetime(path: Path) -> datetime | None:
    birthtime = getattr(path.stat(), "st_birthtime", None)
    if birthtime is None:
        return None
    return datetime.fromtimestamp(birthtime).astimezone()


def read_modification_datetime(path: Path) -> datetime:
    return datetime.fromtimestamp(path.stat().st_mtime).astimezone()


def resolve_capture_datetime(path: Path) -> datetime:
    for reader in (
        read_content_creation_datetime,
        read_birthtime_datetime,
        read_modification_datetime,
    ):
        try:
            value = reader(path)
        except ValueError:
            continue
        if value is not None:
            return value
    raise RuntimeError(f"Unable to resolve capture time for {path}")


def format_year_month(moment: datetime) -> str:
    return moment.astimezone().strftime("%Y-%m")
