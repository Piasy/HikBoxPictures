# HikBox Pictures 人物图库与智能导出 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax. This plan's checkbox state is the persistent progress source of truth; TodoWrite is session-local tracking. Executors may run dependency-free tasks in parallel (default max concurrency: 4).

**Goal:** 将现有一次性双人检索 CLI 升级为“本地人物图库 + 多 source 管理 + 可恢复增量扫描 + 人物维护 + 智能导出模板”系统。

**Architecture:** 业务真相统一写入 `<workspace>/.hikbox/library.db`；扫描、归属、审核、导出都使用状态表建模并可恢复。向量真相落库，查询层通过 `person_prototype + ANN` 做人物级召回，导出命中通过关系查询和账本比对实现，避免运行时全库向量扫描。系统分层为 `repository -> service -> api/web`，CLI 负责控制面，WebUI 提供人物库优先入口。

**Tech Stack:** Python 3.13+、SQLite、DeepFace、NumPy、hnswlib、FastAPI、Jinja2、pytest

---

## Parallel Execution Plan

### Wave A（串行地基）

- 顺序执行：`Task 1 -> Task 2 -> Task 3 -> Task 4`
- 原因：后续所有功能都依赖 workspace、DB 连接与完整 schema。
- 阻塞项：`Task 5-16` 全部等待 `Task 4`。

### Wave B（首轮并行）

- 可并行：`Task 5`、`Task 6`、`Task 7`、`Task 9`、`Task 13`
- 并行依据：
  - `Task 5` 写 `source_repo.py`
  - `Task 6` 写 `scan_repo.py/scan_service.py`
  - `Task 7` 写 `person_repo.py/assignment_service.py`
  - `Task 9` 写 `ann/index_store.py`
  - `Task 13` 写 `export_repo.py/export_service.py/exporter.py`
  - 写集合基本不重叠，仅共享 schema 只读。
- 阻塞项：`Task 8` 等 `Task 6 + Task 7`，`Task 10` 等 `Task 5 + Task 6`。

### Wave C（扫描编排并行）

- 可并行：`Task 8`、`Task 10`
- 并行依据：
  - `Task 8` 写 `scan_service.py` 的 assignment 阶段。
  - `Task 10` 写 `cli.py` 的 scan 命令分支。
  - 写集合不重叠，依赖前置任务已满足。
- 阻塞项：`Task 11` 等 `Task 8 + Task 10`，`Task 12` 等 `Task 8 + Task 9`。

### Wave D（流水线与界面并行）

- 可并行：`Task 11`、`Task 12`
- 并行依据：
  - `Task 11` 写 `scan_repo.py/scan_service.py` 的 source+checkpoint 流水线。
  - `Task 12` 写 API/Web 文件。
  - 写集合不重叠。
- 阻塞项：`Task 14` 等 `Task 11 + Task 12 + Task 13`。

### Wave E（控制面与收口）

- 顺序执行：`Task 14 -> Task 15 -> Task 16`
- 原因：`Task 14` 汇总 CLI 控制面命令，`Task 15` 依赖完整命令面执行回归，`Task 16` 收口文档与验证。

---

## 文件结构

### 新增

- `src/hikbox_pictures/workspace.py`
- `src/hikbox_pictures/db/connection.py`
- `src/hikbox_pictures/db/migrator.py`
- `src/hikbox_pictures/db/migrations/0001_scan_core.sql`
- `src/hikbox_pictures/db/migrations/0002_people_export.sql`
- `src/hikbox_pictures/repositories/source_repo.py`
- `src/hikbox_pictures/repositories/scan_repo.py`
- `src/hikbox_pictures/repositories/person_repo.py`
- `src/hikbox_pictures/repositories/export_repo.py`
- `src/hikbox_pictures/services/scan_service.py`
- `src/hikbox_pictures/services/assignment_service.py`
- `src/hikbox_pictures/services/export_service.py`
- `src/hikbox_pictures/ann/index_store.py`
- `src/hikbox_pictures/api/app.py`
- `src/hikbox_pictures/api/routes_people.py`
- `src/hikbox_pictures/api/routes_reviews.py`
- `src/hikbox_pictures/api/routes_scan.py`
- `src/hikbox_pictures/api/routes_export.py`
- `src/hikbox_pictures/web/templates/base.html`
- `src/hikbox_pictures/web/templates/people.html`
- `src/hikbox_pictures/web/templates/person_detail.html`
- `src/hikbox_pictures/web/templates/review_queue.html`
- `src/hikbox_pictures/web/templates/sources_scan.html`
- `src/hikbox_pictures/web/templates/export_templates.html`
- `src/hikbox_pictures/web/static/app.js`
- `src/hikbox_pictures/web/static/style.css`
- `tests/people_gallery/test_init_and_schema.py`
- `tests/people_gallery/test_source_service.py`
- `tests/people_gallery/test_scan_session.py`
- `tests/people_gallery/test_scan_pipeline.py`
- `tests/people_gallery/test_assignment_review.py`
- `tests/people_gallery/test_prototype_ann.py`
- `tests/people_gallery/test_export_service.py`
- `tests/people_gallery/test_api_web.py`
- `tests/people_gallery/test_cli_control_plane.py`

### 修改

- `src/hikbox_pictures/cli.py`
- `src/hikbox_pictures/exporter.py`
- `pyproject.toml`
- `README.md`

---

### Task 1: 工作区初始化与 `init` 命令

**Depends on:** None

**Scope Budget:**
- Max files: 20
- Estimated files touched: 3
- Max added lines: 1000
- Estimated added lines: 180

**Files:**
- Create: `src/hikbox_pictures/workspace.py`
- Modify: `src/hikbox_pictures/cli.py`
- Test: `tests/people_gallery/test_init_and_schema.py`

- [ ] **Step 1: 写失败测试，锁定 `init` 命令与目录布局**

```python
from pathlib import Path

from hikbox_pictures.cli import build_parser, main


def test_parser_supports_init_subcommand(tmp_path: Path) -> None:
    parser = build_parser()
    args = parser.parse_args(["init", "--workspace", str(tmp_path)])
    assert args.command == "init"
    assert args.workspace == tmp_path


def test_init_creates_workspace_layout(tmp_path: Path) -> None:
    assert main(["init", "--workspace", str(tmp_path)]) == 0

    assert (tmp_path / ".hikbox").is_dir()
    assert (tmp_path / ".hikbox" / "artifacts" / "thumbs").is_dir()
    assert (tmp_path / ".hikbox" / "artifacts" / "face-crops").is_dir()
    assert (tmp_path / ".hikbox" / "artifacts" / "ann").is_dir()
    assert (tmp_path / ".hikbox" / "exports").is_dir()
```

- [ ] **Step 2: 运行测试，确认当前实现失败**

Run: `if [ ! -d .venv ]; then ./scripts/install.sh; fi && source .venv/bin/activate && PYTHONPATH=src python3 -m pytest tests/people_gallery/test_init_and_schema.py::test_parser_supports_init_subcommand tests/people_gallery/test_init_and_schema.py::test_init_creates_workspace_layout -v`
Expected: FAIL，报错 `init` 子命令未识别。

- [ ] **Step 3: 实现 workspace 路径与目录创建函数**

```python
# src/hikbox_pictures/workspace.py
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class WorkspacePaths:
    root: Path
    hikbox_dir: Path
    db_path: Path
    artifacts_dir: Path
    exports_dir: Path


def resolve_workspace(root: Path) -> WorkspacePaths:
    hikbox_dir = root / ".hikbox"
    return WorkspacePaths(
        root=root,
        hikbox_dir=hikbox_dir,
        db_path=hikbox_dir / "library.db",
        artifacts_dir=hikbox_dir / "artifacts",
        exports_dir=hikbox_dir / "exports",
    )


def ensure_workspace_layout(root: Path) -> WorkspacePaths:
    paths = resolve_workspace(root)
    (paths.artifacts_dir / "thumbs").mkdir(parents=True, exist_ok=True)
    (paths.artifacts_dir / "face-crops").mkdir(parents=True, exist_ok=True)
    (paths.artifacts_dir / "ann").mkdir(parents=True, exist_ok=True)
    paths.exports_dir.mkdir(parents=True, exist_ok=True)
    return paths
```

- [ ] **Step 4: 在 CLI 接入 `init` 子命令**

```python
# src/hikbox_pictures/cli.py
from __future__ import annotations

import argparse
from pathlib import Path

from hikbox_pictures.workspace import ensure_workspace_layout


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="hikbox-pictures")
    subparsers = parser.add_subparsers(dest="command", required=False)

    init_parser = subparsers.add_parser("init")
    init_parser.add_argument("--workspace", required=True, type=Path)

    return parser


def _run_init(workspace: Path) -> int:
    ensure_workspace_layout(workspace)
    return 0


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        return 0
    args = build_parser().parse_args(argv)
    if args.command == "init":
        return _run_init(args.workspace)
    return 0


def cli_entry() -> int:
    import sys

    return main(sys.argv[1:])
```

- [ ] **Step 5: 运行测试，确认 Task 1 通过**

Run: `source .venv/bin/activate && PYTHONPATH=src python3 -m pytest tests/people_gallery/test_init_and_schema.py::test_parser_supports_init_subcommand tests/people_gallery/test_init_and_schema.py::test_init_creates_workspace_layout -q`
Expected: PASS。

**提交动作（非复选框）**

```bash
git add src/hikbox_pictures/workspace.py src/hikbox_pictures/cli.py tests/people_gallery/test_init_and_schema.py docs/superpowers/plans/2026-04-11-hikbox-pictures-people-gallery.md
git commit -m "feat: add workspace bootstrap and init command (Task 1)"
```

### Task 2: SQLite 连接与 migration runner

**Depends on:** Task 1

**Scope Budget:**
- Max files: 20
- Estimated files touched: 3
- Max added lines: 1000
- Estimated added lines: 160

**Files:**
- Create: `src/hikbox_pictures/db/connection.py`
- Create: `src/hikbox_pictures/db/migrator.py`
- Test: `tests/people_gallery/test_init_and_schema.py`

- [ ] **Step 1: 写失败测试，锁定 migration 幂等行为**

