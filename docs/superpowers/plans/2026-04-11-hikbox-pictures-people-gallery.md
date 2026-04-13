# HikBox Pictures 人物图库系统 Implementation Plan（可执行重构版）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax. This plan's checkbox state is the persistent progress source of truth; TodoWrite is session-local tracking. Executors may run dependency-free tasks in parallel (default max concurrency: 4).

**Goal:** 交付与 `docs/superpowers/specs/2026-04-11-hikbox-pictures-people-gallery-design.md` 对齐的首版本地人物图库系统，覆盖建库、增量更新、中断恢复、ANN 召回、人物维护、智能导出与可观测体系。

**Architecture:** 采用三层架构：`library.db` 保存业务真相，`artifacts/` 保存可重建派生物（ANN/缩略图/裁剪），`logs/` 保存可轮转结构化日志。系统先落地工作区和迁移框架，再实现 CLI 控制面和 API/Web 骨架，随后完成扫描引擎、人物真相层、导出账本、日志索引与 WebUI 工作台，最终以端到端回归封口。

**Tech Stack:** Python 3.13+、DeepFace、FastAPI、Jinja2、uvicorn、SQLite、hnswlib、pytest

---

## 实现约束（必须同时满足）

1. 本计划是“完整人物图库系统计划”，不是“WebUI 子项目计划”。
2. 任何页面/路由实现前，必须先完成依赖、目录、迁移、工作区骨架。
3. CLI 必须升级为控制面命令集：`init/source/serve/scan/rebuild-artifacts/export run/logs tail/logs prune`。
4. 扫描必须支持多 source、source 级阶段推进、checkpoint、heartbeat、owner 失联回收、默认恢复最近未完成会话。
5. 导出必须实现真实命中计算、`spec_hash`、`export_delivery` 账本、`stale` 标记、Live Photo `MOV` 补齐逻辑。
6. 日志必须实现“结构化文件日志 + `ops_event` 索引”双层方案，且包含保留/清理策略。
7. WebUI 与 API 必须绑定真实 workspace 数据，不接受固定空数组/固定计数占位。
8. 并行只允许在写入集合无冲突时进行，共享文件必须串行。
9. 所有测试命令使用 `.venv`：`source .venv/bin/activate && PYTHONPATH=src python3 -m pytest ...`。
10. 新增或修改的文档与代码注释必须使用中文。

## Spec 覆盖映射

- 基础设施与控制面（spec: 175-194）: Task 1-4
- 扫描/恢复/幂等（spec: 241-337, 581-620, 787）: Task 5-6
- 人物真相与审核闭环（spec: 395-482, 127）: Task 7
- ANN 与性能路径（spec: 417, 460, 631-648）: Task 8
- 导出命中/账本/补齐（spec: 508-697）: Task 9
- 可观测体系（spec: 550, 707-781）: Task 10
- WebUI 信息架构（spec: 117-163）: Task 11
- 总验收与收口（spec 全量）: Task 12

## Parallel Execution Plan

### Wave A（地基，串行）

- 顺序执行：`Task 1 -> Task 2 -> Task 3 -> Task 4`
- 原因：后续任务统一依赖工作区、迁移、仓储层和控制面入口。
- 阻塞项：`Task 5-12`。

### Wave B（并行波次 1）

- 并行执行：`Task 5` 与 `Task 9`
- 并行依据：
- `Task 5` 写入 `scan` 相关文件集合（`services/scan_*`、`repositories/scan_repo.py`、`tests/people_gallery/test_scan_*.py`）。
- `Task 9` 写入 `export` 相关文件集合（`services/export_*`、`repositories/export_repo.py`、`tests/people_gallery/test_export_*.py`）。
- 两任务均依赖 `Task 4`，且写入集合不重叠。
- 阻塞项：`Task 6` 依赖 Task 5，`Task 10` 依赖 Task 5+9。

### Wave C（核心串行）

- 顺序执行：`Task 6 -> Task 7 -> Task 8`
- 原因：先稳定资产阶段流水线，再落人物真相，再接 ANN 召回。
- 阻塞项：`Task 11`。

### Wave D（可观测收敛，串行）

- 顺序执行：`Task 10`
- 原因：日志打点需要 scan/export 两条主链先落地。
- 阻塞项：`Task 11` 与 `Task 12`。

### Wave E（交互层，串行）

- 顺序执行：`Task 11`
- 原因：WebUI 需要一次性绑定已落地的数据与动作接口，避免重复返工。
- 阻塞项：`Task 12`。

### Wave F（验收，串行）

- 顺序执行：`Task 12`
- 原因：统一端到端回归、文档更新与计划收口。

---

## 基础设施面

### Task 1: 工作区、依赖与迁移框架

**Depends on:** None

**Scope Budget:**
- Max files: 20
- Estimated files touched: 7
- Max added lines: 1000
- Estimated added lines: 620

**Files:**
- Modify: `pyproject.toml`
- Create: `src/hikbox_pictures/workspace.py`
- Create: `src/hikbox_pictures/db/__init__.py`
- Create: `src/hikbox_pictures/db/connection.py`
- Create: `src/hikbox_pictures/db/migrator.py`
- Create: `src/hikbox_pictures/db/migrations/0001_people_gallery.sql`
- Create: `tests/people_gallery/test_workspace_bootstrap.py`

- [x] **Step 1: 先写失败测试，锁定工作区布局与全量核心表**

```python
from pathlib import Path

from hikbox_pictures.db.connection import connect_db
from hikbox_pictures.db.migrator import apply_migrations
from hikbox_pictures.workspace import ensure_workspace_layout


def test_workspace_layout_and_tables(tmp_path: Path) -> None:
    paths = ensure_workspace_layout(tmp_path)
    assert paths.db_path == tmp_path / ".hikbox" / "library.db"
    assert (tmp_path / ".hikbox" / "artifacts" / "ann").exists()
    assert (tmp_path / ".hikbox" / "logs" / "runs").exists()

    conn = connect_db(paths.db_path)
    apply_migrations(conn)
    table_names = {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }

    required = {
        "library_source", "scan_session", "scan_session_source", "scan_checkpoint",
        "photo_asset", "face_observation", "face_embedding", "auto_cluster_batch",
        "auto_cluster", "auto_cluster_member", "person", "person_face_assignment",
        "person_prototype", "review_item", "export_template", "export_template_person",
        "export_run", "export_delivery", "ops_event",
    }
    assert required <= table_names
```

- [x] **Step 2: 运行测试，确认失败**

Run: `source .venv/bin/activate && PYTHONPATH=src python3 -m pytest tests/people_gallery/test_workspace_bootstrap.py -v`
Expected: FAIL（模块或表不存在）。

- [x] **Step 3: 落地 workspace/migration 与依赖声明**

```toml
# pyproject.toml（新增依赖）
dependencies = [
  "deepface>=0.0.93",
  "tf-keras>=2.21.0",
  "numpy>=1.26.0",
  "Pillow>=10.0.0",
  "pillow-heif>=0.18.0",
  "fastapi>=0.116.0",
  "jinja2>=3.1.4",
  "uvicorn>=0.30.0",
  "hnswlib>=0.8.0",
]
```

```python
# src/hikbox_pictures/workspace.py
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class WorkspacePaths:
    root: Path
    db_path: Path
    artifacts_dir: Path
    logs_dir: Path
    exports_dir: Path


def ensure_workspace_layout(root: Path) -> WorkspacePaths:
    root = root.expanduser().resolve()
    hikbox = root / ".hikbox"
    db_path = hikbox / "library.db"
    artifacts = hikbox / "artifacts"
    logs = hikbox / "logs"
    exports = hikbox / "exports"

    for path in [hikbox, artifacts / "ann", artifacts / "thumbs", artifacts / "face-crops", logs / "runs", exports]:
        path.mkdir(parents=True, exist_ok=True)

    return WorkspacePaths(root=root, db_path=db_path, artifacts_dir=artifacts, logs_dir=logs, exports_dir=exports)
```

- [x] **Step 4: 运行回归，确认迁移幂等与表结构可用**

Run: `source .venv/bin/activate && PYTHONPATH=src python3 -m pytest tests/people_gallery/test_workspace_bootstrap.py -q`
Expected: PASS。

**Task completion action (not a checkbox step): Commit task changes and plan progress**

```bash
git add pyproject.toml src/hikbox_pictures/workspace.py src/hikbox_pictures/db/__init__.py src/hikbox_pictures/db/connection.py src/hikbox_pictures/db/migrator.py src/hikbox_pictures/db/migrations/0001_people_gallery.sql tests/people_gallery/test_workspace_bootstrap.py docs/superpowers/plans/2026-04-11-hikbox-pictures-people-gallery.md
git commit -m "feat: bootstrap workspace and migration framework (Task 1)"
```

### Task 2: 仓储层与可复用 seed 夹具

**Depends on:** Task 1

**Scope Budget:**
- Max files: 20
- Estimated files touched: 10
- Max added lines: 1000
- Estimated added lines: 820

**Files:**
- Create: `src/hikbox_pictures/repositories/__init__.py`
- Create: `src/hikbox_pictures/repositories/source_repo.py`
- Create: `src/hikbox_pictures/repositories/scan_repo.py`
- Create: `src/hikbox_pictures/repositories/asset_repo.py`
- Create: `src/hikbox_pictures/repositories/person_repo.py`
- Create: `src/hikbox_pictures/repositories/review_repo.py`
- Create: `src/hikbox_pictures/repositories/export_repo.py`
- Create: `src/hikbox_pictures/repositories/ops_event_repo.py`
- Create: `tests/people_gallery/fixtures_workspace.py`
- Create: `tests/people_gallery/test_repository_contract.py`

- [x] **Step 1: 写失败测试，锁定 repo 合同与 seed 可用性**

```python
from tests.people_gallery.fixtures_workspace import build_seed_workspace


def test_seed_workspace_counts(tmp_path):
    ws = build_seed_workspace(tmp_path)
    counts = ws.counts()

    assert counts["library_source"] == 2
    assert counts["person"] >= 3
    assert counts["review_item"] >= 4
    assert counts["export_template"] >= 1


def test_latest_resumable_scan_session(tmp_path):
    ws = build_seed_workspace(tmp_path)
    latest = ws.scan_repo.latest_resumable_session()

    assert latest is not None
    assert latest["status"] in {"running", "paused", "interrupted", "pending"}
```

