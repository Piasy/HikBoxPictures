from __future__ import annotations

from pathlib import Path
import time

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from hikbox_pictures.api.routes_export import router as export_router
from hikbox_pictures.api.routes_health import router as health_router
from hikbox_pictures.api.routes_logs import router as logs_router
from hikbox_pictures.api.routes_media import router as media_router
from hikbox_pictures.api.routes_people import router as people_router
from hikbox_pictures.api.routes_reviews import router as reviews_router
from hikbox_pictures.api.routes_scan import router as scan_router
from hikbox_pictures.api.routes_web import router as web_router
from hikbox_pictures.services.media_preview_service import MediaPreviewService
from hikbox_pictures.services.runtime import initialize_workspace


def create_app(workspace: Path) -> FastAPI:
    paths = initialize_workspace(workspace)
    web_root = Path(__file__).resolve().parent.parent / "web"
    templates = Jinja2Templates(directory=str(web_root / "templates"))
    static_root = web_root / "static"
    asset_version = max(
        (path.stat().st_mtime_ns for path in static_root.rglob("*") if path.is_file()),
        default=time.time_ns(),
    )
    templates.env.globals["asset_version"] = str(asset_version)

    app = FastAPI(title="HikBox Pictures API")
    app.state.workspace_paths = paths
    app.state.workspace = str(paths.root)
    app.state.db_path = str(paths.db_path)
    app.state.templates = templates
    app.state.asset_version = str(asset_version)
    app.state.media_preview_service = MediaPreviewService(
        db_path=paths.db_path,
        workspace=paths.root,
    )

    app.include_router(health_router, prefix="/api")
    app.include_router(scan_router, prefix="/api")
    app.include_router(people_router, prefix="/api")
    app.include_router(reviews_router, prefix="/api")
    app.include_router(export_router, prefix="/api")
    app.include_router(logs_router, prefix="/api")
    app.include_router(media_router, prefix="/api")
    app.include_router(web_router)
    app.mount("/static", StaticFiles(directory=str(web_root / "static")), name="static")
    return app