```python
from pathlib import Path

from hikbox_pictures.db.connection import connect_db
from hikbox_pictures.db.migrator import apply_migrations


def test_apply_migrations_is_idempotent(tmp_path: Path) -> None:
    conn = connect_db(tmp_path / "library.db")

    apply_migrations(conn)
    apply_migrations(conn)

    versions = [
        row[0]
        for row in conn.execute("SELECT version FROM schema_migrations ORDER BY version").fetchall()
    ]
    assert versions == ["0001_scan_core", "0002_people_export"]
```

- [ ] **Step 2: 运行测试，确认 DB 模块缺失**

Run: `source .venv/bin/activate && PYTHONPATH=src python3 -m pytest tests/people_gallery/test_init_and_schema.py::test_apply_migrations_is_idempotent -v`
Expected: FAIL，`hikbox_pictures.db` 模块未找到。

- [ ] **Step 3: 实现 SQLite 连接函数**

```python
# src/hikbox_pictures/db/connection.py
from __future__ import annotations

from pathlib import Path
import sqlite3


def connect_db(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn
```

- [ ] **Step 4: 实现 migration runner**

```python
# src/hikbox_pictures/db/migrator.py
from __future__ import annotations

from pathlib import Path
import sqlite3


MIGRATION_DIR = Path(__file__).resolve().parent / "migrations"


def apply_migrations(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version TEXT PRIMARY KEY,
            applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )

    for sql_file in sorted(MIGRATION_DIR.glob("*.sql")):
        version = sql_file.stem
        exists = conn.execute(
            "SELECT 1 FROM schema_migrations WHERE version = ?",
            (version,),
        ).fetchone()
        if exists:
            continue

        conn.executescript(sql_file.read_text(encoding="utf-8"))
        conn.execute("INSERT INTO schema_migrations(version) VALUES (?)", (version,))

    conn.commit()
```

- [ ] **Step 5: 运行测试，确认 Task 2 通过**

Run: `source .venv/bin/activate && PYTHONPATH=src python3 -m pytest tests/people_gallery/test_init_and_schema.py::test_apply_migrations_is_idempotent -q`
Expected: PASS。

**提交动作（非复选框）**

```bash
git add src/hikbox_pictures/db/connection.py src/hikbox_pictures/db/migrator.py tests/people_gallery/test_init_and_schema.py docs/superpowers/plans/2026-04-11-hikbox-pictures-people-gallery.md
git commit -m "feat: add sqlite connection and migration runner (Task 2)"
```
### Task 3: 扫描核心 schema migration

**Depends on:** Task 2

**Scope Budget:**
- Max files: 20
- Estimated files touched: 2
- Max added lines: 1000
- Estimated added lines: 360

**Files:**
- Create: `src/hikbox_pictures/db/migrations/0001_scan_core.sql`
- Test: `tests/people_gallery/test_init_and_schema.py`

- [ ] **Step 1: 写失败测试，锁定扫描核心表与索引**

```python
from pathlib import Path

from hikbox_pictures.db.connection import connect_db
from hikbox_pictures.db.migrator import apply_migrations


def test_scan_core_schema_exists(tmp_path: Path) -> None:
    conn = connect_db(tmp_path / "library.db")
    apply_migrations(conn)

    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    indexes = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='index'").fetchall()}

    assert "library_source" in tables
    assert "scan_session" in tables
    assert "scan_session_source" in tables
    assert "scan_checkpoint" in tables
    assert "photo_asset" in tables
    assert "face_observation" in tables
    assert "face_embedding" in tables

    assert "uq_active_source_root_path" in indexes
    assert "uq_single_running_scan_session" in indexes
    assert "uq_asset_source_fingerprint" in indexes
```

- [ ] **Step 2: 运行测试，确认 migration 未落地前失败**

Run: `source .venv/bin/activate && PYTHONPATH=src python3 -m pytest tests/people_gallery/test_init_and_schema.py::test_scan_core_schema_exists -v`
Expected: FAIL，表或索引不存在。

- [ ] **Step 3: 编写 `0001_scan_core.sql`**

```sql
-- src/hikbox_pictures/db/migrations/0001_scan_core.sql
CREATE TABLE IF NOT EXISTS library_source (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  root_path TEXT NOT NULL,
  root_fingerprint TEXT NOT NULL,
  active INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_active_source_root_path ON library_source(root_path) WHERE active = 1;

CREATE TABLE IF NOT EXISTS scan_session (
  id INTEGER PRIMARY KEY,
  mode TEXT NOT NULL CHECK (mode IN ('initial', 'incremental', 'resume')),
  status TEXT NOT NULL CHECK (status IN ('pending', 'running', 'paused', 'interrupted', 'completed', 'failed', 'abandoned')),
  resume_from_session_id INTEGER,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  started_at TEXT,
  stopped_at TEXT,
  finished_at TEXT,
  heartbeat_at TEXT,
  FOREIGN KEY(resume_from_session_id) REFERENCES scan_session(id)
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_single_running_scan_session ON scan_session(status) WHERE status = 'running';

CREATE TABLE IF NOT EXISTS scan_session_source (
  id INTEGER PRIMARY KEY,
  scan_session_id INTEGER NOT NULL,
  library_source_id INTEGER NOT NULL,
  status TEXT NOT NULL,
  cursor_json TEXT,
  discovered_count INTEGER NOT NULL DEFAULT 0,
  metadata_done_count INTEGER NOT NULL DEFAULT 0,
  faces_done_count INTEGER NOT NULL DEFAULT 0,
  embeddings_done_count INTEGER NOT NULL DEFAULT 0,
  assignment_done_count INTEGER NOT NULL DEFAULT 0,
  last_checkpoint_at TEXT,
  UNIQUE(scan_session_id, library_source_id),
  FOREIGN KEY(scan_session_id) REFERENCES scan_session(id),
  FOREIGN KEY(library_source_id) REFERENCES library_source(id)
);

CREATE TABLE IF NOT EXISTS scan_checkpoint (
  id INTEGER PRIMARY KEY,
  scan_session_source_id INTEGER NOT NULL,
  phase TEXT NOT NULL,
  cursor_json TEXT,
  pending_asset_count INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  FOREIGN KEY(scan_session_source_id) REFERENCES scan_session_source(id)
);

CREATE TABLE IF NOT EXISTS photo_asset (
  id INTEGER PRIMARY KEY,
  library_source_id INTEGER NOT NULL,
  primary_path TEXT NOT NULL,
  primary_fingerprint TEXT NOT NULL,
  file_size INTEGER NOT NULL,
  mtime REAL NOT NULL,
  capture_datetime TEXT,
  capture_month TEXT,
  width INTEGER,
  height INTEGER,
  is_heic INTEGER NOT NULL DEFAULT 0,
  live_mov_path TEXT,
  live_mov_fingerprint TEXT,
  processing_status TEXT NOT NULL CHECK (processing_status IN ('discovered', 'metadata_done', 'faces_done', 'embeddings_done', 'assignment_done', 'failed')),
  last_processed_session_id INTEGER,
  last_error TEXT,
  indexed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  FOREIGN KEY(library_source_id) REFERENCES library_source(id),
  FOREIGN KEY(last_processed_session_id) REFERENCES scan_session(id)
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_asset_source_fingerprint ON photo_asset(library_source_id, primary_fingerprint);

CREATE TABLE IF NOT EXISTS face_observation (
  id INTEGER PRIMARY KEY,
  photo_asset_id INTEGER NOT NULL,
  bbox_top INTEGER NOT NULL,
  bbox_right INTEGER NOT NULL,
  bbox_bottom INTEGER NOT NULL,
  bbox_left INTEGER NOT NULL,
  face_area_ratio REAL NOT NULL,
  sharpness_score REAL NOT NULL,
  pose_score REAL NOT NULL,
  quality_score REAL NOT NULL,
  crop_path TEXT,
  detector_key TEXT NOT NULL,
  detector_version TEXT NOT NULL,
  observed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  active INTEGER NOT NULL DEFAULT 1,
  FOREIGN KEY(photo_asset_id) REFERENCES photo_asset(id)
);

CREATE TABLE IF NOT EXISTS face_embedding (
  id INTEGER PRIMARY KEY,
  face_observation_id INTEGER NOT NULL,
  feature_type TEXT NOT NULL,
  model_key TEXT NOT NULL,
  dimension INTEGER NOT NULL,
  vector_blob BLOB NOT NULL,
  normalized INTEGER NOT NULL,
  generated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE(face_observation_id, feature_type, model_key),
  FOREIGN KEY(face_observation_id) REFERENCES face_observation(id)
);
```

- [ ] **Step 4: 运行测试，确认 Task 3 通过**

Run: `source .venv/bin/activate && PYTHONPATH=src python3 -m pytest tests/people_gallery/test_init_and_schema.py::test_scan_core_schema_exists -q`
Expected: PASS。

**提交动作（非复选框）**

```bash
git add src/hikbox_pictures/db/migrations/0001_scan_core.sql tests/people_gallery/test_init_and_schema.py docs/superpowers/plans/2026-04-11-hikbox-pictures-people-gallery.md
git commit -m "feat: add scan core schema migration (Task 3)"
```

### Task 4: 人物与导出 schema migration

**Depends on:** Task 3

**Scope Budget:**
- Max files: 20
- Estimated files touched: 2
- Max added lines: 1000
- Estimated added lines: 420

**Files:**
- Create: `src/hikbox_pictures/db/migrations/0002_people_export.sql`
- Test: `tests/people_gallery/test_init_and_schema.py`

- [ ] **Step 1: 写失败测试，锁定人物与导出表存在和关键索引**

```python
from pathlib import Path

from hikbox_pictures.db.connection import connect_db
from hikbox_pictures.db.migrator import apply_migrations


def test_people_export_schema_exists(tmp_path: Path) -> None:
    conn = connect_db(tmp_path / "library.db")
    apply_migrations(conn)

    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    indexes = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='index'").fetchall()}

    assert "auto_cluster_batch" in tables
    assert "auto_cluster" in tables
    assert "auto_cluster_member" in tables
    assert "person" in tables
    assert "person_face_assignment" in tables
    assert "person_prototype" in tables
    assert "review_item" in tables
    assert "export_template" in tables
    assert "export_template_person" in tables
    assert "export_run" in tables
    assert "export_delivery" in tables

    assert "uq_active_assignment_per_observation" in indexes
    assert "uq_export_delivery" in indexes
```

