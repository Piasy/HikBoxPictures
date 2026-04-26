from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi import HTTPException
from fastapi import Query
from fastapi import Request
from fastapi.responses import FileResponse
from fastapi.responses import HTMLResponse
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from hikbox_pictures.product.people_gallery import PeopleGalleryError
from hikbox_pictures.product.people_gallery import load_assignment_context_path
from hikbox_pictures.product.people_gallery import load_people_home_page
from hikbox_pictures.product.people_gallery import load_person_detail_page
from hikbox_pictures.product.sources import WorkspaceContext


TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATE_DIR))


def create_people_gallery_app(
    *,
    workspace_context: WorkspaceContext,
    person_detail_page_size: int,
) -> FastAPI:
    app = FastAPI(title="HikBox People Gallery")

    @app.get("/", include_in_schema=False)
    def people_root() -> RedirectResponse:
        return RedirectResponse(url="/people", status_code=302)

    @app.get("/people", response_class=HTMLResponse)
    def people_home(request: Request) -> HTMLResponse:
        try:
            page = load_people_home_page(workspace_context)
        except PeopleGalleryError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        return templates.TemplateResponse(
            request=request,
            name="people_home.html",
            context={
                "page_title": "人物库浏览",
                "people_page": page,
            },
        )

    @app.get("/people/{person_id}", response_class=HTMLResponse)
    def person_detail(
        request: Request,
        person_id: str,
        page: int = Query(default=1, ge=1),
    ) -> HTMLResponse:
        try:
            detail_page = load_person_detail_page(
                workspace_context,
                person_id=person_id,
                page=page,
                page_size=person_detail_page_size,
            )
        except PeopleGalleryError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        if detail_page is None:
            return templates.TemplateResponse(
                request=request,
                name="not_found.html",
                context={
                    "page_title": "人物不存在",
                    "person_id": person_id,
                },
                status_code=404,
            )
        return templates.TemplateResponse(
            request=request,
            name="person_detail.html",
            context={
                "page_title": detail_page.display_label,
                "detail_page": detail_page,
            },
        )

    @app.get("/images/assignments/{assignment_id}/context")
    def assignment_context_image(assignment_id: int) -> FileResponse:
        try:
            context_path = load_assignment_context_path(
                workspace_context,
                assignment_id=assignment_id,
            )
        except PeopleGalleryError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        if context_path is None or not context_path.is_file():
            raise HTTPException(status_code=404, detail="未找到样本图片。")
        return FileResponse(context_path)

    return app
