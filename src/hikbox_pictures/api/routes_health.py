from __future__ import annotations

from fastapi import APIRouter, Request

router = APIRouter()


@router.get("/health")
def health(request: Request) -> dict[str, object]:
    return {
        "ok": True,
        "workspace": request.app.state.workspace,
        "db_path": request.app.state.db_path,
    }
