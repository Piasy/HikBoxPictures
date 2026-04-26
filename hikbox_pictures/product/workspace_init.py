from __future__ import annotations

from datetime import UTC, datetime
import json
from pathlib import Path
import shutil
import sqlite3


class WorkspaceInitializationError(RuntimeError):
    """工作区初始化失败。"""


def initialize_workspace(
    *,
    workspace: Path,
    external_root: Path,
    command_args: list[str],
) -> None:
    workspace_path = workspace.resolve()
    external_root_path = external_root.resolve()
    hikbox_dir = workspace_path / ".hikbox"
    config_path = hikbox_dir / "config.json"
    library_db_path = hikbox_dir / "library.db"
    embedding_db_path = hikbox_dir / "embedding.db"
    external_root_preexisting_entries: set[str] | None = None

    _ensure_workspace_is_fresh(
        hikbox_dir=hikbox_dir,
        config_path=config_path,
        library_db_path=library_db_path,
        embedding_db_path=embedding_db_path,
    )

    cleanup_roots: list[Path] = []
    try:
        external_root_preexisting_entries = _inspect_external_root(external_root_path)
        _mkdir_with_tracking(hikbox_dir)
        _create_external_directories(external_root_path, cleanup_roots)

        _write_json(
            config_path,
            {
                "config_version": 1,
                "external_root": str(external_root_path),
            },
        )

        _initialize_database(
            db_path=library_db_path,
            sql_path=Path(__file__).resolve().parent / "db" / "sql" / "library_v1.sql",
        )

        _initialize_database(
            db_path=embedding_db_path,
            sql_path=Path(__file__).resolve().parent / "db" / "sql" / "embedding_v1.sql",
        )

        log_path = external_root_path / "logs" / "init.log.jsonl"

        _append_success_log(
            log_path=log_path,
            command_args=command_args,
            workspace_path=workspace_path,
            external_root_path=external_root_path,
        )
    except Exception as exc:  # noqa: BLE001
        _rollback_initialization(
            hikbox_dir=hikbox_dir,
            external_root_path=external_root_path,
            cleanup_roots=cleanup_roots,
            external_root_preexisting_entries=external_root_preexisting_entries,
        )
        raise WorkspaceInitializationError(str(exc)) from exc


def _ensure_workspace_is_fresh(
    *,
    hikbox_dir: Path,
    config_path: Path,
    library_db_path: Path,
    embedding_db_path: Path,
) -> None:
    for path in (hikbox_dir, config_path, library_db_path, embedding_db_path):
        if path.exists():
            raise WorkspaceInitializationError(f"目标工作区已存在初始化产物：{path}")


def _mkdir_with_tracking(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=False)


def _inspect_external_root(external_root_path: Path) -> set[str] | None:
    if not external_root_path.exists():
        return None
    if not external_root_path.is_dir():
        raise RuntimeError(f"external_root 不是目录：{external_root_path}")
    try:
        return {child.name for child in external_root_path.iterdir()}
    except OSError as exc:
        raise RuntimeError(f"external_root 无法访问：{external_root_path}: {exc}") from exc


def _create_external_directories(external_root_path: Path, cleanup_roots: list[Path]) -> None:
    try:
        _mkdir_and_register_cleanup_root(
            external_root_path / "artifacts" / "crops",
            external_root_path,
            cleanup_roots,
        )
        _mkdir_and_register_cleanup_root(
            external_root_path / "artifacts" / "context",
            external_root_path,
            cleanup_roots,
        )
        _mkdir_and_register_cleanup_root(
            external_root_path / "logs",
            external_root_path,
            cleanup_roots,
        )
    except OSError as exc:
        raise RuntimeError(f"external_root 创建失败：{external_root_path}: {exc}") from exc


def _mkdir_and_register_cleanup_root(
    target_path: Path,
    external_root_path: Path,
    cleanup_roots: list[Path],
) -> None:
    cleanup_candidate = _compute_cleanup_candidate(target_path)
    target_path.mkdir(parents=True, exist_ok=False)
    if cleanup_candidate is not None:
        _register_cleanup_root(
            cleanup_roots=cleanup_roots,
            candidate=cleanup_candidate,
            protected_root=external_root_path,
        )


def _compute_cleanup_candidate(target_path: Path) -> Path | None:
    missing_paths: list[Path] = []
    current = target_path
    while not current.exists():
        missing_paths.append(current)
        current = current.parent
    if not missing_paths:
        return None
    return missing_paths[-1]


def _register_cleanup_root(
    *,
    cleanup_roots: list[Path],
    candidate: Path,
    protected_root: Path,
) -> None:
    if any(existing == protected_root or candidate.is_relative_to(existing) for existing in cleanup_roots):
        return
    cleanup_roots[:] = [
        existing for existing in cleanup_roots if not existing.is_relative_to(candidate)
    ]
    cleanup_roots.append(candidate)


def _write_json(path: Path, payload: dict[str, object]) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _initialize_database(*, db_path: Path, sql_path: Path) -> None:
    connection = sqlite3.connect(db_path)
    try:
        with connection:
            connection.executescript(sql_path.read_text(encoding="utf-8"))
    finally:
        connection.close()


def _append_success_log(
    *,
    log_path: Path,
    command_args: list[str],
    workspace_path: Path,
    external_root_path: Path,
) -> None:
    payload = {
        "timestamp": _utc_now_text(),
        "command": " ".join(["hikbox-pictures", *command_args]),
        "workspace": str(workspace_path),
        "external_root": str(external_root_path),
        "result": "success",
    }
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False) + "\n")


def _rollback_initialization(
    *,
    hikbox_dir: Path,
    external_root_path: Path,
    cleanup_roots: list[Path],
    external_root_preexisting_entries: set[str] | None,
) -> None:
    if hikbox_dir.exists():
        shutil.rmtree(hikbox_dir, ignore_errors=True)
    unique_roots = sorted(
        dict.fromkeys(cleanup_roots),
        key=lambda path: (len(path.parts), str(path)),
        reverse=True,
    )
    for root_path in unique_roots:
        if not root_path.exists():
            continue
        if root_path.is_dir():
            shutil.rmtree(root_path, ignore_errors=True)
        else:
            root_path.unlink(missing_ok=True)
    if external_root_preexisting_entries is None:
        if external_root_path.exists():
            shutil.rmtree(external_root_path, ignore_errors=True)
        return
    _remove_new_top_level_entries(
        root_path=external_root_path,
        original_entry_names=external_root_preexisting_entries,
    )


def _remove_new_top_level_entries(*, root_path: Path, original_entry_names: set[str]) -> None:
    if not root_path.is_dir():
        return
    for child in root_path.iterdir():
        if child.name in original_entry_names:
            continue
        if child.is_dir():
            shutil.rmtree(child, ignore_errors=True)
        else:
            child.unlink(missing_ok=True)


def _utc_now_text() -> str:
    return datetime.now(tz=UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
