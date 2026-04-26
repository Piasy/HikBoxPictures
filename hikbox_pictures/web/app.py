from __future__ import annotations

from pathlib import Path
from urllib.parse import parse_qs

from fastapi import FastAPI
from fastapi import HTTPException
from fastapi import Query
from fastapi import Request
from fastapi.responses import FileResponse
from fastapi.responses import HTMLResponse
from fastapi.responses import RedirectResponse
from fastapi.responses import Response
from fastapi.templating import Jinja2Templates

from hikbox_pictures.product.people_gallery import PeopleGalleryError
from hikbox_pictures.product.people_gallery import load_assignment_context_path
from hikbox_pictures.product.people_gallery import load_people_home_page
from hikbox_pictures.product.people_gallery import load_person_detail_page
from hikbox_pictures.product.people_gallery import PersonMergeValidationError
from hikbox_pictures.product.people_gallery import PersonNameValidationError
from hikbox_pictures.product.people_gallery import submit_people_merge
from hikbox_pictures.product.people_gallery import submit_person_name
from hikbox_pictures.product.sources import WorkspaceContext


TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATE_DIR))
NAME_FEEDBACK_COOKIE = "people_name_feedback"
HOME_FEEDBACK_COOKIE = "people_home_feedback"
NAME_FEEDBACK_MESSAGES = {
    "named": {"level": "info", "message": "名称已保存。"},
    "renamed": {"level": "info", "message": "名称已更新。"},
    "noop": {"level": "info", "message": "名称未变化。"},
}
HOME_FEEDBACK_MESSAGES = {
    "merge_succeeded": {"level": "info", "message": "人物已合并。"},
}


def create_people_gallery_app(
    *,
    workspace_context: WorkspaceContext,
    person_detail_page_size: int,
) -> FastAPI:
    app = FastAPI(title="HikBox People Gallery")

    def _render_people_home(
        request: Request,
        *,
        status_code: int = 200,
        home_feedback: dict[str, str] | None = None,
    ) -> HTMLResponse:
        page = load_people_home_page(workspace_context)
        response = templates.TemplateResponse(
            request=request,
            name="people_home.html",
            context={
                "page_title": "人物库浏览",
                "people_page": page,
                "home_feedback": home_feedback,
            },
            status_code=status_code,
        )
        if request.cookies.get(HOME_FEEDBACK_COOKIE) is not None and home_feedback is not None:
            response.delete_cookie(HOME_FEEDBACK_COOKIE, path="/")
        return response

    @app.get("/", include_in_schema=False)
    def people_root() -> RedirectResponse:
        return RedirectResponse(url="/people", status_code=302)

    @app.get("/people", response_class=HTMLResponse)
    def people_home(request: Request) -> HTMLResponse:
        try:
            return _render_people_home(
                request,
                home_feedback=_get_home_feedback(request),
            )
        except PeopleGalleryError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    @app.post("/people/merge", response_class=HTMLResponse)
    async def people_merge_submit(request: Request) -> Response:
        body = await request.body()
        form_data = parse_qs(body.decode("utf-8"), keep_blank_values=True)
        person_ids = form_data.get("person_id", [])
        try:
            submit_people_merge(
                workspace_context,
                person_ids=[str(person_id) for person_id in person_ids],
            )
        except PersonMergeValidationError as exc:
            try:
                return _render_people_home(
                    request,
                    status_code=400,
                    home_feedback={"level": "error", "message": str(exc)},
                )
            except PeopleGalleryError as page_exc:
                raise HTTPException(status_code=500, detail=str(page_exc)) from page_exc
        except PeopleGalleryError:
            try:
                return _render_people_home(
                    request,
                    status_code=500,
                    home_feedback={"level": "error", "message": "人物合并失败，请稍后重试。"},
                )
            except PeopleGalleryError as page_exc:
                raise HTTPException(status_code=500, detail=str(page_exc)) from page_exc

        response = RedirectResponse(url="/people", status_code=303)
        response.set_cookie(
            HOME_FEEDBACK_COOKIE,
            "merge_succeeded",
            httponly=True,
            samesite="lax",
            path="/",
        )
        return response

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
        feedback = _get_name_feedback(request)
        response = templates.TemplateResponse(
            request=request,
            name="person_detail.html",
            context={
                "page_title": detail_page.display_label,
                "detail_page": detail_page,
                "name_feedback": feedback,
                "name_form_value": detail_page.current_display_name or "",
            },
        )
        if feedback is not None:
            response.delete_cookie(NAME_FEEDBACK_COOKIE, path="/")
        return response

    @app.post("/people/{person_id}/name", response_class=HTMLResponse)
    async def person_name_submit(
        request: Request,
        person_id: str,
    ) -> Response:
        body = await request.body()
        form_data = parse_qs(body.decode("utf-8"), keep_blank_values=True)
        display_name = form_data.get("display_name", [""])[0]
        try:
            result = submit_person_name(
                workspace_context,
                person_id=person_id,
                display_name=display_name,
            )
        except PersonNameValidationError as exc:
            if exc.code == "person_not_found":
                return templates.TemplateResponse(
                    request=request,
                    name="not_found.html",
                    context={
                        "page_title": "人物不存在",
                        "person_id": person_id,
                    },
                    status_code=404,
                )
            detail_page = load_person_detail_page(
                workspace_context,
                person_id=person_id,
                page=1,
                page_size=person_detail_page_size,
            )
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
                    "name_feedback": {"level": "error", "message": str(exc)},
                    "name_form_value": display_name,
                },
                status_code=400,
            )
        except PeopleGalleryError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

        response = RedirectResponse(url=f"/people/{person_id}", status_code=303)
        response.set_cookie(
            NAME_FEEDBACK_COOKIE,
            result.outcome,
            httponly=True,
            samesite="lax",
            path="/",
        )
        return response

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


def _get_name_feedback(request: Request) -> dict[str, str] | None:
    feedback_code = request.cookies.get(NAME_FEEDBACK_COOKIE)
    if feedback_code is None:
        return None
    return NAME_FEEDBACK_MESSAGES.get(feedback_code)


def _get_home_feedback(request: Request) -> dict[str, str] | None:
    feedback_code = request.cookies.get(HOME_FEEDBACK_COOKIE)
    if feedback_code is None:
        return None
    return HOME_FEEDBACK_MESSAGES.get(feedback_code)