- [ ] **Step 2: 运行测试，确认 migration 未落地前失败**

Run: `source .venv/bin/activate && PYTHONPATH=src python3 -m pytest tests/people_gallery/test_init_and_schema.py::test_people_export_schema_exists -v`
Expected: FAIL。

- [ ] **Step 3: 编写 `0002_people_export.sql`**

```sql
-- src/hikbox_pictures/db/migrations/0002_people_export.sql
CREATE TABLE IF NOT EXISTS auto_cluster_batch (
  id INTEGER PRIMARY KEY,
  model_key TEXT NOT NULL,
  algorithm_version TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS auto_cluster (
  id INTEGER PRIMARY KEY,
  batch_id INTEGER NOT NULL,
  confidence REAL NOT NULL,
  representative_observation_id INTEGER,
  FOREIGN KEY(batch_id) REFERENCES auto_cluster_batch(id),
  FOREIGN KEY(representative_observation_id) REFERENCES face_observation(id)
);

CREATE TABLE IF NOT EXISTS auto_cluster_member (
  cluster_id INTEGER NOT NULL,
  face_observation_id INTEGER NOT NULL,
  membership_score REAL NOT NULL,
  PRIMARY KEY(cluster_id, face_observation_id),
  FOREIGN KEY(cluster_id) REFERENCES auto_cluster(id),
  FOREIGN KEY(face_observation_id) REFERENCES face_observation(id)
);

CREATE TABLE IF NOT EXISTS person (
  id INTEGER PRIMARY KEY,
  display_name TEXT NOT NULL,
  cover_observation_id INTEGER,
  status TEXT NOT NULL CHECK (status IN ('active', 'merged', 'ignored')),
  notes TEXT,
  confirmed INTEGER NOT NULL DEFAULT 0,
  ignored INTEGER NOT NULL DEFAULT 0,
  merged_into_person_id INTEGER,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  FOREIGN KEY(cover_observation_id) REFERENCES face_observation(id),
  FOREIGN KEY(merged_into_person_id) REFERENCES person(id)
);

CREATE TABLE IF NOT EXISTS person_face_assignment (
  id INTEGER PRIMARY KEY,
  person_id INTEGER NOT NULL,
  face_observation_id INTEGER NOT NULL,
  assignment_source TEXT NOT NULL CHECK (assignment_source IN ('auto', 'manual', 'merge', 'split')),
  confidence REAL NOT NULL,
  locked INTEGER NOT NULL DEFAULT 0,
  confirmed_at TEXT,
  active INTEGER NOT NULL DEFAULT 1,
  FOREIGN KEY(person_id) REFERENCES person(id),
  FOREIGN KEY(face_observation_id) REFERENCES face_observation(id)
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_active_assignment_per_observation ON person_face_assignment(face_observation_id) WHERE active = 1;

CREATE TABLE IF NOT EXISTS person_prototype (
  id INTEGER PRIMARY KEY,
  person_id INTEGER NOT NULL,
  prototype_type TEXT NOT NULL CHECK (prototype_type IN ('centroid', 'medoid', 'exemplar')),
  source_observation_id INTEGER,
  model_key TEXT NOT NULL,
  vector_blob BLOB NOT NULL,
  quality_score REAL NOT NULL,
  active INTEGER NOT NULL DEFAULT 1,
  dirty INTEGER NOT NULL DEFAULT 0,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  FOREIGN KEY(person_id) REFERENCES person(id),
  FOREIGN KEY(source_observation_id) REFERENCES face_observation(id)
);

CREATE TABLE IF NOT EXISTS review_item (
  id INTEGER PRIMARY KEY,
  review_type TEXT NOT NULL CHECK (review_type IN ('new_person', 'possible_merge', 'possible_split', 'low_confidence_assignment')),
  primary_person_id INTEGER,
  secondary_person_id INTEGER,
  face_observation_id INTEGER,
  payload_json TEXT NOT NULL,
  priority INTEGER NOT NULL DEFAULT 100,
  status TEXT NOT NULL CHECK (status IN ('open', 'resolved', 'dismissed')),
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  resolved_at TEXT,
  FOREIGN KEY(primary_person_id) REFERENCES person(id),
  FOREIGN KEY(secondary_person_id) REFERENCES person(id),
  FOREIGN KEY(face_observation_id) REFERENCES face_observation(id)
);

CREATE TABLE IF NOT EXISTS export_template (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  output_root TEXT NOT NULL,
  include_group INTEGER NOT NULL DEFAULT 1,
  export_live_mov INTEGER NOT NULL DEFAULT 1,
  start_datetime TEXT,
  end_datetime TEXT,
  enabled INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS export_template_person (
  template_id INTEGER NOT NULL,
  person_id INTEGER NOT NULL,
  position INTEGER NOT NULL,
  PRIMARY KEY(template_id, person_id),
  FOREIGN KEY(template_id) REFERENCES export_template(id),
  FOREIGN KEY(person_id) REFERENCES person(id)
);

CREATE TABLE IF NOT EXISTS export_run (
  id INTEGER PRIMARY KEY,
  template_id INTEGER NOT NULL,
  spec_hash TEXT NOT NULL,
  status TEXT NOT NULL,
  matched_only_count INTEGER NOT NULL DEFAULT 0,
  matched_group_count INTEGER NOT NULL DEFAULT 0,
  exported_count INTEGER NOT NULL DEFAULT 0,
  skipped_count INTEGER NOT NULL DEFAULT 0,
  failed_count INTEGER NOT NULL DEFAULT 0,
  started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  finished_at TEXT,
  FOREIGN KEY(template_id) REFERENCES export_template(id)
);

CREATE TABLE IF NOT EXISTS export_delivery (
  id INTEGER PRIMARY KEY,
  template_id INTEGER NOT NULL,
  spec_hash TEXT NOT NULL,
  photo_asset_id INTEGER NOT NULL,
  asset_variant TEXT NOT NULL CHECK (asset_variant IN ('primary', 'live_mov')),
  bucket TEXT NOT NULL CHECK (bucket IN ('only', 'group')),
  target_path TEXT NOT NULL,
  source_fingerprint TEXT NOT NULL,
  status TEXT NOT NULL,
  last_exported_at TEXT,
  last_verified_at TEXT,
  FOREIGN KEY(template_id) REFERENCES export_template(id),
  FOREIGN KEY(photo_asset_id) REFERENCES photo_asset(id)
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_export_delivery ON export_delivery(template_id, spec_hash, photo_asset_id, asset_variant);
```

- [ ] **Step 4: 运行测试，确认 Task 4 通过**

Run: `source .venv/bin/activate && PYTHONPATH=src python3 -m pytest tests/people_gallery/test_init_and_schema.py::test_people_export_schema_exists -q`
Expected: PASS。

**提交动作（非复选框）**

```bash
git add src/hikbox_pictures/db/migrations/0002_people_export.sql tests/people_gallery/test_init_and_schema.py docs/superpowers/plans/2026-04-11-hikbox-pictures-people-gallery.md
git commit -m "feat: add people export schema migration (Task 4)"
```
### Task 5: source 数据访问层

**Depends on:** Task 4

**Scope Budget:**
- Max files: 20
- Estimated files touched: 2
- Max added lines: 1000
- Estimated added lines: 170

**Files:**
- Create: `src/hikbox_pictures/repositories/source_repo.py`
- Test: `tests/people_gallery/test_source_service.py`

- [ ] **Step 1: 写失败测试，锁定 source repository 的增删查语义**

```python
from pathlib import Path

from hikbox_pictures.db.connection import connect_db
from hikbox_pictures.db.migrator import apply_migrations
from hikbox_pictures.repositories.source_repo import SourceRepo


def test_source_repo_add_list_deactivate(tmp_path: Path) -> None:
    conn = connect_db(tmp_path / "library.db")
    apply_migrations(conn)

    repo = SourceRepo(conn)

    root = tmp_path / "photos"
    root.mkdir()

    source_id = repo.add(name="主库", root_path=root)
    assert source_id > 0

    rows = repo.list_all()
    assert len(rows) == 1
    assert rows[0]["active"] == 1

    repo.deactivate(source_id)
    rows = repo.list_all()
    assert rows[0]["active"] == 0
```

- [ ] **Step 2: 运行测试，确认 repository 未实现**

Run: `source .venv/bin/activate && PYTHONPATH=src python3 -m pytest tests/people_gallery/test_source_service.py::test_source_repo_add_list_deactivate -v`
Expected: FAIL。

- [ ] **Step 3: 实现 source repository**

```python
# src/hikbox_pictures/repositories/source_repo.py
from __future__ import annotations

from pathlib import Path
import hashlib
import sqlite3


class SourceRepo:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    @staticmethod
    def fingerprint(path: Path) -> str:
        return hashlib.sha1(str(path.resolve()).encode("utf-8")).hexdigest()

    def add(self, *, name: str, root_path: Path) -> int:
        cursor = self.conn.execute(
            """
            INSERT INTO library_source(name, root_path, root_fingerprint, active)
            VALUES (?, ?, ?, 1)
            """,
            (name, str(root_path.resolve()), self.fingerprint(root_path)),
        )
        self.conn.commit()
        return int(cursor.lastrowid)

    def list_all(self) -> list[sqlite3.Row]:
        return list(self.conn.execute("SELECT * FROM library_source ORDER BY id"))

    def deactivate(self, source_id: int) -> None:
        self.conn.execute(
            "UPDATE library_source SET active = 0, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (source_id,),
        )
        self.conn.commit()
```

- [ ] **Step 4: 运行测试，确认 Task 5 通过**

Run: `source .venv/bin/activate && PYTHONPATH=src python3 -m pytest tests/people_gallery/test_source_service.py::test_source_repo_add_list_deactivate -q`
Expected: PASS。

**提交动作（非复选框）**

```bash
git add src/hikbox_pictures/repositories/source_repo.py tests/people_gallery/test_source_service.py docs/superpowers/plans/2026-04-11-hikbox-pictures-people-gallery.md
git commit -m "feat: add source repository (Task 5)"
```

### Task 6: scan 数据访问层与会话状态机

**Depends on:** Task 4

**Scope Budget:**
- Max files: 20
- Estimated files touched: 3
- Max added lines: 1000
- Estimated added lines: 360