- [x] **Step 2: 运行测试，确认失败**

Run: `source .venv/bin/activate && PYTHONPATH=src python3 -m pytest tests/people_gallery/test_repository_contract.py -v`
Expected: FAIL。

- [x] **Step 3: 实现仓储层与 seed 夹具**

```python
# tests/people_gallery/fixtures_workspace.py（关键片段）
conn.execute("INSERT INTO library_source(name, root_path, root_fingerprint, active) VALUES ('iCloud', '/data/a', 'fp-a', 1)")
conn.execute("INSERT INTO library_source(name, root_path, root_fingerprint, active) VALUES ('NAS', '/data/b', 'fp-b', 1)")
conn.execute("INSERT INTO scan_session(mode, status, started_at, heartbeat_at, owner_pid) VALUES ('incremental', 'paused', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, 99999)")
conn.execute("INSERT INTO person(display_name, status, confirmed, ignored) VALUES ('人物A', 'active', 1, 0)")
conn.execute("INSERT INTO review_item(review_type, payload_json, priority, status) VALUES ('new_person', '{}', 10, 'open')")
conn.execute("INSERT INTO export_template(name, output_root, include_group, export_live_mov, enabled) VALUES ('家庭模板', '/tmp/out', 1, 1, 1)")
```

```python
# src/hikbox_pictures/repositories/scan_repo.py（关键片段）
def latest_resumable_session(self):
    return self.conn.execute(
        """
        SELECT id, mode, status, started_at, heartbeat_at
        FROM scan_session
        WHERE status IN ('pending', 'running', 'paused', 'interrupted')
          AND COALESCE(abandoned, 0) = 0
        ORDER BY id DESC
        LIMIT 1
        """
    ).fetchone()
```

- [x] **Step 4: 运行仓储层回归**

Run: `source .venv/bin/activate && PYTHONPATH=src python3 -m pytest tests/people_gallery/test_repository_contract.py -q`
Expected: PASS。

**Task completion action (not a checkbox step): Commit task changes and plan progress**

```bash
git add src/hikbox_pictures/repositories/__init__.py src/hikbox_pictures/repositories/source_repo.py src/hikbox_pictures/repositories/scan_repo.py src/hikbox_pictures/repositories/asset_repo.py src/hikbox_pictures/repositories/person_repo.py src/hikbox_pictures/repositories/review_repo.py src/hikbox_pictures/repositories/export_repo.py src/hikbox_pictures/repositories/ops_event_repo.py tests/people_gallery/fixtures_workspace.py tests/people_gallery/test_repository_contract.py docs/superpowers/plans/2026-04-11-hikbox-pictures-people-gallery.md
git commit -m "feat: add repository layer and seed workspace fixtures (Task 2)"
```

### Task 3: CLI 控制面与 API 启动骨架

**Depends on:** Task 2

**Scope Budget:**
- Max files: 20
- Estimated files touched: 9
- Max added lines: 1000
- Estimated added lines: 760

**Files:**
- Modify: `src/hikbox_pictures/cli.py`
- Create: `src/hikbox_pictures/api/__init__.py`
- Create: `src/hikbox_pictures/api/app.py`
- Create: `src/hikbox_pictures/api/routes_health.py`
- Create: `src/hikbox_pictures/services/runtime.py`
- Create: `tests/people_gallery/test_cli_control_plane.py`
- Create: `tests/people_gallery/test_api_bootstrap.py`
- Modify: `README.md`
- Modify: `pyproject.toml`

- [x] **Step 1: 写失败测试，锁定控制面命令与 app 启动行为**

```python
from pathlib import Path

from hikbox_pictures.cli import main


def test_cli_init_creates_workspace_and_db(tmp_path: Path) -> None:
    rc = main(["init", "--workspace", str(tmp_path)])
    assert rc == 0
    assert (tmp_path / ".hikbox" / "library.db").exists()


def test_cli_help_contains_control_plane_commands(capsys) -> None:
    rc = main(["--help"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "source" in out
    assert "scan" in out
    assert "serve" in out
    assert "logs" in out
```

```python
from fastapi.testclient import TestClient

from hikbox_pictures.api.app import create_app
from tests.people_gallery.fixtures_workspace import build_seed_workspace


def test_create_app_binds_workspace_and_health_route(tmp_path):
    ws = build_seed_workspace(tmp_path)
    client = TestClient(create_app(workspace=ws.root))

    resp = client.get("/api/health")
    assert resp.status_code == 200
    assert resp.json()["workspace"].endswith(str(ws.root))
```

- [x] **Step 2: 运行测试，确认先失败**

Run: `source .venv/bin/activate && PYTHONPATH=src python3 -m pytest tests/people_gallery/test_cli_control_plane.py tests/people_gallery/test_api_bootstrap.py -v`
Expected: FAIL（缺少子命令与 app 骨架）。

- [x] **Step 3: 实现 CLI 子命令树与 FastAPI 启动骨架**

```python
# src/hikbox_pictures/cli.py（关键片段）
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="hikbox-pictures")
    sub = parser.add_subparsers(dest="command", required=True)

    p_init = sub.add_parser("init")
    p_init.add_argument("--workspace", type=Path, required=True)

    p_source = sub.add_parser("source")
    source_sub = p_source.add_subparsers(dest="source_command", required=True)
    source_sub.add_parser("list").add_argument("--workspace", type=Path, required=True)

    p_serve = sub.add_parser("serve")
    p_serve.add_argument("--workspace", type=Path, required=True)
    p_serve.add_argument("--host", default="127.0.0.1")
    p_serve.add_argument("--port", type=int, default=7860)

    sub.add_parser("scan")
    sub.add_parser("rebuild-artifacts")
    sub.add_parser("logs")
    sub.add_parser("export")
    return parser
```

```python
# src/hikbox_pictures/api/app.py（关键片段）
def create_app(workspace: Path) -> FastAPI:
    paths = ensure_workspace_layout(workspace)
    conn = connect_db(paths.db_path)
    apply_migrations(conn)

    app = FastAPI(title="HikBox Pictures")
    app.state.workspace = str(paths.root)

    app.include_router(health_router, prefix="/api")
    return app
```

- [x] **Step 4: 运行回归，确认 CLI 与 app 起步可用**

Run: `source .venv/bin/activate && PYTHONPATH=src python3 -m pytest tests/people_gallery/test_cli_control_plane.py tests/people_gallery/test_api_bootstrap.py -q`
Expected: PASS。

**Task completion action (not a checkbox step): Commit task changes and plan progress**

```bash
git add src/hikbox_pictures/cli.py src/hikbox_pictures/api/__init__.py src/hikbox_pictures/api/app.py src/hikbox_pictures/api/routes_health.py src/hikbox_pictures/services/runtime.py tests/people_gallery/test_cli_control_plane.py tests/people_gallery/test_api_bootstrap.py README.md pyproject.toml docs/superpowers/plans/2026-04-11-hikbox-pictures-people-gallery.md
git commit -m "feat: add control-plane cli and api bootstrap skeleton (Task 3)"
```

### Task 4: API 查询/动作分层与统一路由注册

**Depends on:** Task 3

**Scope Budget:**
- Max files: 20
- Estimated files touched: 12
- Max added lines: 1000
- Estimated added lines: 880

**Files:**
- Create: `src/hikbox_pictures/services/web_query_service.py`
- Create: `src/hikbox_pictures/services/action_service.py`
- Create: `src/hikbox_pictures/api/routes_people.py`
- Create: `src/hikbox_pictures/api/routes_reviews.py`
- Create: `src/hikbox_pictures/api/routes_scan.py`
- Create: `src/hikbox_pictures/api/routes_export.py`
- Create: `src/hikbox_pictures/api/routes_logs.py`
- Modify: `src/hikbox_pictures/api/app.py`
- Create: `tests/people_gallery/test_api_contract.py`
- Create: `tests/people_gallery/test_api_actions.py`
- Modify: `tests/people_gallery/fixtures_workspace.py`
- Modify: `README.md`

- [x] **Step 1: 写失败测试，锁定真实读库和动作回写**

```python
from fastapi.testclient import TestClient

from hikbox_pictures.api.app import create_app
from tests.people_gallery.fixtures_workspace import build_seed_workspace


def test_scan_status_reads_real_session(tmp_path):
    ws = build_seed_workspace(tmp_path)
    client = TestClient(create_app(workspace=ws.root))

    data = client.get("/api/scan/status").json()
    assert data["status"] == "paused"
    assert data["session_id"] == 1


def test_people_rename_action_persists(tmp_path):
    ws = build_seed_workspace(tmp_path)
    client = TestClient(create_app(workspace=ws.root))

    resp = client.post("/api/people/1/actions/rename", json={"display_name": "爸爸"})
    assert resp.status_code == 200

    rows = client.get("/api/people").json()
    assert rows[0]["display_name"] == "爸爸"
```

- [x] **Step 2: 运行测试，确认先失败**

Run: `source .venv/bin/activate && PYTHONPATH=src python3 -m pytest tests/people_gallery/test_api_contract.py tests/people_gallery/test_api_actions.py -v`
Expected: FAIL。

- [x] **Step 3: 实现 query/action service 并接入路由**

```python
# src/hikbox_pictures/services/web_query_service.py（关键片段）
class WebQueryService:
    def list_people(self) -> list[dict]:
        rows = self.conn.execute(
            """
            SELECT p.id, p.display_name, p.status, p.confirmed, p.ignored,
                   COUNT(a.id) AS assignment_count
            FROM person p
            LEFT JOIN person_face_assignment a ON a.person_id = p.id AND a.active = 1
            WHERE p.status != 'merged'
            GROUP BY p.id
            ORDER BY p.updated_at DESC, p.id DESC
            """
        ).fetchall()
        return [dict(row) for row in rows]
```

```python
# src/hikbox_pictures/services/action_service.py（关键片段）
class ActionService:
    def rename_person(self, person_id: int, display_name: str) -> None:
        self.conn.execute(
            "UPDATE person SET display_name=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (display_name.strip(), int(person_id)),
        )
        self.conn.commit()
```

- [x] **Step 4: 运行 API 回归，确认真实数据与动作闭环**

Run: `source .venv/bin/activate && PYTHONPATH=src python3 -m pytest tests/people_gallery/test_api_contract.py tests/people_gallery/test_api_actions.py -q`
Expected: PASS。

