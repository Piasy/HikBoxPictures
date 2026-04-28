from __future__ import annotations

from pathlib import Path
from urllib.parse import parse_qs
from urllib.parse import urlencode

from fastapi import FastAPI
from fastapi import HTTPException
from fastapi import Query
from fastapi import Request
from fastapi.responses import FileResponse
from fastapi.responses import HTMLResponse
from fastapi.responses import RedirectResponse
from fastapi.responses import Response
from fastapi.templating import Jinja2Templates

from hikbox_pictures.product.export_templates import compute_export_preview
from hikbox_pictures.product.export_templates import create_export_template
from hikbox_pictures.product.export_templates import execute_export
from hikbox_pictures.product.export_templates import ExportTemplateError
from hikbox_pictures.product.export_templates import ExportTemplateValidationError
from hikbox_pictures.product.export_templates import load_eligible_persons_for_template
from hikbox_pictures.product.export_templates import load_export_run_detail
from hikbox_pictures.product.export_templates import load_export_runs_for_template
from hikbox_pictures.product.export_templates import load_export_template_detail
from hikbox_pictures.product.export_templates import load_export_templates_list
from hikbox_pictures.product.people_gallery import PeopleGalleryError
from hikbox_pictures.product.people_gallery import load_assignment_context_path
from hikbox_pictures.product.people_gallery import load_people_home_page
from hikbox_pictures.product.people_gallery import load_person_detail_page
from hikbox_pictures.product.people_gallery import PersonExclusionValidationError
from hikbox_pictures.product.people_gallery import PersonMergeValidationError
from hikbox_pictures.product.people_gallery import PersonMergeUndoValidationError
from hikbox_pictures.product.people_gallery import PersonNameValidationError
from hikbox_pictures.product.people_gallery import submit_people_merge
from hikbox_pictures.product.people_gallery import submit_people_merge_undo
from hikbox_pictures.product.people_gallery import submit_person_exclusions
from hikbox_pictures.product.people_gallery import submit_person_name
from hikbox_pictures.product.sources import WorkspaceContext


TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATE_DIR))
NAME_FEEDBACK_COOKIE = "people_name_feedback"
HOME_FEEDBACK_COOKIE = "people_home_feedback"
EXCLUSION_FEEDBACK_COOKIE = "people_exclusion_feedback"
NAME_FEEDBACK_MESSAGES = {
    "named": {"level": "info", "message": "名称已保存。"},
    "renamed": {"level": "info", "message": "名称已更新。"},
    "noop": {"level": "info", "message": "名称未变化。"},
}
HOME_FEEDBACK_MESSAGES = {
    "merge_succeeded": {"level": "info", "message": "人物已合并。"},
    "merge_undo_succeeded": {"level": "info", "message": "最近一次合并已撤销。"},
    "exclude_succeeded_person_removed": {"level": "info", "message": "已排除所选样本，当前人物已清空。"},
}
EXCLUSION_FEEDBACK_MESSAGES = {
    "exclude_succeeded": {"level": "info", "message": "已排除所选样本。"},
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

    def _render_person_detail(
        request: Request,
        *,
        person_id: str,
        status_code: int = 200,
        name_feedback: dict[str, str] | None = None,
        exclusion_feedback: dict[str, str] | None = None,
        name_form_value: str | None = None,
    ) -> HTMLResponse:
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
        response = templates.TemplateResponse(
            request=request,
            name="person_detail.html",
            context={
                "page_title": detail_page.display_label,
                "detail_page": detail_page,
                "name_feedback": name_feedback,
                "exclusion_feedback": exclusion_feedback,
                "name_form_value": detail_page.current_display_name if name_form_value is None else name_form_value,
            },
            status_code=status_code,
        )
        if request.cookies.get(NAME_FEEDBACK_COOKIE) is not None and name_feedback is not None:
            response.delete_cookie(NAME_FEEDBACK_COOKIE, path="/")
        if request.cookies.get(EXCLUSION_FEEDBACK_COOKIE) is not None and exclusion_feedback is not None:
            response.delete_cookie(EXCLUSION_FEEDBACK_COOKIE, path="/")
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

    @app.post("/people/merge/undo", response_class=HTMLResponse)
    def people_merge_undo_submit(request: Request) -> Response:
        try:
            submit_people_merge_undo(workspace_context)
        except PersonMergeUndoValidationError as exc:
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
                    home_feedback={"level": "error", "message": "撤销最近一次合并失败，请稍后重试。"},
                )
            except PeopleGalleryError as page_exc:
                raise HTTPException(status_code=500, detail=str(page_exc)) from page_exc

        response = RedirectResponse(url="/people", status_code=303)
        response.set_cookie(
            HOME_FEEDBACK_COOKIE,
            "merge_undo_succeeded",
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
            detail_page = load_person_detail_page(workspace_context, person_id=person_id, page=page, page_size=person_detail_page_size)
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
                "exclusion_feedback": _get_exclusion_feedback(request),
                "name_form_value": detail_page.current_display_name or "",
            },
        )
        if feedback is not None:
            response.delete_cookie(NAME_FEEDBACK_COOKIE, path="/")
        if request.cookies.get(EXCLUSION_FEEDBACK_COOKIE) is not None:
            response.delete_cookie(EXCLUSION_FEEDBACK_COOKIE, path="/")
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
                return _render_person_detail(request, person_id=person_id, status_code=404)
            return _render_person_detail(
                request,
                person_id=person_id,
                status_code=400,
                name_feedback={"level": "error", "message": str(exc)},
                name_form_value=display_name,
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

    @app.post("/people/{person_id}/exclude", response_class=HTMLResponse)
    async def person_exclusion_submit(
        request: Request,
        person_id: str,
    ) -> Response:
        body = await request.body()
        form_data = parse_qs(body.decode("utf-8"), keep_blank_values=True)
        assignment_ids = form_data.get("assignment_id", [])
        try:
            result = submit_person_exclusions(
                workspace_context,
                person_id=person_id,
                assignment_ids=[str(assignment_id) for assignment_id in assignment_ids],
            )
        except PersonExclusionValidationError as exc:
            if exc.code == "person_not_found":
                return _render_person_detail(
                    request,
                    person_id=person_id,
                    status_code=404,
                    exclusion_feedback={"level": "error", "message": str(exc)},
                )
            return _render_person_detail(
                request,
                person_id=person_id,
                status_code=400,
                exclusion_feedback={"level": "error", "message": str(exc)},
            )
        except PeopleGalleryError:
            return _render_person_detail(
                request,
                person_id=person_id,
                status_code=500,
                exclusion_feedback={"level": "error", "message": "批量排除失败，请稍后重试。"},
            )

        if result.remaining_sample_count > 0:
            response = RedirectResponse(url=f"/people/{person_id}", status_code=303)
            response.set_cookie(
                EXCLUSION_FEEDBACK_COOKIE,
                "exclude_succeeded",
                httponly=True,
                samesite="lax",
                path="/",
            )
            return response

        response = RedirectResponse(url="/people", status_code=303)
        response.set_cookie(
            HOME_FEEDBACK_COOKIE,
            "exclude_succeeded_person_removed",
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

    @app.get("/exports", response_class=HTMLResponse)
    def exports_list(request: Request) -> HTMLResponse:
        try:
            template_list = load_export_templates_list(workspace_context)
        except ExportTemplateError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        return templates.TemplateResponse(
            request=request,
            name="exports_list.html",
            context={
                "page_title": "导出模板",
                "templates": template_list,
            },
        )

    @app.get("/exports/new", response_class=HTMLResponse)
    def exports_new(request: Request) -> HTMLResponse:
        try:
            eligible_persons = load_eligible_persons_for_template(workspace_context)
        except ExportTemplateError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        form_feedback = request.query_params.get("form_feedback")
        form_feedback_level = request.query_params.get("form_feedback_level", "error")
        return templates.TemplateResponse(
            request=request,
            name="export_template_new.html",
            context={
                "page_title": "新建导出模板",
                "eligible_persons": eligible_persons,
                "form_feedback": {"message": form_feedback, "level": form_feedback_level} if form_feedback else None,
                "form_name_value": request.query_params.get("form_name_value", ""),
                "form_output_root_value": request.query_params.get("form_output_root_value", ""),
                "form_person_ids": request.query_params.getlist("form_person_id"),
            },
        )

    @app.post("/exports/new")
    async def exports_new_post(request: Request) -> Response:
        body = await request.body()
        form_data = parse_qs(body.decode("utf-8"), keep_blank_values=True)
        name = form_data.get("name", [""])[0]
        output_root = form_data.get("output_root", [""])[0]
        person_ids = form_data.get("person_id", [])
        try:
            create_export_template(
                workspace_context,
                name=name,
                person_ids=[str(pid) for pid in person_ids],
                output_root=output_root,
            )
        except ExportTemplateValidationError as exc:
            params = urlencode({
                "form_feedback": str(exc),
                "form_feedback_level": "error",
                "form_name_value": name,
                "form_output_root_value": output_root,
                "form_person_id": person_ids,
            }, doseq=True)
            return RedirectResponse(url=f"/exports/new?{params}", status_code=303)
        except ExportTemplateError as exc:
            params = urlencode({
                "form_feedback": str(exc),
                "form_feedback_level": "error",
                "form_name_value": name,
                "form_output_root_value": output_root,
                "form_person_id": person_ids,
            }, doseq=True)
            return RedirectResponse(url=f"/exports/new?{params}", status_code=303)
        return RedirectResponse(url="/exports", status_code=303)

    @app.get("/api/export-templates")
    def api_export_templates_list() -> dict[str, object]:
        try:
            templates = load_export_templates_list(workspace_context)
        except ExportTemplateError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        return {
            "templates": [
                {
                    "template_id": t.template_id,
                    "name": t.name,
                    "output_root": t.output_root,
                    "status": t.status,
                    "created_at": t.created_at,
                    "person_count": t.person_count,
                    "person_ids": t.person_ids,
                    "person_names": t.person_names,
                }
                for t in templates
            ]
        }

    @app.post("/api/export-templates")
    async def api_export_templates_create(request: Request) -> Response:
        body = await request.body()
        form_data = parse_qs(body.decode("utf-8"), keep_blank_values=True)
        name = form_data.get("name", [""])[0]
        output_root = form_data.get("output_root", [""])[0]
        person_ids = form_data.get("person_id", [])
        try:
            result = create_export_template(
                workspace_context,
                name=name,
                person_ids=[str(pid) for pid in person_ids],
                output_root=output_root,
            )
        except ExportTemplateValidationError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except ExportTemplateError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        return {"template_id": result.template_id}

    @app.get("/exports/{template_id}/preview", response_class=HTMLResponse)
    def export_template_preview_page(request: Request, template_id: str) -> HTMLResponse:
        try:
            preview = compute_export_preview(workspace_context, template_id=template_id)
        except ExportTemplateValidationError as exc:
            return templates.TemplateResponse(
                request=request,
                name="export_template_preview.html",
                context={
                    "page_title": "预览导出模板",
                    "error_message": str(exc),
                },
                status_code=400,
            )
        except ExportTemplateError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

        template = load_export_template_detail(workspace_context, template_id=template_id)
        return templates.TemplateResponse(
            request=request,
            name="export_template_preview.html",
            context={
                "page_title": f"预览：{template.name}",
                "template": template,
                "preview": preview,
            },
        )

    @app.get("/api/export-templates/{template_id}/preview")
    def api_export_template_preview(template_id: str) -> dict[str, object]:
        try:
            preview = compute_export_preview(workspace_context, template_id=template_id)
        except ExportTemplateValidationError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except ExportTemplateError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        return {
            "template_id": template_id,
            "total_count": preview.total_count,
            "only_count": preview.only_count,
            "group_count": preview.group_count,
            "months": [
                {
                    "month": m.month,
                    "only": [
                        {
                            "asset_id": a.asset_id,
                            "file_name": a.file_name,
                            "context_url": a.context_url,
                            "representative_person_id": a.representative_person_id,
                        }
                        for a in m.only_assets
                    ],
                    "group": [
                        {
                            "asset_id": a.asset_id,
                            "file_name": a.file_name,
                            "context_url": a.context_url,
                            "representative_person_id": a.representative_person_id,
                        }
                        for a in m.group_assets
                    ],
                }
                for m in preview.month_buckets
            ],
        }

    @app.get("/exports/{template_id}/execute", response_class=HTMLResponse)
    def export_template_execute_page(request: Request, template_id: str) -> HTMLResponse:
        try:
            template = load_export_template_detail(workspace_context, template_id=template_id)
            preview = compute_export_preview(workspace_context, template_id=template_id)
        except ExportTemplateValidationError as exc:
            return templates.TemplateResponse(
                request=request,
                name="export_template_execute.html",
                context={
                    "page_title": "执行导出",
                    "error_message": str(exc),
                },
                status_code=400,
            )
        except ExportTemplateError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        return templates.TemplateResponse(
            request=request,
            name="export_template_execute.html",
            context={
                "page_title": f"执行导出：{template.name}",
                "template": template,
                "preview": preview,
            },
        )

    @app.post("/api/export-templates/{template_id}/execute")
    def api_export_template_execute(template_id: str) -> dict[str, object]:
        try:
            run_id = execute_export(workspace_context, template_id=template_id)
        except ExportTemplateValidationError as exc:
            if exc.code == "export_in_progress":
                raise HTTPException(status_code=423, detail=str(exc)) from exc
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except ExportTemplateError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"导出执行失败：{exc}") from exc
        return {"run_id": run_id}

    @app.get("/exports/{template_id}/history", response_class=HTMLResponse)
    def export_template_history_page(request: Request, template_id: str) -> HTMLResponse:
        try:
            template = load_export_template_detail(workspace_context, template_id=template_id)
            runs = load_export_runs_for_template(workspace_context, template_id=template_id)
        except ExportTemplateError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

        runs_with_deliveries = []
        for run in runs:
            try:
                detail = load_export_run_detail(workspace_context, run_id=run.run_id)
                deliveries = detail.deliveries
            except ExportTemplateValidationError:
                deliveries = []
            runs_with_deliveries.append(
                {
                    "run_id": run.run_id,
                    "status": run.status,
                    "started_at": run.started_at,
                    "copied_count": run.copied_count,
                    "skipped_count": run.skipped_count,
                    "deliveries": [
                        {
                            "delivery_id": d.delivery_id,
                            "target_path": d.target_path,
                            "result": d.result,
                            "mov_result": d.mov_result,
                        }
                        for d in deliveries
                    ],
                }
            )

        return templates.TemplateResponse(
            request=request,
            name="export_template_history.html",
            context={
                "page_title": f"导出历史：{template.name}",
                "template": template,
                "runs": runs_with_deliveries,
            },
        )

    @app.get("/api/export-templates/{template_id}/runs")
    def api_export_template_runs(template_id: str) -> dict[str, object]:
        try:
            runs = load_export_runs_for_template(workspace_context, template_id=template_id)
        except ExportTemplateError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        return {
            "runs": [
                {
                    "run_id": r.run_id,
                    "template_id": r.template_id,
                    "status": r.status,
                    "started_at": r.started_at,
                    "completed_at": r.completed_at,
                    "copied_count": r.copied_count,
                    "skipped_count": r.skipped_count,
                }
                for r in runs
            ]
        }

    @app.get("/api/export-runs/{run_id}")
    def api_export_run_detail(run_id: int) -> dict[str, object]:
        try:
            detail = load_export_run_detail(workspace_context, run_id=run_id)
        except ExportTemplateValidationError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ExportTemplateError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        return {
            "run_id": detail.run_id,
            "template_id": detail.template_id,
            "template_name": detail.template_name,
            "status": detail.status,
            "started_at": detail.started_at,
            "completed_at": detail.completed_at,
            "copied_count": detail.copied_count,
            "skipped_count": detail.skipped_count,
            "deliveries": [
                {
                    "delivery_id": d.delivery_id,
                    "asset_id": d.asset_id,
                    "target_path": d.target_path,
                    "result": d.result,
                    "mov_result": d.mov_result,
                }
                for d in detail.deliveries
            ],
        }

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


def _get_exclusion_feedback(request: Request) -> dict[str, str] | None:
    feedback_code = request.cookies.get(EXCLUSION_FEEDBACK_COOKIE)
    if feedback_code is None:
        return None
    return EXCLUSION_FEEDBACK_MESSAGES.get(feedback_code)