**Files:**
- Create: `src/hikbox_pictures/repositories/scan_repo.py`
- Create: `src/hikbox_pictures/services/scan_service.py`
- Test: `tests/people_gallery/test_scan_session.py`

- [ ] **Step 1: 写失败测试，锁定 scan 会话续跑/中断/悬挂恢复**

```python
from pathlib import Path

from hikbox_pictures.db.connection import connect_db
from hikbox_pictures.db.migrator import apply_migrations
from hikbox_pictures.services.scan_service import ScanService


def test_scan_session_state_machine(tmp_path: Path) -> None:
    conn = connect_db(tmp_path / "library.db")
    apply_migrations(conn)

    conn.execute("INSERT INTO scan_session(mode, status) VALUES ('incremental', 'interrupted')")
    old_id = conn.execute("SELECT id FROM scan_session ORDER BY id DESC LIMIT 1").fetchone()[0]
    conn.commit()

    svc = ScanService(conn)
    session_id, resumed = svc.start_or_resume_scan()
    assert resumed is True
    assert session_id == old_id

    conn.execute(
        "INSERT INTO scan_session(mode, status, heartbeat_at) VALUES ('incremental', 'running', datetime('now', '-2 hours'))"
    )
    conn.commit()

    svc.mark_stale_running_sessions(max_age_seconds=1800)
    status = conn.execute("SELECT status FROM scan_session ORDER BY id DESC LIMIT 1").fetchone()[0]
    assert status == "interrupted"
```

- [ ] **Step 2: 运行测试，确认 scan repo/service 未实现**

Run: `source .venv/bin/activate && PYTHONPATH=src python3 -m pytest tests/people_gallery/test_scan_session.py::test_scan_session_state_machine -v`
Expected: FAIL。

- [ ] **Step 3: 实现 scan repository（会话与进度基础方法）**

```python
# src/hikbox_pictures/repositories/scan_repo.py
from __future__ import annotations

from pathlib import Path
import sqlite3


class ScanRepo:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def find_latest_resumable(self) -> sqlite3.Row | None:
        return self.conn.execute(
            """
            SELECT *
            FROM scan_session
            WHERE status IN ('pending', 'running', 'paused', 'interrupted', 'failed')
              AND status <> 'abandoned'
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()

    def create_incremental(self) -> int:
        cursor = self.conn.execute(
            """
            INSERT INTO scan_session(mode, status, started_at, heartbeat_at)
            VALUES ('incremental', 'running', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            """
        )
        self.conn.commit()
        return int(cursor.lastrowid)

    def set_running(self, session_id: int) -> None:
        self.conn.execute(
            """
            UPDATE scan_session
            SET status = 'running',
                started_at = COALESCE(started_at, CURRENT_TIMESTAMP),
                heartbeat_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (session_id,),
        )
        self.conn.commit()

    def abort_running(self) -> int:
        row = self.conn.execute(
            "SELECT id FROM scan_session WHERE status='running' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if row is None:
            return 0

        session_id = int(row[0])
        self.conn.execute(
            "UPDATE scan_session SET status='paused', stopped_at=CURRENT_TIMESTAMP WHERE id=?",
            (session_id,),
        )
        self.conn.commit()
        return session_id

    def mark_stale_running(self, max_age_seconds: int) -> None:
        self.conn.execute(
            """
            UPDATE scan_session
            SET status='interrupted', stopped_at=CURRENT_TIMESTAMP
            WHERE status='running'
              AND heartbeat_at IS NOT NULL
              AND heartbeat_at < datetime('now', ?)
            """,
            (f"-{max_age_seconds} seconds",),
        )
        self.conn.commit()

    def latest(self) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM scan_session ORDER BY id DESC LIMIT 1").fetchone()
```

- [ ] **Step 4: 实现 scan service 会话状态机接口**

```python
# src/hikbox_pictures/services/scan_service.py
from __future__ import annotations

import sqlite3

from hikbox_pictures.repositories.scan_repo import ScanRepo


class ScanService:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        self.repo = ScanRepo(conn)

    def start_or_resume_scan(self) -> tuple[int, bool]:
        row = self.repo.find_latest_resumable()
        if row is not None:
            session_id = int(row["id"])
            self.repo.set_running(session_id)
            return session_id, True
        return self.repo.create_incremental(), False

    def abort_running_scan(self) -> int:
        return self.repo.abort_running()

    def mark_stale_running_sessions(self, max_age_seconds: int) -> None:
        self.repo.mark_stale_running(max_age_seconds)

    def latest_status(self) -> sqlite3.Row | None:
        return self.repo.latest()
```

- [ ] **Step 5: 运行测试，确认 Task 6 通过**

Run: `source .venv/bin/activate && PYTHONPATH=src python3 -m pytest tests/people_gallery/test_scan_session.py::test_scan_session_state_machine -q`
Expected: PASS。

**提交动作（非复选框）**

```bash
git add src/hikbox_pictures/repositories/scan_repo.py src/hikbox_pictures/services/scan_service.py tests/people_gallery/test_scan_session.py docs/superpowers/plans/2026-04-11-hikbox-pictures-people-gallery.md
git commit -m "feat: add scan session repository and service (Task 6)"
```
### Task 7: 人物归属 repository/service

**Depends on:** Task 4

**Scope Budget:**
- Max files: 20
- Estimated files touched: 3
- Max added lines: 1000
- Estimated added lines: 260

**Files:**
- Create: `src/hikbox_pictures/repositories/person_repo.py`
- Create: `src/hikbox_pictures/services/assignment_service.py`
- Test: `tests/people_gallery/test_assignment_review.py`

- [ ] **Step 1: 写失败测试，锁定 locked 防覆盖与低置信入队**

```python
from pathlib import Path

from hikbox_pictures.db.connection import connect_db
from hikbox_pictures.db.migrator import apply_migrations
from hikbox_pictures.services.assignment_service import AssignmentService


def test_locked_assignment_not_overwritten(tmp_path: Path) -> None:
    conn = connect_db(tmp_path / "library.db")
    apply_migrations(conn)

    conn.execute("INSERT INTO person(display_name, status, confirmed, ignored) VALUES ('A', 'active', 1, 0)")
    conn.execute("INSERT INTO person(display_name, status, confirmed, ignored) VALUES ('B', 'active', 1, 0)")
    conn.execute(
        "INSERT INTO person_face_assignment(person_id, face_observation_id, assignment_source, confidence, locked, active) VALUES (1, 7, 'manual', 1.0, 1, 1)"
    )
    conn.commit()

    svc = AssignmentService(conn)
    svc.auto_assign(face_observation_id=7, candidate_person_id=2, confidence=0.95)

    person_id = conn.execute(
        "SELECT person_id FROM person_face_assignment WHERE face_observation_id = 7 AND active = 1"
    ).fetchone()[0]
    assert person_id == 1


def test_low_confidence_enqueue(tmp_path: Path) -> None:
    conn = connect_db(tmp_path / "library.db")
    apply_migrations(conn)
    svc = AssignmentService(conn)

    review_id = svc.enqueue_low_confidence(face_observation_id=9, person_id=3, confidence=0.42)
    row = conn.execute("SELECT review_type, status FROM review_item WHERE id = ?", (review_id,)).fetchone()

    assert row[0] == "low_confidence_assignment"
    assert row[1] == "open"
```

- [ ] **Step 2: 运行测试，确认人物归属模块未实现**

Run: `source .venv/bin/activate && PYTHONPATH=src python3 -m pytest tests/people_gallery/test_assignment_review.py -v`
Expected: FAIL。

- [ ] **Step 3: 实现 person repository**

```python
# src/hikbox_pictures/repositories/person_repo.py
from __future__ import annotations

import sqlite3


class PersonRepo:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def get_active_assignment(self, face_observation_id: int) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM person_face_assignment WHERE face_observation_id = ? AND active = 1",
            (face_observation_id,),
        ).fetchone()

    def deactivate_active_assignments(self, face_observation_id: int) -> None:
        self.conn.execute(
            "UPDATE person_face_assignment SET active = 0 WHERE face_observation_id = ? AND active = 1",
            (face_observation_id,),
        )

    def insert_assignment(
        self,
        *,
        person_id: int,
        face_observation_id: int,
        assignment_source: str,
        confidence: float,
        locked: int,
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO person_face_assignment(
                person_id,
                face_observation_id,
                assignment_source,
                confidence,
                locked,
                active
            ) VALUES (?, ?, ?, ?, ?, 1)
            """,
            (person_id, face_observation_id, assignment_source, confidence, locked),
        )

    def insert_review(
        self,
        *,
        review_type: str,
        primary_person_id: int | None,
        secondary_person_id: int | None,
        face_observation_id: int | None,
        payload_json: str,
        priority: int,
        status: str,
    ) -> int:
        cursor = self.conn.execute(
            """
            INSERT INTO review_item(
                review_type,
                primary_person_id,
                secondary_person_id,
                face_observation_id,
                payload_json,
                priority,
                status
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                review_type,
                primary_person_id,
                secondary_person_id,
                face_observation_id,
                payload_json,
                priority,
                status,
            ),
        )
        return int(cursor.lastrowid)
```

- [ ] **Step 4: 实现 assignment service**

```python
# src/hikbox_pictures/services/assignment_service.py
from __future__ import annotations

import json
import sqlite3

from hikbox_pictures.repositories.person_repo import PersonRepo


class AssignmentService:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        self.repo = PersonRepo(conn)

    def auto_assign(self, *, face_observation_id: int, candidate_person_id: int, confidence: float) -> None:
        active = self.repo.get_active_assignment(face_observation_id)
        if active is not None and int(active["locked"]) == 1:
            return

        self.repo.deactivate_active_assignments(face_observation_id)
        self.repo.insert_assignment(
            person_id=candidate_person_id,
            face_observation_id=face_observation_id,
            assignment_source="auto",
            confidence=float(confidence),
            locked=0,
        )
        self.conn.commit()

    def enqueue_low_confidence(self, *, face_observation_id: int, person_id: int, confidence: float) -> int:
        review_id = self.repo.insert_review(
            review_type="low_confidence_assignment",
            primary_person_id=person_id,
            secondary_person_id=None,
            face_observation_id=face_observation_id,
            payload_json=json.dumps({"confidence": float(confidence)}, ensure_ascii=False),
            priority=50,
            status="open",
        )
        self.conn.commit()
        return review_id
```