**Task completion action (not a checkbox step): Commit task changes and plan progress**

```bash
git add src/hikbox_pictures/services/web_query_service.py src/hikbox_pictures/services/action_service.py src/hikbox_pictures/api/routes_people.py src/hikbox_pictures/api/routes_reviews.py src/hikbox_pictures/api/routes_scan.py src/hikbox_pictures/api/routes_export.py src/hikbox_pictures/api/routes_logs.py src/hikbox_pictures/api/app.py tests/people_gallery/test_api_contract.py tests/people_gallery/test_api_actions.py tests/people_gallery/fixtures_workspace.py README.md docs/superpowers/plans/2026-04-11-hikbox-pictures-people-gallery.md
git commit -m "feat: add workspace-backed api query and action layers (Task 4)"
```

## 核心引擎面

### Task 5: 多 source 可恢复扫描控制面（会话/heartbeat/checkpoint）

**Depends on:** Task 4

**Scope Budget:**
- Max files: 20
- Estimated files touched: 12
- Max added lines: 1000
- Estimated added lines: 920

**Files:**
- Create: `src/hikbox_pictures/services/scan_orchestrator.py`
- Create: `src/hikbox_pictures/services/scan_recovery.py`
- Modify: `src/hikbox_pictures/repositories/scan_repo.py`
- Modify: `src/hikbox_pictures/repositories/source_repo.py`
- Modify: `src/hikbox_pictures/api/routes_scan.py`
- Modify: `src/hikbox_pictures/cli.py`
- Create: `tests/people_gallery/test_scan_resume_semantics.py`
- Create: `tests/people_gallery/test_scan_owner_reaper.py`
- Modify: `tests/people_gallery/test_api_contract.py`
- Modify: `tests/people_gallery/test_cli_control_plane.py`
- Modify: `tests/people_gallery/fixtures_workspace.py`
- Modify: `README.md`

- [x] **Step 1: 写失败测试，锁定默认恢复语义与 owner 回收**

```python
from hikbox_pictures.services.scan_recovery import mark_stale_running_sessions


def test_scan_resume_uses_latest_unfinished_session(seed_workspace):
    session_id = seed_workspace.scan_repo.start_incremental_or_resume()
    assert session_id == seed_workspace.latest_paused_session_id


def test_stale_running_session_marked_interrupted(seed_workspace):
    changed = mark_stale_running_sessions(seed_workspace.root, stale_after_seconds=1)
    assert changed >= 1
    status = seed_workspace.scan_repo.get_session(seed_workspace.running_session_id)["status"]
    assert status == "interrupted"
```

- [x] **Step 2: 运行测试，确认失败**

Run: `source .venv/bin/activate && PYTHONPATH=src python3 -m pytest tests/people_gallery/test_scan_resume_semantics.py tests/people_gallery/test_scan_owner_reaper.py -v`
Expected: FAIL。

- [x] **Step 3: 实现 scan 会话控制、heartbeat 与 checkpoint 语义**

```python
# src/hikbox_pictures/services/scan_orchestrator.py（关键片段）
class ScanOrchestrator:
    def start_or_resume(self) -> int:
        resumable = self.scan_repo.latest_resumable_session()
        if resumable is not None:
            self.scan_repo.mark_running(resumable["id"], owner_pid=os.getpid())
            return int(resumable["id"])
        session_id = self.scan_repo.create_session(mode="incremental", status="running", owner_pid=os.getpid())
        self.scan_repo.attach_all_active_sources(session_id)
        return session_id

    def write_checkpoint(self, session_source_id: int, phase: str, cursor_json: str, pending_asset_count: int) -> None:
        self.scan_repo.insert_checkpoint(session_source_id, phase, cursor_json, pending_asset_count)
        self.scan_repo.touch_source_heartbeat(session_source_id)
```

```python
# src/hikbox_pictures/services/scan_recovery.py（关键片段）
def mark_stale_running_sessions(workspace: Path, stale_after_seconds: int) -> int:
    repo = ScanRepo(open_workspace_conn(workspace))
    return repo.mark_stale_running_as_interrupted(stale_after_seconds=stale_after_seconds)
```

- [x] **Step 4: 运行回归，确认 scan 控制面可恢复**

Run: `source .venv/bin/activate && PYTHONPATH=src python3 -m pytest tests/people_gallery/test_scan_resume_semantics.py tests/people_gallery/test_scan_owner_reaper.py tests/people_gallery/test_cli_control_plane.py::test_scan_status_command -q`
Expected: PASS。

**Task completion action (not a checkbox step): Commit task changes and plan progress**

```bash
git add src/hikbox_pictures/services/scan_orchestrator.py src/hikbox_pictures/services/scan_recovery.py src/hikbox_pictures/repositories/scan_repo.py src/hikbox_pictures/repositories/source_repo.py src/hikbox_pictures/api/routes_scan.py src/hikbox_pictures/cli.py tests/people_gallery/test_scan_resume_semantics.py tests/people_gallery/test_scan_owner_reaper.py tests/people_gallery/test_api_contract.py tests/people_gallery/test_cli_control_plane.py tests/people_gallery/fixtures_workspace.py README.md docs/superpowers/plans/2026-04-11-hikbox-pictures-people-gallery.md
git commit -m "feat: implement resumable multi-source scan control plane (Task 5)"
```

### Task 6: 资产阶段流水线与幂等推进（metadata/faces/embedding/assignment）

**Depends on:** Task 5

**Scope Budget:**
- Max files: 20
- Estimated files touched: 13
- Max added lines: 1000
- Estimated added lines: 980

**Files:**
- Create: `src/hikbox_pictures/services/asset_pipeline.py`
- Create: `src/hikbox_pictures/services/asset_stage_runner.py`
- Modify: `src/hikbox_pictures/repositories/asset_repo.py`
- Modify: `src/hikbox_pictures/repositories/scan_repo.py`
- Modify: `src/hikbox_pictures/deepface_engine.py`
- Modify: `src/hikbox_pictures/metadata.py`
- Modify: `src/hikbox_pictures/scanner.py`
- Modify: `src/hikbox_pictures/api/routes_scan.py`
- Modify: `src/hikbox_pictures/cli.py`
- Create: `tests/people_gallery/test_asset_stage_idempotency.py`
- Create: `tests/people_gallery/test_scan_session_source_progress.py`
- Modify: `tests/people_gallery/fixtures_workspace.py`
- Modify: `README.md`

- [x] **Step 1: 写失败测试，锁定阶段单调推进与幂等**

```python
def test_asset_stage_progress_is_monotonic(seed_workspace):
    asset_id = seed_workspace.add_new_asset("/data/a/IMG_0001.HEIC", "fp-1")

    seed_workspace.pipeline.run_until(asset_id, stage="metadata_done")
    seed_workspace.pipeline.run_until(asset_id, stage="faces_done")
    seed_workspace.pipeline.run_until(asset_id, stage="embeddings_done")
    seed_workspace.pipeline.run_until(asset_id, stage="assignment_done")

    status = seed_workspace.asset_repo.get(asset_id)["processing_status"]
    assert status == "assignment_done"


def test_repeated_stage_execution_does_not_duplicate_embeddings(seed_workspace):
    asset_id = seed_workspace.add_new_asset("/data/a/IMG_0002.HEIC", "fp-2")
    seed_workspace.pipeline.run_until(asset_id, stage="embeddings_done")
    first_count = seed_workspace.asset_repo.embedding_count(asset_id)

    seed_workspace.pipeline.run_until(asset_id, stage="embeddings_done")
    second_count = seed_workspace.asset_repo.embedding_count(asset_id)

    assert first_count == second_count
```

- [x] **Step 2: 运行测试，确认失败**

Run: `source .venv/bin/activate && PYTHONPATH=src python3 -m pytest tests/people_gallery/test_asset_stage_idempotency.py tests/people_gallery/test_scan_session_source_progress.py -v`
Expected: FAIL。

- [x] **Step 3: 实现资产阶段 runner 与 source 进度更新**

```python
# src/hikbox_pictures/services/asset_stage_runner.py（关键片段）
STAGE_ORDER = ["discovered", "metadata_done", "faces_done", "embeddings_done", "assignment_done"]


def advance_stage(asset_repo, scan_repo, asset_id: int, target_stage: str, session_id: int, session_source_id: int) -> None:
    current = asset_repo.get(asset_id)["processing_status"]
    for stage in STAGE_ORDER[STAGE_ORDER.index(current) + 1 : STAGE_ORDER.index(target_stage) + 1]:
        if stage == "metadata_done":
            asset_repo.ensure_metadata(asset_id)
            scan_repo.bump_source_progress(session_source_id, "metadata_done_count")
        elif stage == "faces_done":
            asset_repo.ensure_face_observations(asset_id)
            scan_repo.bump_source_progress(session_source_id, "faces_done_count")
        elif stage == "embeddings_done":
            asset_repo.ensure_face_embeddings(asset_id)
            scan_repo.bump_source_progress(session_source_id, "embeddings_done_count")
        elif stage == "assignment_done":
            asset_repo.ensure_auto_assignment(asset_id)
            scan_repo.bump_source_progress(session_source_id, "assignment_done_count")
        asset_repo.set_processing_status(asset_id, stage, session_id)
```

- [x] **Step 4: 运行回归，确认幂等与进度统计正确**

Run: `source .venv/bin/activate && PYTHONPATH=src python3 -m pytest tests/people_gallery/test_asset_stage_idempotency.py tests/people_gallery/test_scan_session_source_progress.py tests/people_gallery/test_api_contract.py::test_scan_status_reports_source_progress -q`
Expected: PASS。

**Task completion action (not a checkbox step): Commit task changes and plan progress**

```bash
git add src/hikbox_pictures/services/asset_pipeline.py src/hikbox_pictures/services/asset_stage_runner.py src/hikbox_pictures/repositories/asset_repo.py src/hikbox_pictures/repositories/scan_repo.py src/hikbox_pictures/deepface_engine.py src/hikbox_pictures/metadata.py src/hikbox_pictures/scanner.py src/hikbox_pictures/api/routes_scan.py src/hikbox_pictures/cli.py tests/people_gallery/test_asset_stage_idempotency.py tests/people_gallery/test_scan_session_source_progress.py tests/people_gallery/fixtures_workspace.py README.md docs/superpowers/plans/2026-04-11-hikbox-pictures-people-gallery.md
git commit -m "feat: add idempotent asset stage pipeline and source progress tracking (Task 6)"
```

