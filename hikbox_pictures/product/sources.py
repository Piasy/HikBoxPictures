from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import sqlite3


class WorkspaceAccessError(RuntimeError):
    """工作区访问失败。"""


class SourceRegistryError(RuntimeError):
    """源目录登记或查询失败。"""


@dataclass(frozen=True)
class WorkspaceContext:
    workspace_path: Path
    external_root_path: Path
    library_db_path: Path


def add_source(
    *,
    workspace: Path,
    source_path: Path,
    label: str,
    command_args: list[str],
) -> None:
    workspace_context = load_workspace_context(workspace)
    source_dir_path = _resolve_source_directory(source_path)
    if not label.strip():
        raise SourceRegistryError("参数错误: --label 不能为空白。")

    created_at = _utc_now_text()
    try:
        connection = sqlite3.connect(workspace_context.library_db_path)
    except sqlite3.Error as exc:
        raise WorkspaceAccessError(
            f"工作区数据库无法打开：{workspace_context.library_db_path}"
        ) from exc
    try:
        with connection:
            connection.execute(
                """
                INSERT INTO library_sources (path, label, active, created_at)
                VALUES (?, ?, 1, ?)
                """,
                (str(source_dir_path), label, created_at),
            )
            _append_source_log(
                workspace_context=workspace_context,
                payload={
                    "timestamp": _utc_now_text(),
                    "command": " ".join(["hikbox", *command_args]),
                    "workspace": str(workspace_context.workspace_path),
                    "source_path": str(source_dir_path),
                    "label": label,
                    "result": "success",
                },
            )
    except sqlite3.IntegrityError as exc:
        raise SourceRegistryError(f"源目录已存在：{source_dir_path}") from exc
    except sqlite3.Error as exc:
        raise SourceRegistryError(f"源目录写入失败：{source_dir_path}") from exc
    except OSError as exc:
        raise SourceRegistryError(
            f"source 日志写入失败：{workspace_context.external_root_path / 'logs'}"
        ) from exc
    finally:
        connection.close()


def list_sources(*, workspace: Path) -> list[dict[str, object]]:
    workspace_context = load_workspace_context(workspace)
    try:
        connection = sqlite3.connect(workspace_context.library_db_path)
    except sqlite3.Error as exc:
        raise WorkspaceAccessError(
            f"工作区数据库无法打开：{workspace_context.library_db_path}"
        ) from exc
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            """
            SELECT id, label, path, active, created_at
            FROM library_sources
            ORDER BY id ASC
            """
        ).fetchall()
    except sqlite3.Error as exc:
        raise SourceRegistryError(
            f"源目录读取失败：{workspace_context.library_db_path}"
        ) from exc
    finally:
        connection.close()

    return [
        {
            "id": int(row["id"]),
            "label": str(row["label"]),
            "path": str(row["path"]),
            "active": bool(row["active"]),
            "created_at": str(row["created_at"]),
        }
        for row in rows
    ]


def load_workspace_context(workspace: Path) -> WorkspaceContext:
    workspace_path = workspace.resolve()
    hikbox_dir = workspace_path / ".hikbox"
    config_path = hikbox_dir / "config.json"
    library_db_path = hikbox_dir / "library.db"

    if not config_path.is_file() or not library_db_path.is_file():
        raise WorkspaceAccessError(
            f"工作区未初始化或缺少必要文件：{workspace_path}"
        )

    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WorkspaceAccessError(f"工作区配置无法读取：{config_path}") from exc

    external_root = config.get("external_root")
    if not isinstance(external_root, str) or not external_root.strip():
        raise WorkspaceAccessError(f"工作区配置缺少 external_root：{config_path}")

    return WorkspaceContext(
        workspace_path=workspace_path,
        external_root_path=Path(external_root),
        library_db_path=library_db_path,
    )


def _resolve_source_directory(source_path: Path) -> Path:
    source_dir_path = source_path.resolve()
    if not source_dir_path.exists():
        raise SourceRegistryError(f"源目录不存在：{source_dir_path}")
    if not source_dir_path.is_dir():
        raise SourceRegistryError(f"源目录不是目录：{source_dir_path}")
    if not os.access(source_dir_path, os.R_OK | os.X_OK):
        raise SourceRegistryError(f"源目录不可读：{source_dir_path}")
    return source_dir_path


def _append_source_log(
    *,
    workspace_context: WorkspaceContext,
    payload: dict[str, object],
) -> None:
    logs_dir = workspace_context.external_root_path / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    with (logs_dir / "source.log.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False) + "\n")


def _utc_now_text() -> str:
    return datetime.now(tz=UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
