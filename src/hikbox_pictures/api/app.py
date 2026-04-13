from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI

from hikbox_pictures.api.routes_health import router as health_router
from hikbox_pictures.services.runtime import initialize_workspace


def create_app(workspace: Path) -> FastAPI:
    paths = initialize_workspace(workspace)

    app = FastAPI(title="HikBox Pictures API")
    app.state.workspace = str(paths.root)
    app.state.db_path = str(paths.db_path)

    app.include_router(health_router, prefix="/api")
    return app
