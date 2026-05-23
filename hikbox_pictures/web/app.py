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
from hikbox_pictures.product.export_templates import delete_export_template
from hikbox_pictures.product.export_templates import execute_export
from hikbox_pictures.product.export_templates import execute_export_async
from hikbox_pictures.product.export_templates import ExportTemplateError
from hikbox_pictures.product.export_templates import ExportTemplateValidationError
from hikbox_pictures.product.export_templates import load_eligible_persons_for_template
from hikbox_pictures.product.export_templates import load_export_template_burst_pick
from hikbox_pictures.product.export_templates import load_export_preview_asset_detail
from hikbox_pictures.product.export_templates import load_export_run_detail
from hikbox_pictures.product.export_templates import load_export_runs_for_template
from hikbox_pictures.product.export_templates import load_export_template_detail
from hikbox_pictures.product.export_templates import load_export_templates_list
from hikbox_pictures.product.export_templates import submit_export_template_burst_pick
from hikbox_pictures.product.people_gallery import PeopleGalleryError
from hikbox_pictures.product.people_gallery import load_assignment_context_path
from hikbox_pictures.product.people_gallery import load_face_crop_path
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
PREVIEW_EXCLUSION_FEEDBACK_COOKIE = "preview_exclusion_feedback"
PREVIEW_EXCLUSION_FEEDBACK_MESSAGES = {
    "exclude_succeeded": {"level": "info", "message": "已排除该人脸。"},
    "person_removed": {"level": "info", "message": "已排除该人脸，该人物已无剩余样本。"},
    "export_in_progress": {"level": "error", "message": "导出进行中，无法排除。"},
    "internal_error": {"level": "error", "message": "排除失败，请稍后重试。"},
}
EXPORTS_FEEDBACK_COOKIE = "export_templates_feedback"
EXPORTS_FEEDBACK_MESSAGES = {
    "delete_succeeded": {"level": "info", "message": "模板已删除。"},
    "delete_missing": {"level": "error", "message": "模板不存在。"},
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
        except ExportTemplateValidationError as exc:
            if exc.code == "export_in_progress":
                try:
                    return _render_people_home(
                        request,
                        status_code=423,
                        home_feedback={"level": "error", "message": str(exc)},
                    )
                except PeopleGalleryError as page_exc:
                    raise HTTPException(status_code=500, detail=str(page_exc)) from page_exc
            raise
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
        except ExportTemplateValidationError as exc:
            if exc.code == "export_in_progress":
                try:
                    return _render_people_home(
                        request,
                        status_code=423,
                        home_feedback={"level": "error", "message": str(exc)},
                    )
                except PeopleGalleryError as page_exc:
                    raise HTTPException(status_code=500, detail=str(page_exc)) from page_exc
            raise
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
        except ExportTemplateValidationError as exc:
            if exc.code == "export_in_progress":
                return _render_person_detail(
                    request,
                    person_id=person_id,
                    status_code=423,
                    name_feedback={"level": "error", "message": str(exc)},
                    name_form_value=display_name,
                )
            raise
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
        except ExportTemplateValidationError as exc:
            if exc.code == "export_in_progress":
                return _render_person_detail(
                    request,
                    person_id=person_id,
                    status_code=423,
                    exclusion_feedback={"level": "error", "message": str(exc)},
                )
            raise
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

    @app.get("/images/faces/{face_observation_id}/crop")
    def face_crop_image(face_observation_id: int) -> FileResponse:
        try:
            crop_path = load_face_crop_path(
                workspace_context,
                face_observation_id=face_observation_id,
            )
        except PeopleGalleryError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        if crop_path is None or not crop_path.is_file():
            raise HTTPException(status_code=404, detail="未找到人脸裁切图。")
        return FileResponse(crop_path)

    @app.get("/exports", response_class=HTMLResponse)
    def exports_list(request: Request) -> HTMLResponse:
        try:
            template_list = load_export_templates_list(workspace_context)
        except ExportTemplateError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        list_feedback = _get_exports_feedback(request)
        response = templates.TemplateResponse(
            request=request,
            name="exports_list.html",
            context={
                "page_title": "导出模板",
                "templates": template_list,
                "list_feedback": list_feedback,
            },
        )
        if list_feedback is not None:
            response.delete_cookie(EXPORTS_FEEDBACK_COOKIE, path="/")
        return response

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

    @app.post("/exports/{template_id}/delete")
    def export_template_delete_action(template_id: str) -> RedirectResponse:
        response = RedirectResponse(url="/exports", status_code=303)
        try:
            delete_export_template(workspace_context, template_id=template_id)
        except ExportTemplateValidationError as exc:
            if exc.code == "template_not_found":
                response.set_cookie(
                    EXPORTS_FEEDBACK_COOKIE,
                    "delete_missing",
                    httponly=True,
                    samesite="lax",
                    path="/",
                )
                return response
            raise
        except ExportTemplateError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        response.set_cookie(
            EXPORTS_FEEDBACK_COOKIE,
            "delete_succeeded",
            httponly=True,
            samesite="lax",
            path="/",
        )
        return response

    @app.delete("/api/export-templates/{template_id}", status_code=204)
    def api_export_template_delete(template_id: str) -> Response:
        try:
            delete_export_template(workspace_context, template_id=template_id)
        except ExportTemplateValidationError as exc:
            if exc.code == "template_not_found":
                raise HTTPException(status_code=404, detail=str(exc)) from exc
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except ExportTemplateError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        return Response(status_code=204)

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
                    "total_count": m.total_count,
                    "only": [
                        {
                            "asset_id": a.asset_id,
                            "file_name": a.file_name,
                            "context_url": a.context_url,
                            "representative_person_id": a.representative_person_id,
                            "is_live": a.is_live,
                        }
                        for a in m.only_assets
                    ],
                    "group": [
                        {
                            "asset_id": a.asset_id,
                            "file_name": a.file_name,
                            "context_url": a.context_url,
                            "representative_person_id": a.representative_person_id,
                            "is_live": a.is_live,
                        }
                        for a in m.group_assets
                    ],
                }
                for m in preview.month_buckets
            ],
        }

    @app.get("/exports/{template_id}/burst-pick", response_class=HTMLResponse)
    def export_template_burst_pick_page(request: Request, template_id: str) -> HTMLResponse:
        try:
            template = load_export_template_detail(workspace_context, template_id=template_id)
            burst_pick = load_export_template_burst_pick(workspace_context, template_id=template_id)
        except ExportTemplateValidationError as exc:
            return templates.TemplateResponse(
                request=request,
                name="export_template_burst_pick.html",
                context={
                    "page_title": "连拍挑选",
                    "error_message": str(exc),
                    "form_feedback": {"level": "error", "message": str(exc)},
                },
                status_code=400,
            )
        except ExportTemplateError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

        feedback_code = request.query_params.get("feedback")
        feedback = None
        if feedback_code == "saved":
            feedback = {"level": "info", "message": "连拍挑选已保存。"}
        return templates.TemplateResponse(
            request=request,
            name="export_template_burst_pick.html",
            context={
                "page_title": f"连拍挑选：{template.name}",
                "template": template,
                "burst_pick": burst_pick,
                "form_feedback": feedback,
            },
        )

    @app.post("/exports/{template_id}/burst-pick", response_class=HTMLResponse)
    async def export_template_burst_pick_submit(request: Request, template_id: str) -> Response:
        body = await request.body()
        form_data = parse_qs(body.decode("utf-8"), keep_blank_values=True)
        group_keys = [str(value) for value in form_data.get("group_key", [])]
        submitted_groups = [
            {
                "group_key": group_key,
                "keep_asset_ids": form_data.get(f"keep_asset_id__{group_key}", []),
            }
            for group_key in group_keys
        ]
        try:
            submit_export_template_burst_pick(
                workspace_context,
                template_id=template_id,
                submitted_groups=submitted_groups,
            )
        except ExportTemplateValidationError as exc:
            try:
                template = load_export_template_detail(workspace_context, template_id=template_id)
                burst_pick = load_export_template_burst_pick(workspace_context, template_id=template_id)
            except ExportTemplateValidationError as page_exc:
                return templates.TemplateResponse(
                    request=request,
                    name="export_template_burst_pick.html",
                    context={
                        "page_title": "连拍挑选",
                        "error_message": str(page_exc),
                        "form_feedback": {"level": "error", "message": str(page_exc)},
                    },
                    status_code=400,
                )
            except ExportTemplateError as page_exc:
                raise HTTPException(status_code=500, detail=str(page_exc)) from page_exc
            return templates.TemplateResponse(
                request=request,
                name="export_template_burst_pick.html",
                context={
                    "page_title": f"连拍挑选：{template.name}",
                    "template": template,
                    "burst_pick": burst_pick,
                    "form_feedback": {"level": "error", "message": str(exc)},
                },
                status_code=423 if exc.code == "export_in_progress" else 400,
            )
        except ExportTemplateError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        return RedirectResponse(url=f"/exports/{template_id}/burst-pick?feedback=saved", status_code=303)

    @app.get("/api/export-templates/{template_id}/burst-pick")
    def api_export_template_burst_pick(template_id: str) -> dict[str, object]:
        try:
            burst_pick = load_export_template_burst_pick(workspace_context, template_id=template_id)
        except ExportTemplateValidationError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except ExportTemplateError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        return _serialize_burst_pick(template_id, burst_pick)

    @app.post("/api/export-templates/{template_id}/burst-pick")
    async def api_export_template_burst_pick_submit(template_id: str, request: Request) -> dict[str, object]:
        try:
            payload = await request.json()
        except Exception as exc:
            raise HTTPException(status_code=400, detail="请求 JSON 无效。") from exc
        groups = payload.get("groups") if isinstance(payload, dict) else None
        if not isinstance(groups, list):
            raise HTTPException(status_code=400, detail="groups 必须是数组。")
        try:
            result = submit_export_template_burst_pick(
                workspace_context,
                template_id=template_id,
                submitted_groups=groups,
            )
        except ExportTemplateValidationError as exc:
            if exc.code == "export_in_progress":
                raise HTTPException(status_code=423, detail=str(exc)) from exc
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except ExportTemplateError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        return {
            "abandoned_asset_ids": result.abandoned_asset_ids,
            "kept_asset_ids": result.kept_asset_ids,
            "created_count": result.created_count,
            "already_abandoned_count": result.already_abandoned_count,
        }

    @app.get("/exports/{template_id}/preview/{asset_id}", response_class=HTMLResponse)
    def export_preview_asset_detail_page(
        request: Request, template_id: str, asset_id: int,
    ) -> Response:
        try:
            detail = load_export_preview_asset_detail(
                workspace_context, template_id=template_id, asset_id=asset_id,
            )
        except ExportTemplateValidationError:
            return RedirectResponse(
                url=f"/exports/{template_id}/preview", status_code=303,
            )
        except ExportTemplateError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

        if detail is None:
            return RedirectResponse(
                url=f"/exports/{template_id}/preview", status_code=303,
            )

        feedback = None
        cookie_val = request.cookies.get(PREVIEW_EXCLUSION_FEEDBACK_COOKIE)
        if cookie_val:
            if cookie_val in PREVIEW_EXCLUSION_FEEDBACK_MESSAGES:
                feedback = PREVIEW_EXCLUSION_FEEDBACK_MESSAGES[cookie_val]
            elif cookie_val.startswith("validation_error:"):
                feedback = {"level": "error", "message": "排除操作校验失败，请刷新页面后重试。"}

        response = templates.TemplateResponse(
            request=request,
            name="export_preview_asset_detail.html",
            context={
                "page_title": f"{detail.file_name} - 照片人物详情",
                "detail": detail,
                "feedback": feedback,
            },
        )
        if cookie_val:
            response.delete_cookie(PREVIEW_EXCLUSION_FEEDBACK_COOKIE, path="/")
        return response

    @app.post("/exports/{template_id}/preview/{asset_id}/exclude")
    async def export_preview_asset_exclude(
        request: Request, template_id: str, asset_id: int,
    ) -> Response:
        body = await request.body()
        form_data = parse_qs(body.decode("utf-8"), keep_blank_values=True)
        person_id = form_data.get("person_id", [""])[0]
        assignment_ids = form_data.get("assignment_id", [])

        redirect_url = f"/exports/{template_id}/preview/{asset_id}"

        try:
            result = submit_person_exclusions(
                workspace_context,
                person_id=person_id,
                assignment_ids=[str(aid) for aid in assignment_ids],
            )
        except ExportTemplateValidationError as exc:
            if exc.code == "export_in_progress":
                response = RedirectResponse(url=redirect_url, status_code=303)
                response.set_cookie(
                    PREVIEW_EXCLUSION_FEEDBACK_COOKIE,
                    "export_in_progress",
                    httponly=True, samesite="lax", path="/",
                )
                return response
            raise
        except PersonExclusionValidationError as exc:
            response = RedirectResponse(url=redirect_url, status_code=303)
            response.set_cookie(
                PREVIEW_EXCLUSION_FEEDBACK_COOKIE,
                f"validation_error:{exc.code}",
                httponly=True, samesite="lax", path="/",
            )
            return response
        except PeopleGalleryError:
            response = RedirectResponse(url=redirect_url, status_code=303)
            response.set_cookie(
                PREVIEW_EXCLUSION_FEEDBACK_COOKIE,
                "internal_error",
                httponly=True, samesite="lax", path="/",
            )
            return response

        if result.remaining_sample_count > 0:
            feedback_code = "exclude_succeeded"
        else:
            feedback_code = "person_removed"

        response = RedirectResponse(url=redirect_url, status_code=303)
        response.set_cookie(
            PREVIEW_EXCLUSION_FEEDBACK_COOKIE,
            feedback_code,
            httponly=True, samesite="lax", path="/",
        )
        return response

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
        error_message = request.query_params.get("error")
        return templates.TemplateResponse(
            request=request,
            name="export_template_execute.html",
            context={
                "page_title": f"执行导出：{template.name}",
                "template": template,
                "preview": preview,
                "error_message": error_message,
            },
        )

    @app.post("/exports/{template_id}/execute")
    def export_template_execute_action(template_id: str) -> RedirectResponse:
        try:
            execute_export_async(workspace_context, template_id=template_id)
        except ExportTemplateValidationError as exc:
            params = urlencode({"error": str(exc)})
            return RedirectResponse(
                url=f"/exports/{template_id}/execute?{params}", status_code=303
            )
        except ExportTemplateError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"导出执行失败：{exc}") from exc
        return RedirectResponse(url=f"/exports/{template_id}/history", status_code=303)

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