### Task 7: 人物真相层与审核动作闭环（merge/split/lock/dismiss）

**Depends on:** Task 6

**Scope Budget:**
- Max files: 20
- Estimated files touched: 12
- Max added lines: 1000
- Estimated added lines: 930

**Files:**
- Create: `src/hikbox_pictures/services/person_truth_service.py`
- Create: `src/hikbox_pictures/services/review_workflow_service.py`
- Modify: `src/hikbox_pictures/repositories/person_repo.py`
- Modify: `src/hikbox_pictures/repositories/review_repo.py`
- Modify: `src/hikbox_pictures/repositories/asset_repo.py`
- Modify: `src/hikbox_pictures/services/action_service.py`
- Modify: `src/hikbox_pictures/api/routes_people.py`
- Modify: `src/hikbox_pictures/api/routes_reviews.py`
- Create: `tests/people_gallery/test_person_truth_actions.py`
- Create: `tests/people_gallery/test_review_actions_contract.py`
- Modify: `tests/people_gallery/fixtures_workspace.py`
- Modify: `README.md`

- [x] **Step 1: 写失败测试，锁定人物真相动作和审核处理**

```python
def test_merge_people_marks_source_as_merged(seed_workspace, client):
    resp = client.post("/api/people/2/actions/merge", json={"target_person_id": 1})
    assert resp.status_code == 200

    people = client.get("/api/people").json()
    merged = [p for p in people if p["id"] == 2][0]
    assert merged["status"] == "merged"


def test_lock_assignment_prevents_auto_overwrite(seed_workspace):
    assignment_id = seed_workspace.create_assignment(person_id=1, observation_id=101, locked=True)
    changed = seed_workspace.person_truth_service.try_auto_reassign(assignment_id, candidate_person_id=2)
    assert changed is False


def test_review_dismiss_sets_resolved(seed_workspace, client):
    resp = client.post("/api/reviews/1/actions/dismiss")
    assert resp.status_code == 200
    row = seed_workspace.review_repo.get(1)
    assert row["status"] == "dismissed"
    assert row["resolved_at"] is not None
```

- [x] **Step 2: 运行测试，确认失败**

Run: `source .venv/bin/activate && PYTHONPATH=src python3 -m pytest tests/people_gallery/test_person_truth_actions.py tests/people_gallery/test_review_actions_contract.py -v`
Expected: FAIL。

- [x] **Step 3: 实现 person truth 与 review workflow 服务**

```python
# src/hikbox_pictures/services/person_truth_service.py（关键片段）
class PersonTruthService:
    def merge_people(self, source_person_id: int, target_person_id: int) -> None:
        self.conn.execute(
            "UPDATE person SET status='merged', merged_into_person_id=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (int(target_person_id), int(source_person_id)),
        )
        self.conn.execute(
            "UPDATE person_face_assignment SET person_id=?, assignment_source='merge' WHERE person_id=? AND active=1",
            (int(target_person_id), int(source_person_id)),
        )
        self.conn.commit()

    def try_auto_reassign(self, assignment_id: int, candidate_person_id: int) -> bool:
        row = self.conn.execute("SELECT locked FROM person_face_assignment WHERE id=?", (int(assignment_id),)).fetchone()
        if row is None or int(row["locked"]) == 1:
            return False
        self.conn.execute(
            "UPDATE person_face_assignment SET person_id=?, assignment_source='auto' WHERE id=?",
            (int(candidate_person_id), int(assignment_id)),
        )
        self.conn.commit()
        return True
```

```python
# src/hikbox_pictures/services/review_workflow_service.py（关键片段）
class ReviewWorkflowService:
    def dismiss(self, review_id: int) -> None:
        self.conn.execute(
            "UPDATE review_item SET status='dismissed', resolved_at=CURRENT_TIMESTAMP WHERE id=?",
            (int(review_id),),
        )
        self.conn.commit()
```

- [x] **Step 4: 运行回归，确认人物与审核闭环成立**

Run: `source .venv/bin/activate && PYTHONPATH=src python3 -m pytest tests/people_gallery/test_person_truth_actions.py tests/people_gallery/test_review_actions_contract.py tests/people_gallery/test_api_actions.py::test_people_rename_action_persists_to_db -q`
Expected: PASS。

**Task completion action (not a checkbox step): Commit task changes and plan progress**

```bash
git add src/hikbox_pictures/services/person_truth_service.py src/hikbox_pictures/services/review_workflow_service.py src/hikbox_pictures/repositories/person_repo.py src/hikbox_pictures/repositories/review_repo.py src/hikbox_pictures/repositories/asset_repo.py src/hikbox_pictures/services/action_service.py src/hikbox_pictures/api/routes_people.py src/hikbox_pictures/api/routes_reviews.py tests/people_gallery/test_person_truth_actions.py tests/people_gallery/test_review_actions_contract.py tests/people_gallery/fixtures_workspace.py README.md docs/superpowers/plans/2026-04-11-hikbox-pictures-people-gallery.md
git commit -m "feat: implement person truth model and review workflow actions (Task 7)"
```

### Task 8: ANN 原型召回与阈值分层

**Depends on:** Task 7

**Scope Budget:**
- Max files: 20
- Estimated files touched: 10
- Max added lines: 1000
- Estimated added lines: 880

**Files:**
- Create: `src/hikbox_pictures/ann/__init__.py`
- Create: `src/hikbox_pictures/ann/index_store.py`
- Create: `src/hikbox_pictures/services/prototype_service.py`
- Create: `src/hikbox_pictures/services/ann_assignment_service.py`
- Modify: `src/hikbox_pictures/repositories/person_repo.py`
- Modify: `src/hikbox_pictures/cli.py`
- Modify: `src/hikbox_pictures/services/asset_pipeline.py`
- Create: `tests/people_gallery/test_ann_recall.py`
- Create: `tests/people_gallery/test_threshold_layers.py`
- Modify: `README.md`

- [x] **Step 1: 写失败测试，锁定 ANN 召回与多阈值语义**

```python
def test_ann_returns_topk_person_candidates(seed_workspace):
    seed_workspace.build_person_prototypes()
    candidates = seed_workspace.ann_assignment_service.recall_person_candidates(seed_workspace.observation_embedding, top_k=5)

    assert len(candidates) <= 5
    assert candidates[0]["person_id"] == 1


def test_threshold_layers_route_to_auto_or_review(seed_workspace):
    result_auto = seed_workspace.ann_assignment_service.classify_distance(0.21)
    result_review = seed_workspace.ann_assignment_service.classify_distance(0.31)
    result_reject = seed_workspace.ann_assignment_service.classify_distance(0.45)

    assert result_auto == "auto_assign"
    assert result_review == "review"
    assert result_reject == "new_person_candidate"
```

- [x] **Step 2: 运行测试，确认失败**

Run: `source .venv/bin/activate && PYTHONPATH=src python3 -m pytest tests/people_gallery/test_ann_recall.py tests/people_gallery/test_threshold_layers.py -v`
Expected: FAIL。

- [x] **Step 3: 实现 ANN 索引与阈值分层服务，并接入 `rebuild-artifacts`**

```python
# src/hikbox_pictures/services/ann_assignment_service.py（关键片段）
class AnnAssignmentService:
    def classify_distance(self, distance: float) -> str:
        if distance <= self.auto_assign_threshold:
            return "auto_assign"
        if distance <= self.review_threshold:
            return "review"
        return "new_person_candidate"
```

```python
# src/hikbox_pictures/cli.py（关键片段）
def handle_rebuild_artifacts(args) -> int:
    runtime = Runtime.from_workspace(args.workspace)
    runtime.prototype_service.rebuild_all_person_prototypes()
    runtime.ann_index_store.rebuild_from_db(runtime.person_repo.list_active_prototypes())
    print("ANN 与人物原型重建完成")
    return 0
```

- [x] **Step 4: 运行回归，确认 ANN 路径生效**

Run: `source .venv/bin/activate && PYTHONPATH=src python3 -m pytest tests/people_gallery/test_ann_recall.py tests/people_gallery/test_threshold_layers.py tests/people_gallery/test_cli_control_plane.py::test_rebuild_artifacts_command -q`
Expected: PASS。

**Task completion action (not a checkbox step): Commit task changes and plan progress**

```bash
git add src/hikbox_pictures/ann/__init__.py src/hikbox_pictures/ann/index_store.py src/hikbox_pictures/services/prototype_service.py src/hikbox_pictures/services/ann_assignment_service.py src/hikbox_pictures/repositories/person_repo.py src/hikbox_pictures/cli.py src/hikbox_pictures/services/asset_pipeline.py tests/people_gallery/test_ann_recall.py tests/people_gallery/test_threshold_layers.py README.md docs/superpowers/plans/2026-04-11-hikbox-pictures-people-gallery.md
git commit -m "feat: add ann prototype recall and threshold layering (Task 8)"
```

## 交付与运维面

### Task 9: 导出命中计算、账本与补齐/过期处理

**Depends on:** Task 4

**Scope Budget:**
- Max files: 20
- Estimated files touched: 13
- Max added lines: 1000
- Estimated added lines: 980

**Files:**
- Create: `src/hikbox_pictures/services/export_match_service.py`
- Create: `src/hikbox_pictures/services/export_delivery_service.py`
- Modify: `src/hikbox_pictures/repositories/export_repo.py`
- Modify: `src/hikbox_pictures/services/action_service.py`
- Modify: `src/hikbox_pictures/api/routes_export.py`
- Modify: `src/hikbox_pictures/cli.py`
- Modify: `src/hikbox_pictures/exporter.py`
- Modify: `src/hikbox_pictures/models.py`
- Create: `tests/people_gallery/test_export_matching_and_ledger.py`
- Create: `tests/people_gallery/test_export_stale_cleanup.py`
- Create: `tests/people_gallery/test_export_live_photo_delivery.py`
- Modify: `tests/people_gallery/fixtures_workspace.py`
- Modify: `README.md`

- [ ] **Step 1: 写失败测试，锁定命中统计、账本跳过与 stale 语义**

