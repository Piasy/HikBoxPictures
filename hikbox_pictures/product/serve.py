from __future__ import annotations

from pathlib import Path
import socket

import uvicorn

from hikbox_pictures.product.export_templates import cleanup_stale_export_runs
from hikbox_pictures.product.export_templates import ExportTemplateError
from hikbox_pictures.product.people_gallery import PeopleGalleryError
from hikbox_pictures.product.people_gallery import ensure_webui_schema_ready
from hikbox_pictures.product.sources import load_workspace_context
from hikbox_pictures.product.workspace_runtime import acquire_workspace_operation_lock
from hikbox_pictures.product.workspace_runtime import WorkspaceOperationLockError
from hikbox_pictures.web.app import create_people_gallery_app


class ServeStartError(RuntimeError):
    """WebUI 启动失败。"""


def serve_workspace(
    *,
    workspace: Path,
    port: int,
    person_detail_page_size: int,
) -> None:
    try:
        _ensure_valid_port(port)
        workspace_context = load_workspace_context(workspace)
        ensure_webui_schema_ready(workspace_context)
        cleanup_stale_export_runs(workspace_context)
    except (PeopleGalleryError, ExportTemplateError) as exc:
        raise ServeStartError(str(exc)) from exc

    try:
        with acquire_workspace_operation_lock(
            workspace_context=workspace_context,
            operation_name="serve",
        ):
            _ensure_port_available(port)
            app = create_people_gallery_app(
                workspace_context=workspace_context,
                person_detail_page_size=person_detail_page_size,
            )
            uvicorn.run(
                app,
                host="127.0.0.1",
                port=port,
                access_log=False,
                log_level="warning",
                server_header=False,
            )
    except WorkspaceOperationLockError as exc:
        raise ServeStartError(str(exc)) from exc


def _ensure_port_available(port: int) -> None:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            probe.bind(("127.0.0.1", port))
    except OSError as exc:
        raise ServeStartError(f"目标端口已被占用：127.0.0.1:{port}") from exc


def _ensure_valid_port(port: int) -> None:
    if port < 1 or port > 65535:
        raise ServeStartError(f"端口必须在 1-65535 之间：{port}")
