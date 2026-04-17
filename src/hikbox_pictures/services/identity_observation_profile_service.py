from __future__ import annotations

try:
    import sqlite3
except ModuleNotFoundError:
    import pysqlite3 as sqlite3  # type: ignore[no-redef]

from hikbox_pictures.repositories.identity_observation_repo import IdentityObservationRepo


class IdentityObservationProfileService:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.repo = IdentityObservationRepo(conn)

    def get_active_profile_id(self) -> int:
        profile = self.repo.get_active_profile()
        if profile is None:
            raise ValueError("当前缺少 active observation profile")
        return int(profile["id"])