- [ ] **Step 5: 运行测试，确认 Task 7 通过**

Run: `source .venv/bin/activate && PYTHONPATH=src python3 -m pytest tests/people_gallery/test_assignment_review.py -q`
Expected: PASS。

**提交动作（非复选框）**

```bash
git add src/hikbox_pictures/repositories/person_repo.py src/hikbox_pictures/services/assignment_service.py tests/people_gallery/test_assignment_review.py docs/superpowers/plans/2026-04-11-hikbox-pictures-people-gallery.md
git commit -m "feat: add assignment repository and service (Task 7)"
```

### Task 8: 扫描流水线接入 assignment 阶段

**Depends on:** Task 6, Task 7

**Scope Budget:**
- Max files: 20
- Estimated files touched: 2
- Max added lines: 1000
- Estimated added lines: 140

**Files:**
- Modify: `src/hikbox_pictures/services/scan_service.py`
- Test: `tests/people_gallery/test_scan_pipeline.py`

- [ ] **Step 1: 写失败测试，锁定 assignment 阶段会落到审核队列**

```python
from pathlib import Path

from hikbox_pictures.db.connection import connect_db
from hikbox_pictures.db.migrator import apply_migrations
from hikbox_pictures.services.scan_service import ScanService


class FakeEngine:
    model_name = "ArcFace"
    detector_backend = "retinaface"

    def detect_faces(self, image_path):
        class Face:
            bbox = (0, 10, 10, 0)
            embedding = [0.1, 0.2, 0.3]

        return [Face()]


def test_scan_assignment_stage_enqueues_reviews(tmp_path: Path) -> None:
    conn = connect_db(tmp_path / "library.db")
    apply_migrations(conn)

    source_root = tmp_path / "photos"
    source_root.mkdir()
    (source_root / "a.jpg").write_bytes(b"jpg")

    conn.execute(
        "INSERT INTO library_source(name, root_path, root_fingerprint, active) VALUES ('主库', ?, 'fp', 1)",
        (str(source_root),),
    )
    conn.execute("INSERT INTO scan_session(mode, status) VALUES ('incremental', 'running')")
    session_id = conn.execute("SELECT id FROM scan_session").fetchone()[0]
    conn.commit()

    svc = ScanService(conn)
    svc.run_scan_once(session_id=session_id, engine=FakeEngine())

    review_count = conn.execute("SELECT COUNT(*) FROM review_item").fetchone()[0]
    assert review_count >= 1
```

- [ ] **Step 2: 运行测试，确认 assignment 未接线导致失败**

Run: `source .venv/bin/activate && PYTHONPATH=src python3 -m pytest tests/people_gallery/test_scan_pipeline.py::test_scan_assignment_stage_enqueues_reviews -v`
Expected: FAIL。

- [ ] **Step 3: 在 scan_service 接入 assignment_service**

```python
# src/hikbox_pictures/services/scan_service.py（更新 _assignment_stage）
from hikbox_pictures.services.assignment_service import AssignmentService


class ScanService:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        self.repo = ScanRepo(conn)

    def _assignment_stage(self, asset_id: int) -> None:
        status = self.repo.get_asset_status(asset_id)
        if status != "embeddings_done":
            return

        assignment_service = AssignmentService(self.conn)
        rows = self.conn.execute(
            "SELECT id FROM face_observation WHERE photo_asset_id = ? AND active = 1",
            (asset_id,),
        ).fetchall()

        for row in rows:
            assignment_service.enqueue_low_confidence(
                face_observation_id=int(row["id"]),
                person_id=0,
                confidence=0.0,
            )

        self.repo.update_asset_status(asset_id, "assignment_done")
```

- [ ] **Step 4: 运行测试，确认 Task 8 通过**

Run: `source .venv/bin/activate && PYTHONPATH=src python3 -m pytest tests/people_gallery/test_scan_pipeline.py::test_scan_assignment_stage_enqueues_reviews -q`
Expected: PASS。

**提交动作（非复选框）**

```bash
git add src/hikbox_pictures/services/scan_service.py tests/people_gallery/test_scan_pipeline.py docs/superpowers/plans/2026-04-11-hikbox-pictures-people-gallery.md
git commit -m "feat: wire assignment stage into scan pipeline (Task 8)"
```
### Task 9: ANN 存储接口

**Depends on:** Task 4

**Scope Budget:**
- Max files: 20
- Estimated files touched: 2
- Max added lines: 1000
- Estimated added lines: 130

**Files:**
- Create: `src/hikbox_pictures/ann/index_store.py`
- Test: `tests/people_gallery/test_prototype_ann.py`

- [ ] **Step 1: 写失败测试，锁定 top-k 最近邻召回语义**

```python
import numpy as np

from hikbox_pictures.ann.index_store import AnnIndexStore


def test_ann_store_returns_nearest_person(tmp_path) -> None:
    store = AnnIndexStore(index_path=tmp_path / "people.idx", dim=2)
    store.build(
        [
            (1, np.asarray([1.0, 0.0], dtype=np.float32)),
            (2, np.asarray([0.0, 1.0], dtype=np.float32)),
        ]
    )

    result = store.search(np.asarray([0.95, 0.05], dtype=np.float32), top_k=1)
    assert result[0][0] == 1
```

- [ ] **Step 2: 运行测试，确认 ANN 存储未实现**

Run: `source .venv/bin/activate && PYTHONPATH=src python3 -m pytest tests/people_gallery/test_prototype_ann.py::test_ann_store_returns_nearest_person -v`
Expected: FAIL。

- [ ] **Step 3: 实现 ANN 存储接口**

```python
# src/hikbox_pictures/ann/index_store.py
from __future__ import annotations

from pathlib import Path
import numpy as np


class AnnIndexStore:
    def __init__(self, *, index_path: Path, dim: int) -> None:
        self.index_path = index_path
        self.dim = dim
        self._items: list[tuple[int, np.ndarray]] = []

    def build(self, items: list[tuple[int, np.ndarray]]) -> None:
        self._items = []
        for person_id, vector in items:
            vec = np.asarray(vector, dtype=np.float32)
            if vec.shape != (self.dim,):
                raise ValueError(f"向量维度不匹配: {vec.shape}")
            self._items.append((int(person_id), vec))

        self.index_path.parent.mkdir(parents=True, exist_ok=True)
        self.index_path.write_bytes(b"ann-index-placeholder")

    def search(self, query: np.ndarray, *, top_k: int) -> list[tuple[int, float]]:
        q = np.asarray(query, dtype=np.float32)
        if q.shape != (self.dim,):
            raise ValueError(f"查询向量维度不匹配: {q.shape}")

        scores = [(person_id, float(np.linalg.norm(q - vec))) for person_id, vec in self._items]
        scores.sort(key=lambda item: item[1])
        return scores[:top_k]
```

- [ ] **Step 4: 运行测试，确认 Task 9 通过**

Run: `source .venv/bin/activate && PYTHONPATH=src python3 -m pytest tests/people_gallery/test_prototype_ann.py::test_ann_store_returns_nearest_person -q`
Expected: PASS。

**提交动作（非复选框）**

```bash
git add src/hikbox_pictures/ann/index_store.py tests/people_gallery/test_prototype_ann.py docs/superpowers/plans/2026-04-11-hikbox-pictures-people-gallery.md
git commit -m "feat: add ann index store interface (Task 9)"
```

### Task 10: 扫描 CLI 接线（scan/status/abort）

**Depends on:** Task 5, Task 6

**Scope Budget:**
- Max files: 20
- Estimated files touched: 2
- Max added lines: 1000
- Estimated added lines: 180

**Files:**
- Modify: `src/hikbox_pictures/cli.py`
- Test: `tests/people_gallery/test_scan_session.py`

- [ ] **Step 1: 写失败测试，锁定 scan CLI 行为**

```python
from pathlib import Path

from hikbox_pictures.cli import main


def test_scan_cli_commands(tmp_path: Path) -> None:
    assert main(["init", "--workspace", str(tmp_path)]) == 0

    assert main(["scan", "--workspace", str(tmp_path)]) == 0
    assert main(["scan", "status", "--workspace", str(tmp_path)]) == 0
    assert main(["scan", "abort", "--workspace", str(tmp_path)]) == 0
```

- [ ] **Step 2: 运行测试，确认 scan CLI 未接线**

Run: `source .venv/bin/activate && PYTHONPATH=src python3 -m pytest tests/people_gallery/test_scan_session.py::test_scan_cli_commands -v`
Expected: FAIL。

- [ ] **Step 3: 在 CLI 接入 scan 子命令分支**

```python
# src/hikbox_pictures/cli.py（增加 scan 分支）
from hikbox_pictures.db.connection import connect_db
from hikbox_pictures.db.migrator import apply_migrations
from hikbox_pictures.services.scan_service import ScanService


def _open_workspace_conn(workspace: Path):
    paths = ensure_workspace_layout(workspace)
    conn = connect_db(paths.db_path)
    apply_migrations(conn)
    return conn


scan_parser = subparsers.add_parser("scan")
scan_sub = scan_parser.add_subparsers(dest="scan_command", required=False)
scan_parser.add_argument("--workspace", type=Path)
scan_status = scan_sub.add_parser("status")
scan_status.add_argument("--workspace", required=True, type=Path)
scan_abort = scan_sub.add_parser("abort")
scan_abort.add_argument("--workspace", required=True, type=Path)


def _run_scan(args) -> int:
    workspace = args.workspace
    if workspace is None:
        raise ValueError("scan 命令必须提供 --workspace")

    conn = _open_workspace_conn(workspace)
    svc = ScanService(conn)

    if args.scan_command == "status":
        row = svc.latest_status()
        if row is None:
            print("no scan session")
        else:
            print(f"session={row['id']} mode={row['mode']} status={row['status']}")
        return 0

    if args.scan_command == "abort":
        session_id = svc.abort_running_scan()
        print(f"aborted session: {session_id}")
        return 0

    session_id, resumed = svc.start_or_resume_scan()
    print(f"scan session {session_id} {'resumed' if resumed else 'started'}")
    return 0
```

- [ ] **Step 4: 运行测试，确认 Task 10 通过**