```python
def test_export_preview_returns_real_only_group_counts(seed_workspace, client):
    data = client.get("/api/export/templates/1/preview").json()
    assert data["matched_only_count"] == 2
    assert data["matched_group_count"] == 1


def test_export_run_skips_already_delivered_asset(seed_workspace):
    result = seed_workspace.export_service.run_template(template_id=1)
    assert result["skipped_count"] >= 1


def test_export_rule_change_marks_previous_delivery_stale(seed_workspace):
    first = seed_workspace.export_service.run_template(template_id=1)
    seed_workspace.export_service.update_template_include_group(template_id=1, include_group=False)
    second = seed_workspace.export_service.run_template(template_id=1)

    assert second["spec_hash"] != first["spec_hash"]
    assert seed_workspace.export_repo.count_stale_deliveries(template_id=1) > 0
```

- [ ] **Step 2: 运行测试，确认失败**

Run: `source .venv/bin/activate && PYTHONPATH=src python3 -m pytest tests/people_gallery/test_export_matching_and_ledger.py tests/people_gallery/test_export_stale_cleanup.py tests/people_gallery/test_export_live_photo_delivery.py -v`
Expected: FAIL。

- [ ] **Step 3: 实现导出匹配器、账本与 Live Photo 补齐**

```python
# src/hikbox_pictures/services/export_match_service.py（关键片段）
class ExportMatchService:
    def classify_bucket(self, matched_observations, extra_observations):
        selected_min_area = min(obs["face_area_ratio"] for obs in matched_observations)
        threshold = selected_min_area / 4.0
        for obs in extra_observations:
            area = obs.get("face_area_ratio")
            if area is None or area >= threshold:
                return "group"
        return "only"
```

```python
# src/hikbox_pictures/services/export_delivery_service.py（关键片段）
class ExportDeliveryService:
    def upsert_delivery(self, template_id: int, spec_hash: str, photo_asset_id: int, variant: str, bucket: str, target_path: str, fingerprint: str, status: str):
        self.conn.execute(
            """
            INSERT INTO export_delivery(template_id, spec_hash, photo_asset_id, asset_variant, bucket, target_path, source_fingerprint, status, last_exported_at, last_verified_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            ON CONFLICT(template_id, spec_hash, photo_asset_id, asset_variant)
            DO UPDATE SET target_path=excluded.target_path, source_fingerprint=excluded.source_fingerprint, status=excluded.status, last_exported_at=CURRENT_TIMESTAMP, last_verified_at=CURRENT_TIMESTAMP
            """,
            (template_id, spec_hash, photo_asset_id, variant, bucket, target_path, fingerprint, status),
        )
        self.conn.commit()
```

- [ ] **Step 4: 运行回归，确认导出链路满足 spec**

Run: `source .venv/bin/activate && PYTHONPATH=src python3 -m pytest tests/people_gallery/test_export_matching_and_ledger.py tests/people_gallery/test_export_stale_cleanup.py tests/people_gallery/test_export_live_photo_delivery.py tests/people_gallery/test_api_contract.py::test_export_preview_contains_real_counts -q`
Expected: PASS。

**Task completion action (not a checkbox step): Commit task changes and plan progress**

```bash
git add src/hikbox_pictures/services/export_match_service.py src/hikbox_pictures/services/export_delivery_service.py src/hikbox_pictures/repositories/export_repo.py src/hikbox_pictures/services/action_service.py src/hikbox_pictures/api/routes_export.py src/hikbox_pictures/cli.py src/hikbox_pictures/exporter.py src/hikbox_pictures/models.py tests/people_gallery/test_export_matching_and_ledger.py tests/people_gallery/test_export_stale_cleanup.py tests/people_gallery/test_export_live_photo_delivery.py tests/people_gallery/fixtures_workspace.py README.md docs/superpowers/plans/2026-04-11-hikbox-pictures-people-gallery.md
git commit -m "feat: implement export matching ledger and stale delivery semantics (Task 9)"
```

### Task 10: 结构化日志双层落地与保留清理命令

**Depends on:** Task 5, Task 9

**Scope Budget:**
- Max files: 20
- Estimated files touched: 12
- Max added lines: 1000
- Estimated added lines: 900

**Files:**
- Create: `src/hikbox_pictures/logging_config.py`
- Create: `src/hikbox_pictures/services/observability_service.py`
- Modify: `src/hikbox_pictures/repositories/ops_event_repo.py`
- Modify: `src/hikbox_pictures/services/scan_orchestrator.py`
- Modify: `src/hikbox_pictures/services/export_delivery_service.py`
- Modify: `src/hikbox_pictures/api/routes_logs.py`
- Modify: `src/hikbox_pictures/cli.py`
- Create: `tests/people_gallery/test_ops_event_filters.py`
- Create: `tests/people_gallery/test_logs_tail_and_prune.py`
- Modify: `tests/people_gallery/test_api_contract.py`
- Modify: `tests/people_gallery/fixtures_workspace.py`
- Modify: `README.md`

补充说明：`test_cli_control_plane.py` 属于 logs 命令依赖性触达，本轮 `fixtures_workspace` 无必要改动。

- [x] **Step 1: 写失败测试，锁定事件过滤与保留清理**

```python
def test_logs_api_filters_by_run_kind_and_event_type(seed_workspace, client):
    data = client.get("/api/logs/events", params={"run_kind": "scan", "event_type": "scan.session.started"}).json()
    assert len(data["items"]) >= 1
    assert all(item["event_type"] == "scan.session.started" for item in data["items"])


def test_logs_prune_deletes_old_rows(seed_workspace):
    deleted = seed_workspace.observability_service.prune_ops_events(days=0)
    assert deleted >= 1
```

- [x] **Step 2: 运行测试，确认失败**

Run: `source .venv/bin/activate && PYTHONPATH=src python3 -m pytest tests/people_gallery/test_ops_event_filters.py tests/people_gallery/test_logs_tail_and_prune.py -v`
Expected: FAIL。

- [x] **Step 3: 实现结构化日志写入与 logs 命令**

```python
# src/hikbox_pictures/services/observability_service.py（关键片段）
class ObservabilityService:
    def emit_event(self, *, level: str, component: str, event_type: str, run_kind: str | None, run_id: int | None, message: str, detail_json: str = "{}") -> None:
        self.repo.insert_event(
            level=level,
            component=component,
            event_type=event_type,
            run_kind=run_kind,
            run_id=run_id,
            message=message,
            detail_json=detail_json,
        )
        self.file_logger.info(
            message,
            extra={
                "event_type": event_type,
                "component": component,
                "run_kind": run_kind,
                "run_id": run_id,
            },
        )
```

```python
# src/hikbox_pictures/cli.py（关键片段）
def handle_logs_tail(args) -> int:
    for line in iter_run_log_lines(args.workspace, run_kind=args.run_kind, run_id=args.run_id, limit=args.limit):
        print(line)
    return 0


def handle_logs_prune(args) -> int:
    deleted_events = Runtime.from_workspace(args.workspace).observability_service.prune_ops_events(days=args.days)
    print(f"已清理 ops_event 行数: {deleted_events}")
    return 0
```

- [x] **Step 4: 运行回归，确认日志体系可用**

Run: `source .venv/bin/activate && PYTHONPATH=src python3 -m pytest tests/people_gallery/test_ops_event_filters.py tests/people_gallery/test_logs_tail_and_prune.py tests/people_gallery/test_api_contract.py::test_logs_api_filter_event_type -q`
Expected: PASS。

**Task completion action (not a checkbox step): Commit task changes and plan progress**

```bash
git add src/hikbox_pictures/logging_config.py src/hikbox_pictures/services/observability_service.py src/hikbox_pictures/repositories/ops_event_repo.py src/hikbox_pictures/services/scan_orchestrator.py src/hikbox_pictures/services/export_delivery_service.py src/hikbox_pictures/api/routes_logs.py src/hikbox_pictures/cli.py tests/people_gallery/test_ops_event_filters.py tests/people_gallery/test_logs_tail_and_prune.py tests/people_gallery/test_api_contract.py tests/people_gallery/fixtures_workspace.py README.md docs/superpowers/plans/2026-04-11-hikbox-pictures-people-gallery.md
git commit -m "feat: implement structured observability pipeline and log lifecycle commands (Task 10)"
```

### Task 11: WebUI 工作台（人物库/待审核/源扫描/导出/日志）绑定真实数据

**Depends on:** Task 8, Task 10

**Scope Budget:**
- Max files: 20
- Estimated files touched: 17
- Max added lines: 1000
- Estimated added lines: 980

**Files:**
- Create: `src/hikbox_pictures/api/routes_web.py`
- Create: `src/hikbox_pictures/web/templates/base.html`
- Create: `src/hikbox_pictures/web/templates/people.html`
- Create: `src/hikbox_pictures/web/templates/person_detail.html`
- Create: `src/hikbox_pictures/web/templates/review_queue.html`
- Create: `src/hikbox_pictures/web/templates/sources_scan.html`
- Create: `src/hikbox_pictures/web/templates/export_templates.html`
- Create: `src/hikbox_pictures/web/templates/logs.html`
- Create: `src/hikbox_pictures/web/static/style.css`
- Create: `src/hikbox_pictures/web/static/app.js`
- Modify: `src/hikbox_pictures/api/app.py`
- Create: `tests/people_gallery/test_web_navigation.py`
- Create: `tests/people_gallery/test_webui_content.py`
- Create: `tests/people_gallery/test_webui_actions_e2e.py`
- Modify: `tests/people_gallery/fixtures_workspace.py`
- Modify: `tests/people_gallery/test_api_contract.py`
- Modify: `README.md`

补充说明：`web_query_service.py` 属于页面数据聚合依赖性触达，本轮 `fixtures_workspace.py` 无必要改动。

- [x] **Step 1: 写失败测试，锁定页面结构、真实数据与动作闭环**

```python
def test_people_page_has_cards_and_real_names(seed_workspace, client):
    html = client.get("/").text
    assert "person-card" in html
    assert "人物A" in html
    assert "进入维护" in html


def test_reviews_page_has_typed_queues(seed_workspace, client):
    html = client.get("/reviews").text
    assert "queue-new_person" in html
    assert "queue-possible_merge" in html
    assert "queue-possible_split" in html
    assert "queue-low_confidence_assignment" in html


def test_web_action_roundtrip(seed_workspace, client):
    assert client.post("/api/people/1/actions/rename", json={"display_name": "爸爸"}).status_code == 200
    assert "爸爸" in client.get("/").text
```