def _serialize_burst_pick(template_id: str, burst_pick: object) -> dict[str, object]:
    return {
        "template_id": template_id,
        "groups": [
            {
                "group_key": group.group_key,
                "assets": [
                    {
                        "asset_id": asset.asset_id,
                        "file_name": asset.file_name,
                        "bucket": asset.bucket,
                        "month": asset.month,
                        "context_url": asset.context_url,
                        "is_live": asset.is_live,
                    }
                    for asset in group.assets
                ],
                "match_evidence": {
                    "algorithm": "visual_fingerprint_v1",
                    "edges": [
                        {
                            "asset_ids": list(edge.asset_ids),
                            "threshold": edge.threshold,
                            "metadata_assisted": edge.metadata_assisted,
                            "dhash_hamming": edge.dhash_hamming,
                            "luminance_cosine": edge.luminance_cosine,
                            "color_histogram_intersection": edge.color_histogram_intersection,
                            "capture_time_delta_seconds": edge.capture_time_delta_seconds,
                            "normalized_device_match": edge.normalized_device_match,
                        }
                        for edge in group.edges
                    ],
                },
            }
            for group in burst_pick.groups
        ],
        "diagnostics": {
            "skipped_missing_or_unreadable_count": burst_pick.skipped_missing_or_unreadable_count,
        },
    }


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


def _get_exports_feedback(request: Request) -> dict[str, str] | None:
    feedback_code = request.cookies.get(EXPORTS_FEEDBACK_COOKIE)
    if feedback_code is None:
        return None
    return EXPORTS_FEEDBACK_MESSAGES.get(feedback_code)