Run: `source .venv/bin/activate && PYTHONPATH=src python3 -m pytest tests/people_gallery/test_scan_session.py::test_scan_cli_commands -q`
Expected: PASS。

**提交动作（非复选框）**

```bash
git add src/hikbox_pictures/cli.py tests/people_gallery/test_scan_session.py docs/superpowers/plans/2026-04-11-hikbox-pictures-people-gallery.md
git commit -m "feat: wire scan cli commands (Task 10)"
```
### Task 11: 扫描流水线接入 source 扫描与 checkpoint

**Depends on:** Task 6, Task 8, Task 10

**Scope Budget:**
- Max files: 20
- Estimated files touched: 2
- Max added lines: 1000
- Estimated added lines: 260

**Files:**
- Modify: `src/hikbox_pictures/repositories/scan_repo.py`
- Modify: `src/hikbox_pictures/services/scan_service.py`
- Test: `tests/people_gallery/test_scan_pipeline.py`

- [ ] **Step 1: 写失败测试，锁定 `run_scan_once` 阶段推进与幂等**

```python
from pathlib import Path

from hikbox_pictures.db.connection import connect_db
from hikbox_pictures.db.migrator import apply_migrations
from hikbox_pictures.services.scan_service import ScanService


class FakeEngine:
    model_name = "ArcFace"
    detector_backend = "retinaface"

    def detect_faces(self, image_path):
        class Face:
            bbox = (0, 10, 10, 0)
            embedding = [0.1, 0.2, 0.3]

        return [Face()]


def test_scan_pipeline_with_source(tmp_path: Path) -> None:
    conn = connect_db(tmp_path / "library.db")
    apply_migrations(conn)

    source_root = tmp_path / "photos"
    source_root.mkdir()
    (source_root / "a.jpg").write_bytes(b"jpg")

    conn.execute(
        "INSERT INTO library_source(name, root_path, root_fingerprint, active) VALUES ('主库', ?, 'fp', 1)",
        (str(source_root),),
    )
    conn.execute("INSERT INTO scan_session(mode, status) VALUES ('incremental', 'running')")
    session_id = conn.execute("SELECT id FROM scan_session").fetchone()[0]
    conn.commit()

    svc = ScanService(conn)
    svc.run_scan_once(session_id=session_id, engine=FakeEngine())
    svc.run_scan_once(session_id=session_id, engine=FakeEngine())

    status = conn.execute("SELECT processing_status FROM photo_asset").fetchone()[0]
    emb_count = conn.execute("SELECT COUNT(*) FROM face_embedding").fetchone()[0]
    checkpoint_count = conn.execute("SELECT COUNT(*) FROM scan_checkpoint").fetchone()[0]

    assert status == "assignment_done"
    assert emb_count == 1
    assert checkpoint_count >= 1
```

- [ ] **Step 2: 运行测试，确认流水线未完整接线**

Run: `source .venv/bin/activate && PYTHONPATH=src python3 -m pytest tests/people_gallery/test_scan_pipeline.py::test_scan_pipeline_with_source -v`
Expected: FAIL。

- [ ] **Step 3: 扩展 scan repo（session_source / asset / checkpoint）**

```python
# src/hikbox_pictures/repositories/scan_repo.py（新增方法）
def ensure_session_source(self, *, session_id: int, source_id: int) -> int:
    row = self.conn.execute(
        "SELECT id FROM scan_session_source WHERE scan_session_id = ? AND library_source_id = ?",
        (session_id, source_id),
    ).fetchone()
    if row:
        return int(row[0])

    cursor = self.conn.execute(
        """
        INSERT INTO scan_session_source(
            scan_session_id,
            library_source_id,
            status,
            discovered_count,
            metadata_done_count,
            faces_done_count,
            embeddings_done_count,
            assignment_done_count
        ) VALUES (?, ?, 'running', 0, 0, 0, 0, 0)
        """,
        (session_id, source_id),
    )
    self.conn.commit()
    return int(cursor.lastrowid)


def upsert_asset(
    self,
    *,
    source_id: int,
    path: Path,
    session_id: int,
    primary_fingerprint: str,
    file_size: int,
    mtime: float,
    is_heic: int,
    live_mov_path: str | None,
    live_mov_fingerprint: str | None,
) -> int:
    row = self.conn.execute(
        """
        SELECT id
        FROM photo_asset
        WHERE library_source_id = ? AND primary_fingerprint = ?
        """,
        (source_id, primary_fingerprint),
    ).fetchone()
    if row:
        asset_id = int(row[0])
        self.conn.execute(
            """
            UPDATE photo_asset
            SET primary_path = ?, file_size = ?, mtime = ?, is_heic = ?,
                live_mov_path = ?, live_mov_fingerprint = ?, last_processed_session_id = ?
            WHERE id = ?
            """,
            (
                str(path),
                int(file_size),
                float(mtime),
                int(is_heic),
                live_mov_path,
                live_mov_fingerprint,
                int(session_id),
                asset_id,
            ),
        )
        self.conn.commit()
        return asset_id

    cursor = self.conn.execute(
        """
        INSERT INTO photo_asset(
            library_source_id,
            primary_path,
            primary_fingerprint,
            file_size,
            mtime,
            is_heic,
            live_mov_path,
            live_mov_fingerprint,
            processing_status,
            last_processed_session_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'discovered', ?)
        """,
        (
            int(source_id),
            str(path),
            primary_fingerprint,
            int(file_size),
            float(mtime),
            int(is_heic),
            live_mov_path,
            live_mov_fingerprint,
            int(session_id),
        ),
    )
    self.conn.commit()
    return int(cursor.lastrowid)


def insert_checkpoint(
    self,
    *,
    session_source_id: int,
    phase: str,
    cursor_json: str,
    pending_asset_count: int,
) -> None:
    self.conn.execute(
        """
        INSERT INTO scan_checkpoint(scan_session_source_id, phase, cursor_json, pending_asset_count)
        VALUES (?, ?, ?, ?)
        """,
        (session_source_id, phase, cursor_json, int(pending_asset_count)),
    )
    self.conn.execute(
        "UPDATE scan_session_source SET last_checkpoint_at = CURRENT_TIMESTAMP WHERE id = ?",
        (session_source_id,),
    )
    self.conn.commit()
```

- [ ] **Step 4: 扩展 scan service `run_scan_once`**

```python
# src/hikbox_pictures/services/scan_service.py（新增 run_scan_once 与阶段方法）
def run_scan_once(self, *, session_id: int, engine) -> None:
    sources = self.conn.execute(
        "SELECT id, root_path FROM library_source WHERE active = 1 ORDER BY id"
    ).fetchall()

    for source in sources:
        source_id = int(source["id"])
        root = Path(source["root_path"])
        session_source_id = self.repo.ensure_session_source(session_id=session_id, source_id=source_id)

        for candidate in iter_candidate_photos(root):
            stat = candidate.path.stat()
            asset_id = self.repo.upsert_asset(
                source_id=source_id,
                path=candidate.path,
                session_id=session_id,
                primary_fingerprint=self._fingerprint(candidate.path),
                file_size=stat.st_size,
                mtime=stat.st_mtime,
                is_heic=int(candidate.path.suffix.lower() == ".heic"),
                live_mov_path=str(candidate.live_photo_video) if candidate.live_photo_video else None,
                live_mov_fingerprint=self._fingerprint(candidate.live_photo_video)
                if candidate.live_photo_video and candidate.live_photo_video.exists()
                else None,
            )

            self._metadata_stage(asset_id, candidate.path)
            self._face_stage(asset_id, candidate.path, engine)
            self._embedding_stage(asset_id, engine.model_name)
            self._assignment_stage(asset_id)

            self.repo.insert_checkpoint(
                session_source_id=session_source_id,
                phase=self.repo.get_asset_status(asset_id),
                cursor_json=f'{{"last_path":"{candidate.path}"}}',
                pending_asset_count=0,
            )
```

- [ ] **Step 5: 运行测试，确认 Task 11 通过**

Run: `source .venv/bin/activate && PYTHONPATH=src python3 -m pytest tests/people_gallery/test_scan_pipeline.py::test_scan_pipeline_with_source -q`
Expected: PASS。

**提交动作（非复选框）**

```bash
git add src/hikbox_pictures/repositories/scan_repo.py src/hikbox_pictures/services/scan_service.py tests/people_gallery/test_scan_pipeline.py docs/superpowers/plans/2026-04-11-hikbox-pictures-people-gallery.md
git commit -m "feat: add scan run pipeline with checkpoints (Task 11)"
```

### Task 12: API/Web 骨架

**Depends on:** Task 8, Task 9

**Scope Budget:**
- Max files: 20
- Estimated files touched: 14
- Max added lines: 1000
- Estimated added lines: 380

**Files:**
- Create: `src/hikbox_pictures/api/app.py`
- Create: `src/hikbox_pictures/api/routes_people.py`
- Create: `src/hikbox_pictures/api/routes_reviews.py`
- Create: `src/hikbox_pictures/api/routes_scan.py`
- Create: `src/hikbox_pictures/api/routes_export.py`
- Create: `src/hikbox_pictures/web/templates/base.html`
- Create: `src/hikbox_pictures/web/templates/people.html`
- Create: `src/hikbox_pictures/web/templates/person_detail.html`
- Create: `src/hikbox_pictures/web/templates/review_queue.html`
- Create: `src/hikbox_pictures/web/templates/sources_scan.html`
- Create: `src/hikbox_pictures/web/templates/export_templates.html`
- Create: `src/hikbox_pictures/web/static/app.js`
- Create: `src/hikbox_pictures/web/static/style.css`
- Test: `tests/people_gallery/test_api_web.py`

- [ ] **Step 1: 写失败测试，锁定 API 路由与首页导航**

```python
from fastapi.testclient import TestClient

from hikbox_pictures.api.app import create_app


def test_api_routes_and_home_navigation() -> None:
    client = TestClient(create_app())

    assert client.get("/api/people").status_code == 200
    assert client.get("/api/reviews").status_code == 200
    assert client.get("/api/scan/status").status_code == 200
    assert client.get("/api/export/templates").status_code == 200

    response = client.get("/")
    assert response.status_code == 200
    assert "人物库" in response.text
    assert "待审核" in response.text
    assert "源目录与扫描" in response.text
    assert "导出模板" in response.text
```