- [x] **Step 2: 运行测试，确认失败**

Run: `source .venv/bin/activate && PYTHONPATH=src python3 -m pytest tests/people_gallery/test_web_navigation.py tests/people_gallery/test_webui_content.py tests/people_gallery/test_webui_actions_e2e.py -v`
Expected: FAIL。

- [x] **Step 3: 实现 routes_web 与全量模板，接入统一导航与样式**

```python
# src/hikbox_pictures/api/routes_web.py（关键片段）
@router.get("/", response_class=HTMLResponse)
def people_page(request: Request):
    svc = request.app.state.web_query_service
    return request.app.state.templates.TemplateResponse(
        request,
        "people.html",
        {
            "page_title": "人物库",
            "page_key": "people",
            "people": svc.list_people(),
        },
    )


@router.get("/reviews", response_class=HTMLResponse)
def reviews_page(request: Request):
    queues = request.app.state.web_query_service.list_review_queues()
    return request.app.state.templates.TemplateResponse(
        request,
        "review_queue.html",
        {
            "page_title": "待审核",
            "page_key": "reviews",
            "queues": queues,
        },
    )
```

```html
<!-- src/hikbox_pictures/web/templates/base.html（关键片段） -->
<body data-page="{{ page_key }}">
  <nav class="main-nav">
    <a href="/">人物库</a>
    <a href="/reviews">待审核</a>
    <a href="/sources">源目录与扫描</a>
    <a href="/exports">导出模板</a>
    <a href="/logs">日志</a>
  </nav>
  <main>{% block content %}{% endblock %}</main>
</body>
```

- [x] **Step 4: 运行回归，确认 WebUI 非占位并与 API 同步**

Run: `source .venv/bin/activate && PYTHONPATH=src python3 -m pytest tests/people_gallery/test_web_navigation.py tests/people_gallery/test_webui_content.py tests/people_gallery/test_webui_actions_e2e.py tests/people_gallery/test_api_contract.py::test_people_api_matches_people_page -q`
Expected: PASS。

**Task completion action (not a checkbox step): Commit task changes and plan progress**

```bash
git add src/hikbox_pictures/api/routes_web.py src/hikbox_pictures/web/templates/base.html src/hikbox_pictures/web/templates/people.html src/hikbox_pictures/web/templates/person_detail.html src/hikbox_pictures/web/templates/review_queue.html src/hikbox_pictures/web/templates/sources_scan.html src/hikbox_pictures/web/templates/export_templates.html src/hikbox_pictures/web/templates/logs.html src/hikbox_pictures/web/static/style.css src/hikbox_pictures/web/static/app.js src/hikbox_pictures/api/app.py tests/people_gallery/test_web_navigation.py tests/people_gallery/test_webui_content.py tests/people_gallery/test_webui_actions_e2e.py tests/people_gallery/fixtures_workspace.py tests/people_gallery/test_api_contract.py README.md docs/superpowers/plans/2026-04-11-hikbox-pictures-people-gallery.md
git commit -m "feat: implement full web workbench bound to real workspace data (Task 11)"
```

### Task 12: 端到端验收、文档更新与计划收口

**Depends on:** Task 11

**Scope Budget:**
- Max files: 20
- Estimated files touched: 5
- Max added lines: 1000
- Estimated added lines: 380

**Files:**
- Create: `tests/people_gallery/test_e2e_full_system.py`
- Modify: `tests/people_gallery/test_cli_control_plane.py`
- Modify: `tests/people_gallery/test_api_bootstrap.py`
- Modify: `README.md`
- Modify: `docs/superpowers/plans/2026-04-11-hikbox-pictures-people-gallery.md`

- [ ] **Step 1: 写失败测试，锁定“init -> source -> scan -> review -> export -> logs”主流程**

```python
def test_full_system_happy_path(tmp_path):
    from hikbox_pictures.cli import main

    assert main(["init", "--workspace", str(tmp_path)]) == 0
    assert main(["source", "add", "--workspace", str(tmp_path), "--name", "iCloud", "--root", str(tmp_path / "sample")]) == 0
    assert main(["scan", "--workspace", str(tmp_path)]) == 0
    assert main(["export", "run", "--workspace", str(tmp_path), "--template-id", "1"]) == 0
    assert main(["logs", "prune", "--workspace", str(tmp_path), "--days", "30"]) == 0
```

- [ ] **Step 2: 运行 people_gallery 全量测试**

Run: `source .venv/bin/activate && PYTHONPATH=src python3 -m pytest tests/people_gallery -q`
Expected: PASS。

- [ ] **Step 3: 运行关键存量回归，防止旧流程回归损坏**

Run: `source .venv/bin/activate && PYTHONPATH=src python3 -m pytest tests/test_cli.py tests/test_matcher.py tests/test_exporter.py tests/test_reference_template.py -q`
Expected: PASS。

- [ ] **Step 4: 更新 README 验收口径与运维说明**

```markdown
## 控制面命令

- `hikbox-pictures init --workspace <dir>`
- `hikbox-pictures source add|list|remove ...`
- `hikbox-pictures scan --workspace <dir>`
- `hikbox-pictures scan status --workspace <dir>`
- `hikbox-pictures export run --workspace <dir> --template-id <id>`
- `hikbox-pictures logs tail --workspace <dir> --run-kind scan --run-id <id>`
- `hikbox-pictures logs prune --workspace <dir> --days 90`

## 验收口径（必须同时满足）

1. 可以初始化 workspace 并完成 schema migration。
2. 可以对多 source 执行可恢复扫描，并支持默认续跑。
3. WebUI 页面展示真实人物/审核/导出/日志数据，动作回写数据库后刷新可见。
4. 导出支持 `only/group`、账本跳过、规则变更 stale 标记与 Live Photo MOV 补齐。
5. 日志支持 run 过滤查询与保留清理，不影响业务真相。
```

- [ ] **Step 5: 勾选任务状态并写入最终验收结论**

```markdown
## 最终验收结论

- 基础设施面：通过（Task 1-4）。
- 核心引擎面：通过（Task 5-8）。
- 交付与运维面：通过（Task 9-12）。
- 与 spec 对齐：通过。

## 残余风险

- 需要在真实几十万张照片库上补充长时压测与资源占用基线。
- 需要补充更多异常图片（损坏 HEIC、缺失 MOV、EXIF 破损）样本回归。
```

**Task completion action (not a checkbox step): Commit task changes and plan progress**

```bash
git add tests/people_gallery/test_e2e_full_system.py tests/people_gallery/test_cli_control_plane.py tests/people_gallery/test_api_bootstrap.py README.md docs/superpowers/plans/2026-04-11-hikbox-pictures-people-gallery.md
git commit -m "test: add full-system e2e acceptance and finalize implementation plan (Task 12)"
```

---

## Dependency Validation

- 每个任务都包含显式 `Depends on`。
- 依赖图无环，且至少有一个起始任务（Task 1）。
- 声明可并行的任务写入集合不冲突（Task 5 与 Task 9）。
- 共享文件（`cli.py`、`api/app.py`、`README.md`）均被串行约束。

## Scope Validation

- 每个任务都有 `Scope Budget`。
- 每个任务估算文件数 `<= 20`。
- 每个任务估算新增行数 `<= 1000`。
- 超预算能力（完整系统）已拆分为 12 个可落地任务。

## 自检结论

- 已补齐旧计划缺失的地基任务（依赖、迁移、工作区、CLI 控制面）。
- 已补齐扫描恢复语义（source 级阶段、checkpoint、heartbeat、owner 回收、默认恢复）。
- 已补齐导出真实语义（命中统计、账本、spec_hash、stale、MOV）。
- 已补齐可观测体系（结构化日志 + 事件索引 + prune）。
- 已修正并行设计，移除共享文件高冲突并行波次。

---

## 增补任务（2026-04-13，WebUI 看图能力）

> 说明：本节仅追加新任务，不修改 Task 1-12 内容。新任务用于覆盖 spec 新增的“WebUI 看图与预览（P0）”“图片读取与预览服务边界”“看图验收标准”。

## 增补并行执行计划

### Wave G（顺序）

- 顺序执行：`Task 13 -> Task 14`
- 原因：`Task 14` 依赖 `Task 13` 的媒体读取接口与安全边界。
- 阻塞项：`Task 15`。

### Wave H（顺序）

- 顺序执行：`Task 15`
- 原因：需要复用 `Task 13-14` 的接口和异常降级语义，并与既有 `Task 11` 页面绑定。
- 阻塞项：`Task 16`。

### Wave I（顺序）

- 顺序执行：`Task 16`
- 原因：统一做 P0 验收与性能烟测，避免在 UI 迭代过程中反复改门槛。

### Task 13: 媒体预览 API 与路径安全边界

**Depends on:** Task 11

**Scope Budget:**
- Max files: 20
- Estimated files touched: 11
- Max added lines: 1000
- Estimated added lines: 760

**Files:**
- Create: `src/hikbox_pictures/api/routes_media.py`
- Create: `src/hikbox_pictures/services/media_preview_service.py`
- Create: `src/hikbox_pictures/services/path_guard.py`
- Modify: `src/hikbox_pictures/repositories/asset_repo.py`
- Modify: `src/hikbox_pictures/services/runtime.py`
- Modify: `src/hikbox_pictures/api/app.py`
- Create: `tests/people_gallery/test_media_api_contract.py`
- Create: `tests/people_gallery/test_media_range_request.py`
- Create: `tests/people_gallery/test_media_path_security.py`
- Modify: `tests/people_gallery/fixtures_workspace.py`
- Modify: `README.md`

- [x] **Step 1: 写失败测试，锁定 4 个媒体端点与路径越界防护**

```python
def test_crop_and_context_endpoint_returns_image(seed_workspace, client):
    crop = client.get("/api/observations/1/crop")
    context = client.get("/api/observations/1/context")
    assert crop.status_code == 200
    assert context.status_code == 200
    assert crop.headers["content-type"].startswith("image/")
    assert context.headers["content-type"].startswith("image/")


def test_original_endpoint_supports_range(seed_workspace, client):
    resp = client.get("/api/photos/1/original", headers={"Range": "bytes=0-1023"})
    assert resp.status_code == 206
    assert "Content-Range" in resp.headers


def test_path_traversal_is_blocked(seed_workspace):
    from hikbox_pictures.services.path_guard import ensure_safe_asset_path
    import pytest

    with pytest.raises(PermissionError):
        ensure_safe_asset_path(
            candidate="/etc/passwd",
            allowed_roots=["/tmp/workspace/sample"],
        )
```

