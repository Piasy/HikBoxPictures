from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.templating import Jinja2Templates

from hikbox_pictures.product.audit.service import AuditSamplingService
from hikbox_pictures.product.export.run_service import ExportRunService
from hikbox_pictures.product.export.template_service import ExportTemplateService
from hikbox_pictures.product.ops_event import OpsEventService
from hikbox_pictures.product.people.repository import SQLitePeopleRepository
from hikbox_pictures.product.people.service import PeopleService
from hikbox_pictures.product.scan.session_service import SQLiteScanSessionRepository, ScanSessionService
from hikbox_pictures.product.source.repository import SQLiteSourceRepository
from hikbox_pictures.product.source.service import SourceService

from .api_routes import ApiContractError, router as api_router
from .page_routes import router as page_router


@dataclass(frozen=True)
class ServiceContainer:
    library_db_path: Path
    scan_session_service: ScanSessionService
    source_service: SourceService
    people_service: PeopleService
    export_template_service: ExportTemplateService
    export_run_service: ExportRunService
    audit_service: AuditSamplingService
    ops_event_service: OpsEventService

    @classmethod
    def from_library_db(cls, library_db_path: Path) -> "ServiceContainer":
        scan_repo = SQLiteScanSessionRepository(library_db_path)
        source_repo = SQLiteSourceRepository(library_db_path)
        people_repo = SQLitePeopleRepository(library_db_path)
        return cls(
            library_db_path=library_db_path,
            scan_session_service=ScanSessionService(scan_repo),
            source_service=SourceService(source_repo),
            people_service=PeopleService(people_repo),
            export_template_service=ExportTemplateService(library_db_path),
            export_run_service=ExportRunService(library_db_path),
            audit_service=AuditSamplingService(library_db_path),
            ops_event_service=OpsEventService(library_db_path),
        )


def create_app(services: ServiceContainer) -> FastAPI:
    app = FastAPI(title="HikBox Pictures")
    templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

    app.state.services = services
    app.state.templates = templates

    @app.exception_handler(RequestValidationError)
    async def _handle_validation_error(_request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=400,
            content={
                "ok": False,
                "error": {
                    "code": "VALIDATION_ERROR",
                    "message": str(exc),
                },
            },
        )

    @app.exception_handler(ApiContractError)
    async def _handle_api_contract_error(_request, exc: ApiContractError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "ok": False,
                "error": {
                    "code": exc.code,
                    "message": exc.message,
                },
            },
        )

    app.include_router(page_router)
    app.include_router(api_router, prefix="/api")
    return app