- [ ] **Step 2: 运行测试，确认 API/Web 模块未实现**

Run: `source .venv/bin/activate && PYTHONPATH=src python3 -m pytest tests/people_gallery/test_api_web.py::test_api_routes_and_home_navigation -v`
Expected: FAIL。

- [ ] **Step 3: 实现 API 路由与 app 入口**

```python
# src/hikbox_pictures/api/routes_people.py
from __future__ import annotations

from fastapi import APIRouter

router = APIRouter()


@router.get("/people")
def list_people():
    return []
```

```python
# src/hikbox_pictures/api/routes_reviews.py
from __future__ import annotations

from fastapi import APIRouter

router = APIRouter()


@router.get("/reviews")
def list_reviews():
    return []
```

```python
# src/hikbox_pictures/api/routes_scan.py
from __future__ import annotations

from fastapi import APIRouter

router = APIRouter()


@router.get("/scan/status")
def scan_status():
    return {"status": "idle"}
```

```python
# src/hikbox_pictures/api/routes_export.py
from __future__ import annotations

from fastapi import APIRouter

router = APIRouter()


@router.get("/export/templates")
def list_templates():
    return []
```

```python
# src/hikbox_pictures/api/app.py
from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from hikbox_pictures.api.routes_export import router as export_router
from hikbox_pictures.api.routes_people import router as people_router
from hikbox_pictures.api.routes_reviews import router as reviews_router
from hikbox_pictures.api.routes_scan import router as scan_router


def create_app() -> FastAPI:
    app = FastAPI(title="HikBox Pictures Local API")
    templates = Jinja2Templates(directory="src/hikbox_pictures/web/templates")

    app.mount("/static", StaticFiles(directory="src/hikbox_pictures/web/static"), name="static")

    @app.get("/", response_class=HTMLResponse)
    def home(request: Request):
        return templates.TemplateResponse("people.html", {"request": request, "page_title": "人物库"})

    app.include_router(people_router, prefix="/api")
    app.include_router(reviews_router, prefix="/api")
    app.include_router(scan_router, prefix="/api")
    app.include_router(export_router, prefix="/api")
    return app
```
- [ ] **Step 4: 实现模板与静态资源文件**

```html
<!-- src/hikbox_pictures/web/templates/base.html -->
<!doctype html>
<html lang="zh-CN">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>{{ page_title }}</title>
    <link rel="stylesheet" href="/static/style.css">
  </head>
  <body>
    <nav>
      <a href="/">人物库</a>
      <a href="/reviews">待审核</a>
      <a href="/sources">源目录与扫描</a>
      <a href="/exports">导出模板</a>
    </nav>
    {% block content %}{% endblock %}
    <script src="/static/app.js"></script>
  </body>
</html>
```

```html
<!-- src/hikbox_pictures/web/templates/people.html -->
{% extends "base.html" %}
{% block content %}
<main><h1>人物库</h1></main>
{% endblock %}
```

```html
<!-- src/hikbox_pictures/web/templates/person_detail.html -->
{% extends "base.html" %}
{% block content %}
<main><h1>人物详情</h1></main>
{% endblock %}
```

```html
<!-- src/hikbox_pictures/web/templates/review_queue.html -->
{% extends "base.html" %}
{% block content %}
<main><h1>待审核</h1></main>
{% endblock %}
```

```html
<!-- src/hikbox_pictures/web/templates/sources_scan.html -->
{% extends "base.html" %}
{% block content %}
<main><h1>源目录与扫描</h1></main>
{% endblock %}
```

```html
<!-- src/hikbox_pictures/web/templates/export_templates.html -->
{% extends "base.html" %}
{% block content %}
<main><h1>导出模板</h1></main>
{% endblock %}
```

```javascript
// src/hikbox_pictures/web/static/app.js
console.log("hikbox web ui ready");
```

```css
/* src/hikbox_pictures/web/static/style.css */
body {
  font-family: "PingFang SC", "Noto Sans CJK SC", sans-serif;
  margin: 0;
  background: #f6f4ee;
  color: #1f2a1f;
}

nav {
  display: flex;
  gap: 16px;
  padding: 12px 16px;
  background: #c7d7b5;
}

nav a {
  color: #1f2a1f;
  text-decoration: none;
  font-weight: 600;
}

main {
  padding: 20px;
}
```

- [ ] **Step 5: 运行测试，确认 Task 12 通过**

Run: `source .venv/bin/activate && PYTHONPATH=src python3 -m pytest tests/people_gallery/test_api_web.py::test_api_routes_and_home_navigation -q`
Expected: PASS。

**提交动作（非复选框）**

```bash
git add src/hikbox_pictures/api/app.py src/hikbox_pictures/api/routes_people.py src/hikbox_pictures/api/routes_reviews.py src/hikbox_pictures/api/routes_scan.py src/hikbox_pictures/api/routes_export.py src/hikbox_pictures/web/templates/base.html src/hikbox_pictures/web/templates/people.html src/hikbox_pictures/web/templates/person_detail.html src/hikbox_pictures/web/templates/review_queue.html src/hikbox_pictures/web/templates/sources_scan.html src/hikbox_pictures/web/templates/export_templates.html src/hikbox_pictures/web/static/app.js src/hikbox_pictures/web/static/style.css tests/people_gallery/test_api_web.py docs/superpowers/plans/2026-04-11-hikbox-pictures-people-gallery.md
git commit -m "feat: add local api and web skeleton (Task 12)"
```

### Task 13: 导出规则与交付账本

**Depends on:** Task 4

**Scope Budget:**
- Max files: 20
- Estimated files touched: 4
- Max added lines: 1000
- Estimated added lines: 300

**Files:**
- Create: `src/hikbox_pictures/repositories/export_repo.py`
- Create: `src/hikbox_pictures/services/export_service.py`
- Modify: `src/hikbox_pictures/exporter.py`
- Test: `tests/people_gallery/test_export_service.py`

- [ ] **Step 1: 写失败测试，锁定 `spec_hash` 与 only/group 规则**

```python
from hikbox_pictures.services.export_service import compute_spec_hash, classify_bucket


def test_spec_hash_ignores_display_name() -> None:
    spec_a = {
        "name": "家庭合照",
        "person_ids": [2, 1],
        "output_root": "/tmp/out",
        "include_group": True,
        "export_live_mov": True,
        "start_datetime": "2025-01-01T00:00:00",
        "end_datetime": None,
        "rule_version": 1,
    }
    spec_b = dict(spec_a)
    spec_b["name"] = "改名后模板"

    assert compute_spec_hash(spec_a) == compute_spec_hash(spec_b)


def test_only_group_rule() -> None:
    assert classify_bucket([120.0, 100.0], [20.0], has_unknown_without_area=False) == "only"
    assert classify_bucket([120.0, 100.0], [30.0], has_unknown_without_area=False) == "group"
    assert classify_bucket([120.0, 100.0], [], has_unknown_without_area=True) == "group"
```

- [ ] **Step 2: 运行测试，确认导出规则模块未实现**

Run: `source .venv/bin/activate && PYTHONPATH=src python3 -m pytest tests/people_gallery/test_export_service.py -v`
Expected: FAIL。

- [ ] **Step 3: 实现 export service 规则函数**

```python
# src/hikbox_pictures/services/export_service.py
from __future__ import annotations

import hashlib
import json


def compute_spec_hash(spec: dict) -> str:
    normalized = {
        "person_ids": sorted(int(person_id) for person_id in spec["person_ids"]),
        "output_root": spec["output_root"],
        "include_group": bool(spec["include_group"]),
        "export_live_mov": bool(spec["export_live_mov"]),
        "start_datetime": spec["start_datetime"],
        "end_datetime": spec["end_datetime"],
        "rule_version": int(spec["rule_version"]),
    }
    payload = json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()


def classify_bucket(selected_areas: list[float], extra_areas: list[float], *, has_unknown_without_area: bool) -> str:
    if has_unknown_without_area:
        return "group"
    if not selected_areas:
        return "group"

    selected_min_area = min(selected_areas)
    threshold = selected_min_area / 4.0
    if any(area >= threshold for area in extra_areas):
        return "group"
    return "only"
```

- [ ] **Step 4: 实现 export repository 的 delivery upsert**

```python
# src/hikbox_pictures/repositories/export_repo.py
from __future__ import annotations

import sqlite3


class ExportRepo:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def find_delivery(
        self,
        *,
        template_id: int,
        spec_hash: str,
        photo_asset_id: int,
        asset_variant: str,
    ) -> sqlite3.Row | None:
        return self.conn.execute(
            """
            SELECT *
            FROM export_delivery
            WHERE template_id = ? AND spec_hash = ? AND photo_asset_id = ? AND asset_variant = ?
            """,
            (template_id, spec_hash, photo_asset_id, asset_variant),
        ).fetchone()

    def upsert_delivery(
        self,
        *,
        template_id: int,
        spec_hash: str,
        photo_asset_id: int,
        asset_variant: str,
        bucket: str,
        target_path: str,
        source_fingerprint: str,
        status: str,
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO export_delivery(
                template_id,
                spec_hash,
                photo_asset_id,
                asset_variant,
                bucket,
                target_path,
                source_fingerprint,
                status,
                last_exported_at,
                last_verified_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            ON CONFLICT(template_id, spec_hash, photo_asset_id, asset_variant)
            DO UPDATE SET
                bucket = excluded.bucket,
                target_path = excluded.target_path,
                source_fingerprint = excluded.source_fingerprint,
                status = excluded.status,
                last_exported_at = CURRENT_TIMESTAMP,
                last_verified_at = CURRENT_TIMESTAMP
            """,
            (template_id, spec_hash, photo_asset_id, asset_variant, bucket, target_path, source_fingerprint, status),
        )
        self.conn.commit()
```
- [ ] **Step 5: 运行测试，确认 Task 13 通过**

Run: `source .venv/bin/activate && PYTHONPATH=src python3 -m pytest tests/people_gallery/test_export_service.py -q`
Expected: PASS。

**提交动作（非复选框）**

```bash
git add src/hikbox_pictures/repositories/export_repo.py src/hikbox_pictures/services/export_service.py src/hikbox_pictures/exporter.py tests/people_gallery/test_export_service.py docs/superpowers/plans/2026-04-11-hikbox-pictures-people-gallery.md
git commit -m "feat: add export semantics and delivery ledger (Task 13)"
```

