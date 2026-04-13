from __future__ import annotations

from typing import Any

try:
    import sqlite3
except ModuleNotFoundError:
    import pysqlite3 as sqlite3  # type: ignore[no-redef]

from hikbox_pictures.repositories import ReviewRepo


class ReviewWorkflowService:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        self.review_repo = ReviewRepo(conn)

    def dismiss(self, review_id: int) -> dict[str, Any]:
        existing = self.review_repo.get_item(int(review_id))
        if existing is None:
            raise LookupError(f"review {review_id} 不存在")
        if existing["status"] == "dismissed" and existing["resolved_at"] is not None:
            return existing

        try:
            updated = self.review_repo.dismiss_item(int(review_id))
            if updated == 0:
                self.conn.rollback()
                latest = self.review_repo.get_item(int(review_id))
                if latest is None:
                    raise LookupError(f"review {review_id} 不存在")
                if latest["status"] == "dismissed" and latest["resolved_at"] is not None:
                    return latest
                raise RuntimeError(f"review {review_id} dismiss 失败")
            else:
                self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

        row = self.review_repo.get_item(int(review_id))
        if row is None:
            raise LookupError(f"review {review_id} 不存在")
        return row
