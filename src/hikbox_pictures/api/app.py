from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI

from hikbox_pictures.api.routes_export import router as export_router
from hikbox_pictures.api.routes_health import router as health_router
from hikbox_pictures.api.routes_logs import router as logs_router
from hikbox_pictures.api.routes_people import router as people_router
from hikbox_pictures.api.routes_reviews import router as reviews_router
from hikbox_pictures.api.routes_scan import router as scan_router
from hikbox_pictures.services.runtime import initialize_workspace


def create_app(workspace: Path) -> FastAPI:
    paths = initialize_workspace(workspace)

    app = FastAPI(title="HikBox Pictures API")
    app.state.workspace = str(paths.root)
    app.state.db_path = str(paths.db_path)

    app.include_router(health_router, prefix="/api")
    app.include_router(scan_router, prefix="/api")
    app.include_router(people_router, prefix="/api")
    app.include_router(reviews_router, prefix="/api")
    app.include_router(export_router, prefix="/api")
    app.include_router(logs_router, prefix="/api")
    return app