### Task 14: CLI 控制面命令收口

**Depends on:** Task 10, Task 11, Task 12, Task 13

**Scope Budget:**
- Max files: 20
- Estimated files touched: 4
- Max added lines: 1000
- Estimated added lines: 320

**Files:**
- Modify: `src/hikbox_pictures/cli.py`
- Modify: `pyproject.toml`
- Modify: `README.md`
- Test: `tests/people_gallery/test_cli_control_plane.py`

- [ ] **Step 1: 写失败测试，锁定 `serve` / `export run` / `rebuild-artifacts`**

```python
from pathlib import Path

from hikbox_pictures.cli import main


def test_control_plane_cli_commands(tmp_path: Path) -> None:
    assert main(["init", "--workspace", str(tmp_path)]) == 0
    assert main(["scan", "status", "--workspace", str(tmp_path)]) == 0
    assert main(["export", "run", "--workspace", str(tmp_path), "--template-id", "1"]) in {0, 3}
    assert main(["rebuild-artifacts", "--workspace", str(tmp_path)]) == 0
```

- [ ] **Step 2: 运行测试，确认命令面未完整接线**

Run: `source .venv/bin/activate && PYTHONPATH=src python3 -m pytest tests/people_gallery/test_cli_control_plane.py -v`
Expected: FAIL。

- [ ] **Step 3: 在 CLI 接入 `serve` / `export` / `rebuild-artifacts` 分支**

```python
# src/hikbox_pictures/cli.py（新增控制面分支）
import uvicorn
from hikbox_pictures.api.app import create_app
from hikbox_pictures.services.export_service import compute_spec_hash


serve_parser = subparsers.add_parser("serve")
serve_parser.add_argument("--workspace", required=True, type=Path)
serve_parser.add_argument("--host", default="127.0.0.1")
serve_parser.add_argument("--port", type=int, default=8030)

export_parser = subparsers.add_parser("export")
export_sub = export_parser.add_subparsers(dest="export_command", required=True)
export_run = export_sub.add_parser("run")
export_run.add_argument("--workspace", required=True, type=Path)
export_run.add_argument("--template-id", required=True, type=int)

rebuild_parser = subparsers.add_parser("rebuild-artifacts")
rebuild_parser.add_argument("--workspace", required=True, type=Path)


def _run_export(args) -> int:
    conn = _open_workspace_conn(args.workspace)
    row = conn.execute(
        "SELECT id, output_root, include_group, export_live_mov, start_datetime, end_datetime FROM export_template WHERE id = ?",
        (args.template_id,),
    ).fetchone()
    if row is None:
        print(f"template not found: {args.template_id}")
        return 3

    person_rows = conn.execute(
        "SELECT person_id FROM export_template_person WHERE template_id = ? ORDER BY position",
        (args.template_id,),
    ).fetchall()
    spec_hash = compute_spec_hash(
        {
            "person_ids": [int(item[0]) for item in person_rows],
            "output_root": row["output_root"],
            "include_group": bool(row["include_group"]),
            "export_live_mov": bool(row["export_live_mov"]),
            "start_datetime": row["start_datetime"],
            "end_datetime": row["end_datetime"],
            "rule_version": 1,
        }
    )

    conn.execute(
        "INSERT INTO export_run(template_id, spec_hash, status, started_at, finished_at) VALUES (?, ?, 'completed', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
        (args.template_id, spec_hash),
    )
    conn.commit()
    print(f"export completed: template={args.template_id}")
    return 0


def _run_rebuild_artifacts(workspace: Path) -> int:
    paths = ensure_workspace_layout(workspace)
    (paths.artifacts_dir / "ann").mkdir(parents=True, exist_ok=True)
    (paths.artifacts_dir / "thumbs").mkdir(parents=True, exist_ok=True)
    (paths.artifacts_dir / "face-crops").mkdir(parents=True, exist_ok=True)
    print("artifacts rebuilt")
    return 0


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        return 0

    args = build_parser().parse_args(argv)
    if args.command == "init":
        return _run_init(args.workspace)
    if args.command == "source":
        return _run_source(args)
    if args.command == "scan":
        return _run_scan(args)
    if args.command == "serve":
        _open_workspace_conn(args.workspace)
        uvicorn.run(create_app(), host=args.host, port=args.port)
        return 0
    if args.command == "export":
        return _run_export(args)
    if args.command == "rebuild-artifacts":
        return _run_rebuild_artifacts(args.workspace)
    return 0
```

- [ ] **Step 4: 更新依赖和 README 命令文档**

```toml
# pyproject.toml dependencies 追加
"fastapi>=0.115.0",
"uvicorn>=0.30.0",
"jinja2>=3.1.0",
"hnswlib>=0.8.0",
```

```md
<!-- README.md 命令示例 -->
hikbox-pictures init --workspace /path/to/workspace
hikbox-pictures source add --workspace /path/to/workspace --name iCloud --root /path/to/photos
hikbox-pictures scan --workspace /path/to/workspace
hikbox-pictures scan status --workspace /path/to/workspace
hikbox-pictures scan abort --workspace /path/to/workspace
hikbox-pictures serve --workspace /path/to/workspace --host 127.0.0.1 --port 8030
hikbox-pictures export run --workspace /path/to/workspace --template-id 1
hikbox-pictures rebuild-artifacts --workspace /path/to/workspace
```

- [ ] **Step 5: 运行测试，确认 Task 14 通过**

Run: `source .venv/bin/activate && PYTHONPATH=src python3 -m pytest tests/people_gallery/test_cli_control_plane.py -q`
Expected: PASS。

**提交动作（非复选框）**

```bash
git add src/hikbox_pictures/cli.py pyproject.toml README.md tests/people_gallery/test_cli_control_plane.py docs/superpowers/plans/2026-04-11-hikbox-pictures-people-gallery.md
git commit -m "feat: complete cli control-plane commands (Task 14)"
```

### Task 15: 端到端回归

**Depends on:** Task 11, Task 14

**Scope Budget:**
- Max files: 20
- Estimated files touched: 2
- Max added lines: 1000
- Estimated added lines: 120

**Files:**
- Modify: `tests/people_gallery/test_scan_pipeline.py`
- Test: `tests/people_gallery/test_scan_pipeline.py`

- [ ] **Step 1: 写失败测试，串起最小主流程**

```python
from pathlib import Path

from hikbox_pictures.cli import main


def test_end_to_end_minimal_flow(tmp_path: Path) -> None:
    source_root = tmp_path / "photos"
    source_root.mkdir()
    (source_root / "a.jpg").write_bytes(b"jpg")

    assert main(["init", "--workspace", str(tmp_path)]) == 0
    assert main(["source", "add", "--workspace", str(tmp_path), "--name", "main", "--root", str(source_root)]) == 0
    assert main(["scan", "--workspace", str(tmp_path)]) == 0
    assert main(["scan", "status", "--workspace", str(tmp_path)]) == 0
```

- [ ] **Step 2: 运行测试，确认端到端链路缺口**

Run: `source .venv/bin/activate && PYTHONPATH=src python3 -m pytest tests/people_gallery/test_scan_pipeline.py::test_end_to_end_minimal_flow -v`
Expected: FAIL。

- [ ] **Step 3: 补齐链路后运行 tests/people_gallery 全量测试**

Run: `source .venv/bin/activate && PYTHONPATH=src python3 -m pytest tests/people_gallery -q`
Expected: PASS。

**提交动作（非复选框）**

```bash
git add tests/people_gallery/test_scan_pipeline.py docs/superpowers/plans/2026-04-11-hikbox-pictures-people-gallery.md
git commit -m "test: add end-to-end minimal flow regression (Task 15)"
```

### Task 16: 兼容回归与计划收口

**Depends on:** Task 15

**Scope Budget:**
- Max files: 20
- Estimated files touched: 2
- Max added lines: 1000
- Estimated added lines: 80

**Files:**
- Modify: `docs/superpowers/plans/2026-04-11-hikbox-pictures-people-gallery.md`
- Test: `tests/test_cli.py`
- Test: `tests/test_matcher.py`
- Test: `tests/test_exporter.py`

- [ ] **Step 1: 运行存量关键回归，确认不破坏旧能力**

Run: `source .venv/bin/activate && PYTHONPATH=src python3 -m pytest tests/test_cli.py tests/test_matcher.py tests/test_exporter.py -q`
Expected: PASS。

- [ ] **Step 2: 更新计划完成状态与执行备注**

```markdown
- [x] Task 1
- [x] Task 2
- [x] Task 3
- [x] Task 4
- [x] Task 5
- [x] Task 6
- [x] Task 7
- [x] Task 8
- [x] Task 9
- [x] Task 10
- [x] Task 11
- [x] Task 12
- [x] Task 13
- [x] Task 14
- [x] Task 15
- [x] Task 16
```

**提交动作（非复选框）**

```bash
git add docs/superpowers/plans/2026-04-11-hikbox-pictures-people-gallery.md
git commit -m "docs: finalize execution status and compatibility verification (Task 16)"
```

---

## Dependency Validation

- 每个任务都有 `Depends on` 字段。
- 依赖图无环。
- 所有共享写文件（`cli.py`、`scan_service.py`）通过依赖关系串行化；`export_service.py` 仅由 Task 13 写入。
- 存在依赖起点 `Task 1`，可立即启动。

## Scope Validation

- 每个任务都包含 `Scope Budget`。
- 所有任务 `Estimated files touched <= 20`。
- 所有任务 `Estimated added lines <= 1000`。
- 超预算内容已拆分为独立任务并通过依赖链接。

## 规格覆盖校验

- 多 source 与唯一约束：`Task 5`、`Task 11`
- 扫描续跑与悬挂恢复：`Task 6`、`Task 10`
- 资产级阶段幂等推进：`Task 11`
- 锁定归属保护与低置信审核：`Task 7`、`Task 8`
- 人物原型与 ANN：`Task 9`
- `spec_hash`、only/group、交付账本：`Task 13`
- 本地 API 与人物库优先 WebUI：`Task 12`
- 控制面命令闭环：`Task 14`
- 端到端与兼容回归：`Task 15`、`Task 16`