- [x] **Step 2: 运行测试，确认失败**

Run: `source .venv/bin/activate && PYTHONPATH=src python3 -m pytest tests/people_gallery/test_media_api_contract.py tests/people_gallery/test_media_range_request.py tests/people_gallery/test_media_path_security.py -v`
Expected: FAIL。

- [x] **Step 3: 实现媒体读取服务、Range 返回与安全校验**

```python
# src/hikbox_pictures/services/path_guard.py（关键片段）
from pathlib import Path


def ensure_safe_asset_path(candidate: str, allowed_roots: list[str]) -> Path:
    resolved = Path(candidate).expanduser().resolve()
    roots = [Path(root).expanduser().resolve() for root in allowed_roots]
    if not any(root == resolved or root in resolved.parents for root in roots):
        raise PermissionError(f"asset path out of allowed roots: {resolved}")
    return resolved
```

```python
# src/hikbox_pictures/api/routes_media.py（关键片段）
@router.get("/api/photos/{photo_id}/original")
def get_original(photo_id: int, request: Request):
    result = request.app.state.media_preview_service.read_original_stream(photo_id, request.headers.get("Range"))
    return StreamingResponse(
        result.body_iter,
        status_code=result.status_code,
        media_type=result.media_type,
        headers=result.headers,
    )
```

- [x] **Step 4: 运行回归，确认媒体 API 合同成立**

Run: `source .venv/bin/activate && PYTHONPATH=src python3 -m pytest tests/people_gallery/test_media_api_contract.py tests/people_gallery/test_media_range_request.py tests/people_gallery/test_media_path_security.py tests/people_gallery/test_api_contract.py::test_people_api_matches_people_page -q`
Expected: PASS。

**Task completion action (not a checkbox step): Commit task changes and plan progress**

```bash
git add src/hikbox_pictures/api/routes_media.py src/hikbox_pictures/services/media_preview_service.py src/hikbox_pictures/services/path_guard.py src/hikbox_pictures/repositories/asset_repo.py src/hikbox_pictures/services/runtime.py src/hikbox_pictures/api/app.py tests/people_gallery/test_media_api_contract.py tests/people_gallery/test_media_range_request.py tests/people_gallery/test_media_path_security.py tests/people_gallery/fixtures_workspace.py README.md docs/superpowers/plans/2026-04-11-hikbox-pictures-people-gallery.md
git commit -m "feat: add media preview apis with range support and path security (Task 13)"
```

### Task 14: 预览 artifact 重建与错误降级事件

**Depends on:** Task 10, Task 13

**Scope Budget:**
- Max files: 20
- Estimated files touched: 10
- Max added lines: 1000
- Estimated added lines: 700

**Files:**
- Create: `src/hikbox_pictures/services/preview_artifact_service.py`
- Modify: `src/hikbox_pictures/services/asset_pipeline.py`
- Modify: `src/hikbox_pictures/services/media_preview_service.py`
- Modify: `src/hikbox_pictures/services/observability_service.py`
- Modify: `src/hikbox_pictures/repositories/ops_event_repo.py`
- Create: `tests/people_gallery/test_preview_artifact_rebuild.py`
- Create: `tests/people_gallery/test_preview_error_handling.py`
- Modify: `tests/people_gallery/fixtures_workspace.py`
- Modify: `tests/people_gallery/test_api_contract.py`
- Modify: `README.md`

- [x] **Step 1: 写失败测试，锁定 crop/context 缺失重建与错误码**

```python
def test_missing_crop_is_rebuilt_on_demand(seed_workspace, client):
    seed_workspace.break_crop_for_observation(1)
    resp = client.get("/api/observations/1/crop")
    assert resp.status_code == 200
    assert seed_workspace.crop_exists(1)


def test_missing_original_returns_structured_error(seed_workspace, client):
    seed_workspace.break_original_for_photo(1)
    resp = client.get("/api/photos/1/original")
    assert resp.status_code == 404
    payload = resp.json()
    assert payload["error_code"] == "preview.asset.missing"


def test_decode_failed_emits_ops_event(seed_workspace, client):
    seed_workspace.inject_broken_image_for_photo(2)
    client.get("/api/photos/2/preview")
    assert seed_workspace.count_ops_event("preview.asset.decode_failed") >= 1
```

- [x] **Step 2: 运行测试，确认失败**

Run: `source .venv/bin/activate && PYTHONPATH=src python3 -m pytest tests/people_gallery/test_preview_artifact_rebuild.py tests/people_gallery/test_preview_error_handling.py -v`
Expected: FAIL。

- [x] **Step 3: 实现重建服务与预览错误打点**

```python
# src/hikbox_pictures/services/preview_artifact_service.py（关键片段）
class PreviewArtifactService:
    def ensure_crop(self, observation_id: int) -> str:
        record = self.asset_repo.get_observation(observation_id)
        crop_path = record["crop_path"]
        if crop_path and Path(crop_path).exists():
            return crop_path
        rebuilt = self.rebuild_crop_from_source(record)
        self.asset_repo.update_observation_crop_path(observation_id, rebuilt)
        self.observability.emit_event(
            level="info",
            component="api",
            event_type="preview.context.rebuild_requested",
            run_kind=None,
            run_id=None,
            message=f"rebuild crop for observation={observation_id}",
        )
        return rebuilt
```

```python
# src/hikbox_pictures/services/media_preview_service.py（关键片段）
def read_original_stream(self, photo_id: int, range_header: str | None):
    asset = self.asset_repo.get_photo_asset(photo_id)
    if asset is None or not Path(asset["primary_path"]).exists():
        self.observability.emit_event(level="warning", component="api", event_type="preview.asset.missing", run_kind=None, run_id=None, message=f"photo not found: {photo_id}")
        raise PreviewNotFound(photo_id=photo_id, error_code="preview.asset.missing")
```

- [x] **Step 4: 运行回归，确认重建与降级行为满足 spec**

Run: `source .venv/bin/activate && PYTHONPATH=src python3 -m pytest tests/people_gallery/test_preview_artifact_rebuild.py tests/people_gallery/test_preview_error_handling.py tests/people_gallery/test_api_contract.py::test_logs_api_filter_event_type -q`
Expected: PASS。

**Task completion action (not a checkbox step): Commit task changes and plan progress**

```bash
git add src/hikbox_pictures/services/preview_artifact_service.py src/hikbox_pictures/services/asset_pipeline.py src/hikbox_pictures/services/media_preview_service.py src/hikbox_pictures/services/observability_service.py src/hikbox_pictures/repositories/ops_event_repo.py tests/people_gallery/test_preview_artifact_rebuild.py tests/people_gallery/test_preview_error_handling.py tests/people_gallery/fixtures_workspace.py tests/people_gallery/test_api_contract.py README.md docs/superpowers/plans/2026-04-11-hikbox-pictures-people-gallery.md
git commit -m "feat: add preview artifact rebuild and structured degradation events (Task 14)"
```

### Task 15: WebUI 统一预览器接入人物详情、待审核与导出预览

**Depends on:** Task 11, Task 13, Task 14

**Scope Budget:**
- Max files: 20
- Estimated files touched: 13
- Max added lines: 1000
- Estimated added lines: 930

**Files:**
- Create: `src/hikbox_pictures/web/templates/components/media_viewer.html`
- Modify: `src/hikbox_pictures/web/templates/person_detail.html`
- Modify: `src/hikbox_pictures/web/templates/review_queue.html`
- Modify: `src/hikbox_pictures/web/templates/export_templates.html`
- Modify: `src/hikbox_pictures/web/templates/people.html`
- Modify: `src/hikbox_pictures/web/static/app.js`
- Modify: `src/hikbox_pictures/web/static/style.css`
- Modify: `src/hikbox_pictures/api/routes_web.py`
- Modify: `src/hikbox_pictures/services/web_query_service.py`
- Create: `tests/people_gallery/test_webui_media_viewer.py`
- Create: `tests/people_gallery/test_webui_export_preview_samples.py`
- Modify: `tests/people_gallery/test_webui_content.py`
- Modify: `README.md`

- [x] **Step 1: 写失败测试，锁定三层视图、快捷键与样例预览**

```python
def test_person_detail_contains_media_viewer(seed_workspace, client):
    html = client.get("/people/1").text
    assert "data-viewer-layer=\"crop\"" in html
    assert "data-viewer-layer=\"context\"" in html
    assert "data-viewer-layer=\"original\"" in html


def test_review_queue_has_viewer_actions(seed_workspace, client):
    html = client.get("/reviews").text
    assert "data-action=\"viewer-prev\"" in html
    assert "data-action=\"viewer-next\"" in html
    assert "data-action=\"viewer-toggle-bbox\"" in html


def test_export_preview_has_sample_cards(seed_workspace, client):
    html = client.get("/exports").text
    assert "export-preview-sample" in html
```

- [x] **Step 2: 运行测试，确认失败**

Run: `source .venv/bin/activate && PYTHONPATH=src python3 -m pytest tests/people_gallery/test_webui_media_viewer.py tests/people_gallery/test_webui_export_preview_samples.py tests/people_gallery/test_webui_content.py -v`
Expected: FAIL。

- [x] **Step 3: 实现统一预览器组件与页面接入**

```html
<!-- src/hikbox_pictures/web/templates/components/media_viewer.html（关键片段） -->
<section id="media-viewer" class="media-viewer" data-active="false">
  <header>
    <button data-action="viewer-prev">上一张</button>
    <button data-action="viewer-next">下一张</button>
    <button data-action="viewer-toggle-bbox">脸框开关</button>
  </header>
  <div class="viewer-layers">
    <img data-viewer-layer="crop" alt="crop" />
    <img data-viewer-layer="context" alt="context" />
    <img data-viewer-layer="original" alt="original" />
  </div>
</section>
```

```javascript
// src/hikbox_pictures/web/static/app.js（关键片段）
document.addEventListener("keydown", (event) => {
  if (event.key === "ArrowLeft") {
    window.hikboxViewer.prev();
  } else if (event.key === "ArrowRight") {
    window.hikboxViewer.next();
  } else if (event.key.toLowerCase() === "b") {
    window.hikboxViewer.toggleBbox();
  }
});
```

