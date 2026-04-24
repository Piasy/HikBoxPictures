"""FastAPI app factory。"""

from __future__ import annotations

from fastapi import FastAPI

from hikbox_pictures.product.service_registry import ServiceContainer
from hikbox_pictures.web.api_routes import build_api_router
from hikbox_pictures.web.page_routes import build_page_router


def create_app(services: ServiceContainer) -> FastAPI:
    """创建绑定产品服务容器的 Web 应用。"""

    app = FastAPI(title="HikBox Pictures WebUI")
    app.state.services = services
    app.include_router(build_page_router())
    app.include_router(build_api_router())
    return app