- [x] **Step 4: 运行回归，确认三处页面交互语义一致**

Run: `source .venv/bin/activate && PYTHONPATH=src python3 -m pytest tests/people_gallery/test_webui_media_viewer.py tests/people_gallery/test_webui_export_preview_samples.py tests/people_gallery/test_webui_content.py tests/people_gallery/test_web_navigation.py -q`
Expected: PASS。

**Task completion action (not a checkbox step): Commit task changes and plan progress**

```bash
git add src/hikbox_pictures/web/templates/components/media_viewer.html src/hikbox_pictures/web/templates/person_detail.html src/hikbox_pictures/web/templates/review_queue.html src/hikbox_pictures/web/templates/export_templates.html src/hikbox_pictures/web/templates/people.html src/hikbox_pictures/web/static/app.js src/hikbox_pictures/web/static/style.css src/hikbox_pictures/api/routes_web.py src/hikbox_pictures/services/web_query_service.py tests/people_gallery/test_webui_media_viewer.py tests/people_gallery/test_webui_export_preview_samples.py tests/people_gallery/test_webui_content.py README.md docs/superpowers/plans/2026-04-11-hikbox-pictures-people-gallery.md
git commit -m "feat: integrate unified media viewer across people review and export pages (Task 15)"
```

### Task 16: WebUI 看图 P0 验收与性能烟测

**Depends on:** Task 15

**Scope Budget:**
- Max files: 20
- Estimated files touched: 8
- Max added lines: 1000
- Estimated added lines: 520

**Files:**
- Create: `tests/people_gallery/test_media_viewer_acceptance.py`
- Create: `tests/people_gallery/test_media_preview_performance_smoke.py`
- Modify: `tests/people_gallery/test_e2e_full_system.py`
- Modify: `tests/people_gallery/test_webui_actions_e2e.py`
- Modify: `tests/people_gallery/fixtures_workspace.py`
- Modify: `README.md`
- Modify: `docs/superpowers/plans/2026-04-11-hikbox-pictures-people-gallery.md`

- [ ] **Step 1: 写失败测试，锁定 P0 验收 10 条中的关键路径**

```python
def test_viewer_flow_person_detail_review_export(seed_workspace, client):
    assert "data-viewer-layer=\"original\"" in client.get("/people/1").text
    assert "data-action=\"viewer-next\"" in client.get("/reviews").text
    assert "export-preview-sample" in client.get("/exports").text


def test_single_image_failure_does_not_block_queue(seed_workspace, client):
    seed_workspace.break_original_for_photo(1)
    html = client.get("/reviews").text
    assert "预览失败" in html
    assert "queue-item" in html
```

- [ ] **Step 2: 写失败测试，锁定本机性能烟测门槛**

```python
def test_preview_latency_smoke(seed_workspace, client):
    import time

    start = time.perf_counter()
    response = client.get("/api/photos/1/preview")
    elapsed_ms = (time.perf_counter() - start) * 1000

    assert response.status_code == 200
    assert elapsed_ms <= 600
```

- [ ] **Step 3: 运行测试，确认失败**

Run: `source .venv/bin/activate && PYTHONPATH=src python3 -m pytest tests/people_gallery/test_media_viewer_acceptance.py tests/people_gallery/test_media_preview_performance_smoke.py -v`
Expected: FAIL。

- [ ] **Step 4: 补齐验收脚本、README 运维说明与计划回填**

```markdown
## WebUI 看图验收（P0）

1. 人物详情支持 crop/context/original 三层查看。
2. 待审核支持上一张/下一张、脸框开关与失败降级提示。
3. 导出模板预览含样例照片，不仅有命中数量。
4. 媒体 API 支持 Range 与路径越界防护。
```

- [ ] **Step 5: 运行回归并勾选 Task 13-16 完成状态**

Run: `source .venv/bin/activate && PYTHONPATH=src python3 -m pytest tests/people_gallery/test_media_api_contract.py tests/people_gallery/test_preview_artifact_rebuild.py tests/people_gallery/test_webui_media_viewer.py tests/people_gallery/test_media_viewer_acceptance.py tests/people_gallery/test_media_preview_performance_smoke.py -q`
Expected: PASS。

**Task completion action (not a checkbox step): Commit task changes and plan progress**

```bash
git add tests/people_gallery/test_media_viewer_acceptance.py tests/people_gallery/test_media_preview_performance_smoke.py tests/people_gallery/test_e2e_full_system.py tests/people_gallery/test_webui_actions_e2e.py tests/people_gallery/fixtures_workspace.py README.md docs/superpowers/plans/2026-04-11-hikbox-pictures-people-gallery.md
git commit -m "test: add webui media viewer p0 acceptance and performance smoke coverage (Task 16)"
```

## 增补并行执行计划（补充 2）

### Wave J（顺序）

- 顺序执行：`Task 17`
- 原因：该任务是独立 e2e 验证链路，依赖前置能力已就位后一次性收敛。
- 阻塞项：无（作为增补验收任务，可在 Task 16 后执行）。

### Task 17: 数字图片 + mock embedding 的全链路 e2e 集成测试

**Depends on:** Task 9, Task 10, Task 15

**Scope Budget:**
- Max files: 20
- Estimated files touched: 7
- Max added lines: 1000
- Estimated added lines: 560

**Files:**
- Create: `tests/people_gallery/image_factory.py`
- Create: `tests/people_gallery/test_e2e_mock_embedding_pipeline.py`
- Modify: `tests/people_gallery/fixtures_workspace.py`
- Modify: `tests/people_gallery/test_e2e_full_system.py`
- Modify: `tests/people_gallery/test_api_contract.py`
- Modify: `README.md`
- Modify: `docs/superpowers/plans/2026-04-11-hikbox-pictures-people-gallery.md`

- [ ] **Step 1: 写失败测试，锁定“无人脸源图 + mock embedding 注入 + 后续全流程”**

```python
def test_e2e_with_number_images_and_mock_embeddings(tmp_path):
    from hikbox_pictures.cli import main
    from tests.people_gallery.fixtures_workspace import (
        create_number_image_dataset,
        inject_mock_embeddings_for_assets,
    )

    workspace = tmp_path / "ws"
    dataset = tmp_path / "digits"
    create_number_image_dataset(dataset, names=["001.jpg", "002.jpg", "003.jpg"])

    assert main(["init", "--workspace", str(workspace)]) == 0
    assert main(["source", "add", "--workspace", str(workspace), "--name", "digits", "--root", str(dataset)]) == 0
    assert main(["scan", "--workspace", str(workspace)]) == 0

    inject_mock_embeddings_for_assets(
        workspace=workspace,
        person_specs=[
            {"name": "人物甲", "asset_file": "001.jpg", "vector": [0.11, 0.12, 0.13, 0.14]},
            {"name": "人物乙", "asset_file": "002.jpg", "vector": [0.21, 0.22, 0.23, 0.24]},
        ],
        template_name="甲乙模板",
    )

    assert main(["rebuild-artifacts", "--workspace", str(workspace)]) == 0
    assert main(["export", "run", "--workspace", str(workspace), "--template-id", "1"]) == 0


def test_mock_embedding_flow_visible_in_webui(seed_workspace_with_mock_embeddings, client):
    html_people = client.get("/").text
    html_reviews = client.get("/reviews").text
    html_exports = client.get("/exports").text

    assert "人物甲" in html_people
    assert "人物乙" in html_people
    assert "export-preview-sample" in html_exports
    assert "queue-item" in html_reviews
```

- [ ] **Step 2: 运行测试，确认失败**

Run: `source .venv/bin/activate && PYTHONPATH=src python3 -m pytest tests/people_gallery/test_e2e_mock_embedding_pipeline.py -v`
Expected: FAIL。

- [ ] **Step 3: 实现数字图片工厂与 mock embedding 注入夹具**

```python
# tests/people_gallery/image_factory.py（关键片段）
from pathlib import Path
from PIL import Image, ImageDraw


def write_number_image(path: Path, text: str) -> None:
    img = Image.new("RGB", (512, 512), color=(245, 245, 245))
    draw = ImageDraw.Draw(img)
    draw.rectangle((32, 32, 480, 480), outline=(30, 30, 30), width=4)
    draw.text((180, 220), text, fill=(20, 20, 20))
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path, format="JPEG")
```

```python
# tests/people_gallery/fixtures_workspace.py（关键片段）
def inject_mock_embeddings_for_assets(workspace: Path, person_specs: list[dict], template_name: str) -> None:
    conn = connect_workspace_db(workspace)
    for spec in person_specs:
        # 1) 根据文件名查 photo_asset
        # 2) 写入 face_observation（mock bbox/quality/crop_path）
        # 3) 写入 face_embedding（vector_blob 由 float32 打包）
        # 4) 写入 person 与 person_face_assignment（manual/locked）
        pass
    # 写入 export_template 与 export_template_person，保证 export run 可直接执行
    conn.commit()
```

- [ ] **Step 4: 运行回归，确认 mock 路径可覆盖后续全流程**

Run: `source .venv/bin/activate && PYTHONPATH=src python3 -m pytest tests/people_gallery/test_e2e_mock_embedding_pipeline.py tests/people_gallery/test_media_viewer_acceptance.py tests/people_gallery/test_export_matching_and_ledger.py::test_export_preview_returns_real_only_group_counts -q`
Expected: PASS。

- [ ] **Step 5: 合并到 e2e 套件并更新文档口径**

```markdown
## E2E（Mock Embedding）说明

- 集成测试允许使用“数字图片”作为输入，不依赖真实人脸检测。
- 通过测试夹具向 `face_observation` / `face_embedding` / `person_face_assignment` 注入 mock 数据，绕过检测与 embedding 提取耗时链路。
- 该路径用于验证“人物维护 -> 预览 -> 导出 -> 日志”后续流程稳定性，不替代真实模型链路测试。
```

**Task completion action (not a checkbox step): Commit task changes and plan progress**

```bash
git add tests/people_gallery/image_factory.py tests/people_gallery/test_e2e_mock_embedding_pipeline.py tests/people_gallery/fixtures_workspace.py tests/people_gallery/test_e2e_full_system.py tests/people_gallery/test_api_contract.py README.md docs/superpowers/plans/2026-04-11-hikbox-pictures-people-gallery.md
git commit -m "test: add e2e pipeline with synthetic number images and mock embeddings (Task 17)"
```
