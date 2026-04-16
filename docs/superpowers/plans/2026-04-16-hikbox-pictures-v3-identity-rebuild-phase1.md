# HikBox Pictures v3 阶段 1 身份重建 Implementation Plan（强化重写版）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax. This plan's checkbox state is the persistent progress source of truth; TodoWrite is session-local tracking. Executors may run dependency-free tasks in parallel (default max concurrency: 4).

**Goal:** 交付不依赖 mock/占位逻辑的 v3 phase1 身份重建闭环：真实 `sharpness_score/quality_score` 回填、真实 bootstrap 聚类与完整 materialize 闭环、trusted-sample 驱动 prototype/ANN、可 round-trip 的 threshold profile、非破坏评估脚本、只读调参验收页与 README 最小可执行手册。

**Architecture:** 采用“schema 一次到位 + 服务层承载算法 + 脚本层仅编排”的结构。所有破坏性动作只允许由 `scripts/rebuild_identities_v3.py` 触发；评估只允许通过 `scripts/evaluate_identity_thresholds.py` 在只读路径完成。phase1 明确允许 scan/review/actions/export 等其他既有功能暂时失效，不做封禁或兼容分支，集中资源把身份重建主链做硬。

**Tech Stack:** Python 3.12、SQLite、NumPy、FastAPI/Jinja2、pytest、Playwright（仅调参页验收）

---

## 强化范围与边界（phase1）

- 覆盖范围：migration、`identity_threshold_profile` 契约、`sharpness/quality` 真实回填、bootstrap + materialize、trusted sample、prototype/ANN、rebuild/evaluate 脚本、只读调参页、README 最小闭环。
- 非覆盖范围：phase2 前，不实现 scan/review/actions/export 的恢复与封禁逻辑；按用户授权，这些功能可暂时失效。
- 强约束：
- 禁止“固定分数/静态 cluster 决策/fixture 直写统计”替代真实算法。
- 禁止仅用计数断言代替字段级契约校验（`diagnostic_json`、`threshold_profile_id`、`cover_observation_id`、`origin_cluster_id` 等必须逐项验证）。
- 禁止“person 已创建但 seed/prototype/ANN 未完成”的中间态。

## 文件结构设计（phase1）

### 数据契约与迁移

- Create: `src/hikbox_pictures/db/migrations/0004_identity_rebuild_v3_schema.sql`
- Modify: `docs/db_schema/README.md`
- Create: `tools/build_legacy_v2_fixture.py`
- Create: `tests/data/legacy-v2-small.db`
- Create: `tests/people_gallery/test_legacy_v2_fixture_db.py`
- Create: `tests/people_gallery/test_identity_v3_schema_migration.py`
- 责任：基于仓库内可版本化的小型旧库 fixture 验证迁移可靠性，覆盖已有 `person/assignment/review/export` 数据升级，且不依赖迁移内 `PRAGMA foreign_keys=OFF`。

### 仓储层

- Create: `src/hikbox_pictures/repositories/identity_repo.py`
- Modify: `src/hikbox_pictures/repositories/asset_repo.py`
- Modify: `src/hikbox_pictures/repositories/person_repo.py`
- Modify: `src/hikbox_pictures/repositories/__init__.py`
- 责任：提供 profile round-trip、质量回填、cluster 持久化、materialize 事务、identity/export 清库等强约束读写接口。

### 服务层

- Create: `src/hikbox_pictures/services/identity_threshold_profile_service.py`
- Create: `src/hikbox_pictures/services/quality_score_service.py`
- Create: `src/hikbox_pictures/services/observation_quality_backfill_service.py`
- Create: `src/hikbox_pictures/services/identity_bootstrap_service.py`
- Create: `src/hikbox_pictures/services/identity_rebuild_service.py`
- Create: `src/hikbox_pictures/services/identity_threshold_evaluation_service.py`
- Modify: `src/hikbox_pictures/services/prototype_service.py`
- 责任：把质量分、bootstrap、materialize、prototype/ANN、评估报表全部落在 `src/`，脚本层不持有算法分支。

### 脚本层

- Create: `scripts/rebuild_identities_v3.py`
- Create: `scripts/evaluate_identity_thresholds.py`
- 责任：参数解析、事务边界、输出目录（固定写到 `.tmp/<task-name>/`）和摘要打印。

### WebUI 验收页（只读）

- Modify: `src/hikbox_pictures/api/routes_web.py`
- Modify: `src/hikbox_pictures/services/web_query_service.py`
- Create: `src/hikbox_pictures/web/templates/identity_tuning.html`
- Modify: `src/hikbox_pictures/web/templates/base.html`
- Modify: `src/hikbox_pictures/web/static/style.css`
- 责任：展示 active profile、bootstrap batch、materialized person 详情、pending cluster 诊断；不提供任何写接口。

### 测试与文档

- Create: `tests/people_gallery/test_identity_threshold_profile_contract.py`
- Create: `tests/people_gallery/test_observation_quality_backfill_service.py`
- Create: `tests/people_gallery/test_identity_bootstrap_service.py`
- Create: `tests/people_gallery/test_prototype_from_trusted_samples.py`
- Create: `tests/people_gallery/test_rebuild_identities_v3_script.py`
- Create: `tests/people_gallery/test_identity_threshold_evaluation_script.py`
- Create: `tests/people_gallery/test_identity_rebuild_v3_real_pipeline.py`
- Create: `tests/people_gallery/test_web_identity_tuning_page.py`
- Modify: `tests/people_gallery/fixtures_workspace.py`
- Modify: `tests/people_gallery/test_web_navigation.py`
- Modify: `tests/test_repo_samples.py`
- Modify: `README.md`

## Parallel Execution Plan

### Wave A（地基）

- 顺序执行：`Task 1`。
- 阻塞任务：`Task 2`、`Task 3`、`Task 4`、`Task 5`、`Task 6`、`Task 7`、`Task 8`。
- 原因：未完成 schema 与旧库升级验证前，所有后续任务都不具备可信前提。

### Wave B（顺序：profile 契约 -> 质量回填）

- 顺序执行：`Task 2` -> `Task 3`。
- 顺序理由：
- `Task 2` 先锁定 profile 激活与 embedding 绑定校验，`Task 3` 再按已激活 profile 计算质量分。
- 两任务都要修改 `tests/people_gallery/fixtures_workspace.py`，写入集合存在冲突，不能并行。
- 阻塞任务：`Task 4`（依赖 `Task 2` + `Task 3`）。

### Wave C（bootstrap + materialize 闭环）

- 顺序执行：`Task 4`。
- 阻塞任务：`Task 5`、`Task 6`、`Task 7`。
- 原因：脚本、评估、UI 全部依赖真实 materialize 结果与诊断字段。

### Wave D（并行：脚本主链 + 调参页）

- 可并行任务：`Task 5`、`Task 7`。
- 并行理由：
- `Task 5` 主要修改脚本与重建编排服务。
- `Task 7` 主要修改 route/query/template。
- 写入文件集合互斥，且都只依赖 `Task 4`。
- 阻塞任务：`Task 6`（依赖 `Task 5`）、`Task 8`（依赖 `Task 6` + `Task 7`）。

### Wave E（评估脚本）

- 顺序执行：`Task 6`。
- 阻塞任务：`Task 8`。
- 原因：`Task 6` 必须验证 `candidate-thresholds.json -> --threshold-profile` 的真实 round-trip，依赖 `Task 5`。

### Wave F（README 与最终回归）

- 顺序执行：`Task 8`。
- 原因：必须在脚本、评估、UI 定型后一次性收口。

---
### Task 1: v3 schema 落地 + 真实旧库升级验证

**Depends on:** None

**Scope Budget:**
- Max files: 20
- Estimated files touched: 12
- Max added lines: 1000
- Estimated added lines: 980

**Files:**
- Create: `src/hikbox_pictures/db/migrations/0004_identity_rebuild_v3_schema.sql`
- Create: `src/hikbox_pictures/repositories/identity_repo.py`
- Modify: `src/hikbox_pictures/repositories/asset_repo.py`
- Modify: `src/hikbox_pictures/repositories/person_repo.py`
- Modify: `src/hikbox_pictures/repositories/__init__.py`
- Create: `tools/build_legacy_v2_fixture.py`
- Create: `tests/data/legacy-v2-small.db`
- Create: `tests/people_gallery/test_legacy_v2_fixture_db.py`
- Create: `tests/people_gallery/test_identity_v3_schema_migration.py`
- Modify: `tests/people_gallery/fixtures_workspace.py`
- Modify: `tests/people_gallery/test_workspace_bootstrap.py`
- Modify: `docs/db_schema/README.md`

- [x] **Step 1: 先写失败测试，锁定“旧库升级 + 契约字段 + 约束仍生效”**

```python
# tests/people_gallery/test_identity_v3_schema_migration.py
import sqlite3
import shutil
from pathlib import Path

import pytest

from hikbox_pictures.db.migrator import apply_migrations


FIXTURE_DB = Path(__file__).resolve().parents[1] / "data" / "legacy-v2-small.db"


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return {str(row[1]) for row in rows}


def _fk_targets(conn: sqlite3.Connection, table: str) -> set[str]:
    rows = conn.execute(f"PRAGMA foreign_key_list({table})").fetchall()
    return {str(row[2]) for row in rows}


def test_upgrade_v2_workspace_preserves_existing_rows_and_adds_v3_contract(tmp_path):
    db_path = tmp_path / "legacy-v2-small.db"
    shutil.copy2(FIXTURE_DB, db_path)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    before_person = int(conn.execute("SELECT COUNT(*) AS c FROM person").fetchone()["c"])
    before_assignment = int(conn.execute("SELECT COUNT(*) AS c FROM person_face_assignment").fetchone()["c"])
    before_review = int(conn.execute("SELECT COUNT(*) AS c FROM review_item").fetchone()["c"])

    apply_migrations(conn)

    assert before_person == int(conn.execute("SELECT COUNT(*) AS c FROM person").fetchone()["c"])
    assert before_assignment == int(conn.execute("SELECT COUNT(*) AS c FROM person_face_assignment").fetchone()["c"])
    assert before_review == int(conn.execute("SELECT COUNT(*) AS c FROM review_item").fetchone()["c"])

    pfa_cols = _table_columns(conn, "person_face_assignment")
    assert "confidence" not in pfa_cols
    assert {"diagnostic_json", "threshold_profile_id"}.issubset(pfa_cols)

    person_cols = _table_columns(conn, "person")
    assert "origin_cluster_id" in person_cols
    person_fk = _fk_targets(conn, "person")
    assert "auto_cluster" in person_fk
    assert "auto_cluster_old" not in person_fk

    batch_cols = _table_columns(conn, "auto_cluster_batch")
    assert {"batch_type", "threshold_profile_id", "scan_session_id"}.issubset(batch_cols)

    cluster_cols = _table_columns(conn, "auto_cluster")
    assert {"cluster_status", "resolved_person_id", "diagnostic_json"}.issubset(cluster_cols)
    assert "confidence" not in cluster_cols

    member_cols = _table_columns(conn, "auto_cluster_member")
    assert {"quality_score_snapshot", "is_seed_candidate"}.issubset(member_cols)

    exclusion_fk = _fk_targets(conn, "person_face_exclusion")
    assert "person_face_assignment" in exclusion_fk
    assert "person_face_assignment_old" not in exclusion_fk

    assert _table_columns(conn, "identity_threshold_profile")
    assert _table_columns(conn, "person_trusted_sample")


def test_upgrade_keeps_fk_and_unique_constraints_enabled(tmp_path):
    db_path = tmp_path / "legacy-v2-small-fk.db"
    shutil.copy2(FIXTURE_DB, db_path)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    apply_migrations(conn)

    assert int(conn.execute("PRAGMA foreign_keys").fetchone()[0]) == 1

    person_id = int(conn.execute("SELECT id FROM person ORDER BY id ASC LIMIT 1").fetchone()[0])
    alt_person_id = int(conn.execute("SELECT id FROM person ORDER BY id DESC LIMIT 1").fetchone()[0])
    obs_id = int(conn.execute("SELECT id FROM face_observation ORDER BY id ASC LIMIT 1").fetchone()[0])

    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO person_face_assignment(person_id, face_observation_id, assignment_source, diagnostic_json, active) VALUES (?, ?, 'split', '{}', 0)",
            (person_id, obs_id),
        )

    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO person_face_assignment(person_id, face_observation_id, assignment_source, diagnostic_json, active) VALUES (?, ?, 'manual', '{}', 1)",
            (alt_person_id, obs_id),
        )

    conn.execute(
        "INSERT INTO person_face_assignment(person_id, face_observation_id, assignment_source, diagnostic_json, active) VALUES (?, ?, 'manual', '{}', 0)",
        (alt_person_id, obs_id),
    )

    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO person_trusted_sample(person_id, face_observation_id, trust_source, trust_score, quality_score_snapshot, threshold_profile_id, active) VALUES (999999, 1, 'bootstrap_seed', 1.0, 0.9, 1, 1)"
        )
```

- [x] **Step 2: 运行测试并确认失败**

Run: `source .venv/bin/activate && PYTHONPATH=src python3 -m pytest tests/people_gallery/test_identity_v3_schema_migration.py -v`
Expected: FAIL（0004 迁移与 helper 尚不存在）。

- [x] **Step 3: 实现迁移与仓储契约（禁止迁移内 foreign_keys OFF）**

```sql
-- src/hikbox_pictures/db/migrations/0004_identity_rebuild_v3_schema.sql
PRAGMA foreign_keys = ON;
PRAGMA defer_foreign_keys = ON;

CREATE TABLE identity_threshold_profile (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    profile_name TEXT NOT NULL,
    profile_version TEXT NOT NULL,
    quality_formula_version TEXT NOT NULL,
    embedding_feature_type TEXT NOT NULL,
    embedding_model_key TEXT NOT NULL,
    embedding_distance_metric TEXT NOT NULL,
    embedding_schema_version TEXT NOT NULL,
    quality_area_weight REAL NOT NULL,
    quality_sharpness_weight REAL NOT NULL,
    quality_pose_weight REAL NOT NULL,
    area_log_p10 REAL NOT NULL,
    area_log_p90 REAL NOT NULL,
    sharpness_log_p10 REAL NOT NULL,
    sharpness_log_p90 REAL NOT NULL,
    pose_score_p10 REAL,
    pose_score_p90 REAL,
    low_quality_threshold REAL NOT NULL,
    high_quality_threshold REAL NOT NULL,
    trusted_seed_quality_threshold REAL NOT NULL,
    bootstrap_edge_accept_threshold REAL NOT NULL,
    bootstrap_edge_candidate_threshold REAL NOT NULL,
    bootstrap_margin_threshold REAL NOT NULL,
    bootstrap_min_cluster_size INTEGER NOT NULL,
    bootstrap_min_distinct_photo_count INTEGER NOT NULL,
    bootstrap_min_high_quality_count INTEGER NOT NULL,
    bootstrap_seed_min_count INTEGER NOT NULL,
    bootstrap_seed_max_count INTEGER NOT NULL,
    assignment_auto_min_quality REAL NOT NULL,
    assignment_auto_distance_threshold REAL NOT NULL,
    assignment_auto_margin_threshold REAL NOT NULL,
    assignment_review_distance_threshold REAL NOT NULL,
    assignment_require_photo_conflict_free INTEGER NOT NULL CHECK (assignment_require_photo_conflict_free IN (0, 1)),
    trusted_min_quality REAL NOT NULL,
    trusted_centroid_distance_threshold REAL NOT NULL,
    trusted_margin_threshold REAL NOT NULL,
    trusted_block_exact_duplicate INTEGER NOT NULL CHECK (trusted_block_exact_duplicate IN (0, 1)),
    trusted_block_burst_duplicate INTEGER NOT NULL CHECK (trusted_block_burst_duplicate IN (0, 1)),
    burst_time_window_seconds INTEGER NOT NULL,
    possible_merge_distance_threshold REAL,
    possible_merge_margin_threshold REAL,
    active INTEGER NOT NULL DEFAULT 0 CHECK (active IN (0, 1)),
    activated_at TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE UNIQUE INDEX uq_identity_threshold_profile_active
  ON identity_threshold_profile(active)
  WHERE active = 1;

ALTER TABLE auto_cluster_member RENAME TO auto_cluster_member_old;
ALTER TABLE auto_cluster RENAME TO auto_cluster_old;
ALTER TABLE auto_cluster_batch RENAME TO auto_cluster_batch_old;

CREATE TABLE auto_cluster_batch (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    model_key TEXT NOT NULL,
    algorithm_version TEXT NOT NULL,
    batch_type TEXT NOT NULL CHECK (batch_type IN ('bootstrap', 'incremental')),
    threshold_profile_id INTEGER,
    scan_session_id INTEGER,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (threshold_profile_id) REFERENCES identity_threshold_profile(id),
    FOREIGN KEY (scan_session_id) REFERENCES scan_session(id)
);

CREATE TABLE auto_cluster (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id INTEGER NOT NULL,
    representative_observation_id INTEGER,
    cluster_status TEXT NOT NULL CHECK (cluster_status IN ('materialized', 'review_pending', 'review_resolved', 'ignored', 'discarded')),
    resolved_person_id INTEGER,
    diagnostic_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (batch_id) REFERENCES auto_cluster_batch(id) ON DELETE CASCADE,
    FOREIGN KEY (representative_observation_id) REFERENCES face_observation(id),
    FOREIGN KEY (resolved_person_id) REFERENCES person(id)
);

CREATE TABLE auto_cluster_member (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cluster_id INTEGER NOT NULL,
    face_observation_id INTEGER NOT NULL,
    membership_score REAL,
    quality_score_snapshot REAL,
    is_seed_candidate INTEGER NOT NULL DEFAULT 0 CHECK (is_seed_candidate IN (0, 1)),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (cluster_id) REFERENCES auto_cluster(id) ON DELETE CASCADE,
    FOREIGN KEY (face_observation_id) REFERENCES face_observation(id) ON DELETE CASCADE,
    UNIQUE (cluster_id, face_observation_id)
);

INSERT INTO auto_cluster_batch(id, model_key, algorithm_version, batch_type, threshold_profile_id, scan_session_id, created_at)
SELECT id, model_key, algorithm_version, 'bootstrap', NULL, NULL, created_at
FROM auto_cluster_batch_old;

INSERT INTO auto_cluster(id, batch_id, representative_observation_id, cluster_status, resolved_person_id, diagnostic_json, created_at)
SELECT id, batch_id, representative_observation_id, 'discarded', NULL, '{}', created_at
FROM auto_cluster_old;

INSERT INTO auto_cluster_member(id, cluster_id, face_observation_id, membership_score, quality_score_snapshot, is_seed_candidate, created_at)
SELECT id, cluster_id, face_observation_id, membership_score, NULL, 0, created_at
FROM auto_cluster_member_old;

DROP TABLE auto_cluster_member_old;
DROP TABLE auto_cluster_old;
DROP TABLE auto_cluster_batch_old;

ALTER TABLE person
  ADD COLUMN origin_cluster_id INTEGER REFERENCES auto_cluster(id);

CREATE TABLE person_trusted_sample (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    person_id INTEGER NOT NULL,
    face_observation_id INTEGER NOT NULL,
    trust_source TEXT NOT NULL CHECK (trust_source IN ('bootstrap_seed', 'manual_confirm')),
    trust_score REAL NOT NULL CHECK (trust_score >= 0.0 AND trust_score <= 1.0),
    quality_score_snapshot REAL NOT NULL,
    threshold_profile_id INTEGER NOT NULL,
    source_review_id INTEGER,
    source_auto_cluster_id INTEGER,
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (person_id) REFERENCES person(id),
    FOREIGN KEY (face_observation_id) REFERENCES face_observation(id),
    FOREIGN KEY (threshold_profile_id) REFERENCES identity_threshold_profile(id),
    FOREIGN KEY (source_review_id) REFERENCES review_item(id),
    FOREIGN KEY (source_auto_cluster_id) REFERENCES auto_cluster(id)
);
CREATE UNIQUE INDEX uq_person_trusted_sample_active_observation
  ON person_trusted_sample(face_observation_id)
  WHERE active = 1;

ALTER TABLE person_face_exclusion RENAME TO person_face_exclusion_old;
ALTER TABLE person_face_assignment RENAME TO person_face_assignment_old;

CREATE TABLE person_face_assignment (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    person_id INTEGER NOT NULL,
    face_observation_id INTEGER NOT NULL,
    assignment_source TEXT NOT NULL CHECK (assignment_source IN ('bootstrap', 'auto', 'manual', 'merge')),
    diagnostic_json TEXT NOT NULL DEFAULT '{}',
    threshold_profile_id INTEGER,
    locked INTEGER NOT NULL DEFAULT 0 CHECK (locked IN (0, 1)),
    confirmed_at TEXT,
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (person_id) REFERENCES person(id),
    FOREIGN KEY (face_observation_id) REFERENCES face_observation(id),
    FOREIGN KEY (threshold_profile_id) REFERENCES identity_threshold_profile(id)
);
CREATE UNIQUE INDEX uq_person_face_assignment_active_observation
  ON person_face_assignment(face_observation_id)
  WHERE active = 1;
INSERT INTO person_face_assignment(
    id, person_id, face_observation_id, assignment_source, diagnostic_json, threshold_profile_id, locked, confirmed_at, active, created_at, updated_at
)
SELECT
    id,
    person_id,
    face_observation_id,
    CASE WHEN assignment_source = 'split' THEN 'manual' ELSE assignment_source END,
    '{}',
    NULL,
    locked,
    confirmed_at,
    active,
    created_at,
    updated_at
FROM person_face_assignment_old;

CREATE TABLE person_face_exclusion (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    person_id INTEGER NOT NULL,
    face_observation_id INTEGER NOT NULL,
    assignment_id INTEGER,
    reason TEXT NOT NULL DEFAULT 'manual_exclude',
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (person_id) REFERENCES person(id),
    FOREIGN KEY (face_observation_id) REFERENCES face_observation(id),
    FOREIGN KEY (assignment_id) REFERENCES person_face_assignment(id),
    UNIQUE (person_id, face_observation_id)
);

CREATE INDEX idx_person_face_exclusion_observation_active
  ON person_face_exclusion(face_observation_id, active);
CREATE INDEX idx_person_face_exclusion_person_active
  ON person_face_exclusion(person_id, active);

INSERT INTO person_face_exclusion(
    id, person_id, face_observation_id, assignment_id, reason, active, created_at, updated_at
)
SELECT id, person_id, face_observation_id, assignment_id, reason, active, created_at, updated_at
FROM person_face_exclusion_old;

DROP TABLE person_face_exclusion_old;
DROP TABLE person_face_assignment_old;
```

- [x] **Step 4: 回归 migration + db_schema 文档一致性**

Run: `source .venv/bin/activate && PYTHONPATH=src python3 -m pytest tests/people_gallery/test_identity_v3_schema_migration.py tests/people_gallery/test_workspace_bootstrap.py -q`
Expected: PASS。

**Task completion action (not a checkbox step): Commit task changes and plan progress**

```bash
git add src/hikbox_pictures/db/migrations/0004_identity_rebuild_v3_schema.sql src/hikbox_pictures/repositories/identity_repo.py src/hikbox_pictures/repositories/asset_repo.py src/hikbox_pictures/repositories/person_repo.py src/hikbox_pictures/repositories/__init__.py tools/build_legacy_v2_fixture.py tests/data/legacy-v2-small.db tests/people_gallery/test_legacy_v2_fixture_db.py tests/people_gallery/test_identity_v3_schema_migration.py tests/people_gallery/fixtures_workspace.py tests/people_gallery/test_workspace_bootstrap.py docs/db_schema/README.md docs/superpowers/plans/2026-04-16-hikbox-pictures-v3-identity-rebuild-phase1.md
git commit -m "feat: add v3 schema migration with real legacy-upgrade validation (Task 1)"
```

### Task 2: `identity_threshold_profile` round-trip 契约硬化

**Depends on:** Task 1

**Scope Budget:**
- Max files: 20
- Estimated files touched: 4
- Max added lines: 1000
- Estimated added lines: 560

**Files:**
- Create: `src/hikbox_pictures/services/identity_threshold_profile_service.py`
- Modify: `src/hikbox_pictures/repositories/identity_repo.py`
- Create: `tests/people_gallery/test_identity_threshold_profile_contract.py`
- Modify: `tests/people_gallery/fixtures_workspace.py`

- [x] **Step 1: 写失败测试，锁定 JSON<->表列一一对应与激活约束**

```python
# tests/people_gallery/test_identity_threshold_profile_contract.py
import json

import pytest

from hikbox_pictures.services.identity_threshold_profile_service import IdentityThresholdProfileService


def test_candidate_profile_keys_must_exactly_match_table_columns(identity_seed_workspace):
    svc = IdentityThresholdProfileService(identity_seed_workspace.conn)
    candidate = svc.build_candidate_profile_from_active()
    assert set(candidate.keys()) == set(svc.roundtrip_columns())


def test_import_rejects_missing_or_extra_keys(identity_seed_workspace):
    svc = IdentityThresholdProfileService(identity_seed_workspace.conn)
    candidate = svc.build_candidate_profile_from_active()

    broken_missing = dict(candidate)
    broken_missing.pop("bootstrap_margin_threshold")
    with pytest.raises(ValueError, match="缺失字段"):
        svc.insert_candidate_profile_from_json_dict(broken_missing)

    broken_extra = dict(candidate)
    broken_extra["unexpected_key"] = 1
    with pytest.raises(ValueError, match="非法字段"):
        svc.insert_candidate_profile_from_json_dict(broken_extra)


@pytest.mark.parametrize(
    ("binding_key", "bad_value"),
    [
        ("embedding_feature_type", "body"),
        ("embedding_model_key", "non-existent-model"),
        ("embedding_distance_metric", "l2"),
        ("embedding_schema_version", "face_embedding.v999"),
    ],
)
def test_activate_profile_rejects_embedding_binding_mismatch(identity_seed_workspace, binding_key, bad_value):
    svc = IdentityThresholdProfileService(identity_seed_workspace.conn)
    candidate = svc.build_candidate_profile_from_active()
    candidate[binding_key] = bad_value
    profile_id = svc.insert_candidate_profile_from_json_dict(candidate)

    with pytest.raises(ValueError, match="embedding"):
        svc.activate_profile(profile_id)


def test_activate_profile_rejects_invalid_bootstrap_seed_relation(identity_seed_workspace):
    svc = IdentityThresholdProfileService(identity_seed_workspace.conn)
    candidate = svc.build_candidate_profile_from_active()
    candidate["bootstrap_min_high_quality_count"] = 2
    candidate["bootstrap_seed_min_count"] = 3
    profile_id = svc.insert_candidate_profile_from_json_dict(candidate)

    with pytest.raises(ValueError, match="bootstrap_min_high_quality_count"):
        svc.activate_profile(profile_id)


def test_profile_roundtrip_export_then_import(identity_seed_workspace, tmp_path):
    svc = IdentityThresholdProfileService(identity_seed_workspace.conn)
    current = svc.build_candidate_profile_from_active()

    out = tmp_path / "candidate-thresholds.json"
    out.write_text(json.dumps(current, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    reloaded = json.loads(out.read_text(encoding="utf-8"))
    profile_id = svc.insert_candidate_profile_from_json_dict(reloaded)
    svc.activate_profile(profile_id)

    active = svc.get_active_profile()
    assert int(active["id"]) == profile_id
```

- [x] **Step 2: 运行测试并确认失败**

Run: `source .venv/bin/activate && PYTHONPATH=src python3 -m pytest tests/people_gallery/test_identity_threshold_profile_contract.py -v`
Expected: FAIL（service 尚不存在，激活前约束校验未实现）。

- [x] **Step 3: 实现 strict round-trip service 与激活前置校验**

```python
# src/hikbox_pictures/services/identity_threshold_profile_service.py
class IdentityThresholdProfileService:
    SYSTEM_COLUMNS = {"id", "active", "activated_at", "created_at", "updated_at"}
    EMBEDDING_BINDING_COLUMNS = {
        "embedding_feature_type",
        "embedding_model_key",
        "embedding_distance_metric",
        "embedding_schema_version",
    }

    def roundtrip_columns(self) -> list[str]:
        rows = self.conn.execute("PRAGMA table_info(identity_threshold_profile)").fetchall()
        columns = [str(row["name"] if isinstance(row, dict) else row[1]) for row in rows]
        return [name for name in columns if name not in self.SYSTEM_COLUMNS]

    def validate_candidate_keys(self, candidate: dict[str, object]) -> None:
        required = set(self.roundtrip_columns())
        incoming = set(candidate.keys())
        missing = sorted(required - incoming)
        extra = sorted(incoming - required)
        if missing:
            raise ValueError(f"candidate profile 缺失字段: {missing}")
        if extra:
            raise ValueError(f"candidate profile 非法字段: {extra}")

    def activate_profile(self, profile_id: int) -> None:
        profile = self.repo.get_profile(profile_id)
        self._validate_activation_preconditions(profile)
        self.repo.activate_profile_transactional(profile_id)

    def _validate_activation_preconditions(self, profile: dict[str, object]) -> None:
        if int(profile["bootstrap_min_high_quality_count"]) < int(profile["bootstrap_seed_min_count"]):
            raise ValueError("bootstrap_min_high_quality_count 不得小于 bootstrap_seed_min_count")
        workspace_binding = self.repo.detect_workspace_embedding_binding()
        profile_binding = {k: profile[k] for k in self.EMBEDDING_BINDING_COLUMNS}
        if profile_binding != workspace_binding:
            raise ValueError(f"embedding 绑定不匹配: profile={profile_binding}, workspace={workspace_binding}")
```

说明：`scripts/rebuild_identities_v3.py` 对 `--threshold-profile` 的接入放在 `Task 5` 完成，避免与脚本创建顺序冲突。

- [x] **Step 4: 运行 profile 契约回归**

Run: `source .venv/bin/activate && PYTHONPATH=src python3 -m pytest tests/people_gallery/test_identity_threshold_profile_contract.py -q`
Expected: PASS。

**Task completion action (not a checkbox step): Commit task changes and plan progress**

```bash
git add src/hikbox_pictures/services/identity_threshold_profile_service.py src/hikbox_pictures/repositories/identity_repo.py tests/people_gallery/test_identity_threshold_profile_contract.py tests/people_gallery/fixtures_workspace.py docs/superpowers/plans/2026-04-16-hikbox-pictures-v3-identity-rebuild-phase1.md
git commit -m "feat: harden identity threshold profile roundtrip contract (Task 2)"
```
### Task 3: `sharpness_score/quality_score` 真实回填主链落地

**Depends on:** Task 2

**Scope Budget:**
- Max files: 20
- Estimated files touched: 6
- Max added lines: 1000
- Estimated added lines: 780

**Files:**
- Create: `src/hikbox_pictures/services/quality_score_service.py`
- Create: `src/hikbox_pictures/services/observation_quality_backfill_service.py`
- Modify: `src/hikbox_pictures/repositories/asset_repo.py`
- Modify: `src/hikbox_pictures/repositories/identity_repo.py`
- Modify: `src/hikbox_pictures/services/asset_stage_runner.py`
- Create: `tests/people_gallery/test_observation_quality_backfill_service.py`
- Modify: `tests/people_gallery/fixtures_workspace.py`

- [x] **Step 1: 写失败测试，锁定真实 I/O 回填 + area/sharpness 分位点回传 + 导入 profile 不被覆盖**

```python
# tests/people_gallery/test_observation_quality_backfill_service.py
import pytest

from hikbox_pictures.services.observation_quality_backfill_service import ObservationQualityBackfillService


def test_backfill_reads_crop_or_recrops_from_original(identity_real_workspace):
    svc = ObservationQualityBackfillService(identity_real_workspace.conn)

    obs_keep_crop = identity_real_workspace.pick_observation_with_crop()
    obs_force_recrop = identity_real_workspace.pick_observation_with_crop()
    identity_real_workspace.break_crop_for_observation(obs_force_recrop)

    report = svc.backfill_all_observations(profile_id=identity_real_workspace.profile_id)
    assert report["updated_observation_count"] >= 2

    row_a = identity_real_workspace.get_observation(obs_keep_crop)
    row_b = identity_real_workspace.get_observation(obs_force_recrop)
    assert float(row_a["sharpness_score"]) > 0.0
    assert float(row_b["sharpness_score"]) > 0.0
    assert float(row_a["sharpness_score"]) != float(row_b["sharpness_score"])
    assert row_b["quality_score"] is not None


def test_backfill_fails_if_crop_and_original_both_missing(identity_real_workspace):
    svc = ObservationQualityBackfillService(identity_real_workspace.conn)
    obs_id, photo_id = identity_real_workspace.pick_observation_and_photo()
    identity_real_workspace.break_crop_for_observation(obs_id)
    identity_real_workspace.break_original_for_photo(photo_id)

    with pytest.raises(FileNotFoundError):
        svc.backfill_all_observations(profile_id=identity_real_workspace.profile_id)


def test_backfill_returns_sharpness_quantiles_for_orchestrator(identity_real_workspace):
    svc = ObservationQualityBackfillService(identity_real_workspace.conn)
    report = svc.backfill_all_observations(
        profile_id=identity_real_workspace.profile_id,
        update_profile_quantiles=False,
    )
    assert float(report["sharpness_log_p90"]) > float(report["sharpness_log_p10"])
    assert float(report["area_log_p90"]) > float(report["area_log_p10"])


def test_backfill_does_not_rewrite_profile_quantiles_when_disabled(identity_real_workspace):
    svc = ObservationQualityBackfillService(identity_real_workspace.conn)
    before = identity_real_workspace.get_profile(identity_real_workspace.profile_id)
    svc.backfill_all_observations(
        profile_id=identity_real_workspace.profile_id,
        update_profile_quantiles=False,
    )
    after = identity_real_workspace.get_profile(identity_real_workspace.profile_id)
    assert float(after["area_log_p10"]) == float(before["area_log_p10"])
    assert float(after["area_log_p90"]) == float(before["area_log_p90"])
    assert float(after["sharpness_log_p10"]) == float(before["sharpness_log_p10"])
    assert float(after["sharpness_log_p90"]) == float(before["sharpness_log_p90"])


def test_backfill_can_update_profile_quantiles_when_explicitly_enabled(identity_real_workspace):
    svc = ObservationQualityBackfillService(identity_real_workspace.conn)
    svc.backfill_all_observations(
        profile_id=identity_real_workspace.profile_id,
        update_profile_quantiles=True,
    )
    profile = identity_real_workspace.get_profile(identity_real_workspace.profile_id)
    assert float(profile["area_log_p90"]) > float(profile["area_log_p10"])
    assert float(profile["sharpness_log_p90"]) > float(profile["sharpness_log_p10"])
```

- [x] **Step 2: 运行测试并确认失败**

Run: `source .venv/bin/activate && PYTHONPATH=src python3 -m pytest tests/people_gallery/test_observation_quality_backfill_service.py -v`
Expected: FAIL（真实 backfill 服务不存在）。

- [x] **Step 3: 实现 backfill_all_observations() 真实链路（不得 fixture 直写分数）**

```python
# src/hikbox_pictures/services/observation_quality_backfill_service.py
class ObservationQualityBackfillService:
    def backfill_all_observations(self, *, profile_id: int, update_profile_quantiles: bool = False) -> dict[str, int | float]:
        rows = self.asset_repo.list_active_observations_for_quality_backfill()

        # 1) 先从 active observation 计算 area 分位点（不依赖图像 I/O）
        area_logs = [math.log10(max(float(row["face_area_ratio"] or 0.0), 1e-6)) for row in rows]
        area_p10, area_p90 = self._quantile_pair(area_logs)

        # 2) 回填原始 sharpness_score（读取 crop；缺失则回退原图重裁）
        sharpness_logs: list[float] = []
        for row in rows:
            crop_path = self._resolve_or_rebuild_crop_path(row)
            sharpness_raw = self._compute_sharpness_raw(crop_path)
            self.asset_repo.update_observation_sharpness_score(int(row["id"]), sharpness_raw)
            sharpness_logs.append(math.log1p(max(sharpness_raw, 0.0)))

        # 3) 计算 sharpness 分位点；是否写回 profile 由 orchestrator 显式控制
        sharpness_p10, sharpness_p90 = self._quantile_pair(sharpness_logs)
        if update_profile_quantiles:
            self.identity_repo.update_profile_quality_quantiles(
                profile_id=profile_id,
                area_log_p10=area_p10,
                area_log_p90=area_p90,
                sharpness_log_p10=sharpness_p10,
                sharpness_log_p90=sharpness_p90,
            )

        # 4) 基于 active profile 回填 quality_score
        profile = self.identity_repo.get_profile(profile_id)
        for row in rows:
            sharpness = self.asset_repo.get_observation_sharpness(int(row["id"]))
            score = self.quality_score_service.compute_quality_score(
                face_area_ratio=row["face_area_ratio"],
                sharpness_score=sharpness,
                pose_score=row["pose_score"],
                profile=profile,
            )
            self.asset_repo.update_observation_quality_score(int(row["id"]), score)

        return {
            "updated_observation_count": len(rows),
            "area_log_p10": float(area_p10),
            "area_log_p90": float(area_p90),
            "sharpness_log_p10": float(sharpness_p10),
            "sharpness_log_p90": float(sharpness_p90),
        }
```

```python
# src/hikbox_pictures/services/quality_score_service.py
class QualityScoreService:
    def compute_quality_score(self, *, face_area_ratio, sharpness_score, pose_score, profile):
        area_score = self._normalize(math.log10(max(float(face_area_ratio or 0.0), 1e-6)), float(profile["area_log_p10"]), float(profile["area_log_p90"]))
        sharpness_norm = self._normalize(math.log1p(max(float(sharpness_score or 0.0), 0.0)), float(profile["sharpness_log_p10"]), float(profile["sharpness_log_p90"]))
        pose_norm = 0.0 if pose_score is None else float(pose_score)
        return max(0.0, min(1.0,
            float(profile["quality_area_weight"]) * area_score
            + float(profile["quality_sharpness_weight"]) * sharpness_norm
            + float(profile.get("quality_pose_weight") or 0.0) * pose_norm
        ))
```

- [x] **Step 4: 运行回填链路测试 + 真实数据冒烟**

Run: `source .venv/bin/activate && PYTHONPATH=src python3 -m pytest tests/people_gallery/test_observation_quality_backfill_service.py tests/people_gallery/test_real_face_pipeline.py -q`
Expected: PASS（含真实图片链路）。

**Task completion action (not a checkbox step): Commit task changes and plan progress**

```bash
git add src/hikbox_pictures/services/quality_score_service.py src/hikbox_pictures/services/observation_quality_backfill_service.py src/hikbox_pictures/repositories/asset_repo.py src/hikbox_pictures/repositories/identity_repo.py src/hikbox_pictures/services/asset_stage_runner.py tests/people_gallery/test_observation_quality_backfill_service.py tests/people_gallery/fixtures_workspace.py docs/superpowers/plans/2026-04-16-hikbox-pictures-v3-identity-rebuild-phase1.md
git commit -m "feat: implement real sharpness and quality backfill chain (Task 3)"
```

### Task 4: Bootstrap + Materialize + Trusted Prototype/ANN 完整闭环

**Depends on:** Task 2, Task 3

**Scope Budget:**
- Max files: 20
- Estimated files touched: 8
- Max added lines: 1000
- Estimated added lines: 980

**Files:**
- Create: `src/hikbox_pictures/services/identity_bootstrap_service.py`
- Modify: `src/hikbox_pictures/services/prototype_service.py`
- Modify: `src/hikbox_pictures/repositories/identity_repo.py`
- Modify: `src/hikbox_pictures/repositories/person_repo.py`
- Modify: `src/hikbox_pictures/cli.py`
- Create: `tests/people_gallery/test_identity_bootstrap_service.py`
- Create: `tests/people_gallery/test_prototype_from_trusted_samples.py`
- Modify: `tests/people_gallery/fixtures_workspace.py`

- [x] **Step 1: 写失败测试，锁定算法约束与闭环字段**

```python
# tests/people_gallery/test_identity_bootstrap_service.py
from hikbox_pictures.services.identity_bootstrap_service import IdentityBootstrapService


def test_bootstrap_enforces_edge_rules_and_persists_diagnostics(identity_seed_workspace):
    identity_seed_workspace.seed_edge_rule_challenge_case()
    svc = IdentityBootstrapService(identity_seed_workspace.conn)
    report = svc.run_bootstrap(profile_id=identity_seed_workspace.profile_id)

    assert report["materialized_cluster_count"] >= 1
    assert report["review_pending_cluster_count"] >= 1
    assert report["edge_reject_counts"]["not_mutual"] >= 1
    assert report["edge_reject_counts"]["distance_recheck_failed"] >= 1
    assert report["edge_reject_counts"]["photo_conflict"] >= 1

    pending = identity_seed_workspace.conn.execute(
        """
        SELECT diagnostic_json
        FROM auto_cluster
        WHERE cluster_status = 'review_pending'
        ORDER BY id ASC
        LIMIT 1
        """
    ).fetchone()
    payload = identity_seed_workspace.parse_json(pending["diagnostic_json"])

    assert payload["cluster_size"] >= 1
    assert payload["distinct_photo_count"] >= 1
    assert payload["selected_seed_count"] >= 0
    assert payload["pre_dedup_seed_candidate_count"] >= payload["selected_seed_count"]
    assert "quality_distribution" in payload
    assert "external_margin" in payload
    assert payload["edge_reject_counts"]["not_mutual"] >= 0
    assert payload["edge_reject_counts"]["distance_recheck_failed"] >= 0
    assert payload["edge_reject_counts"]["photo_conflict"] >= 0
    assert payload["dedup_drop_counts"]["exact_duplicate"] >= 0
    assert payload["dedup_drop_counts"]["burst_duplicate"] >= 0
    assert payload.get("reject_reason") is not None


def test_materialize_transaction_creates_person_assignment_seed_cover(identity_seed_workspace):
    svc = IdentityBootstrapService(identity_seed_workspace.conn)
    svc.run_bootstrap(profile_id=identity_seed_workspace.profile_id)

    person = identity_seed_workspace.conn.execute(
        """
        SELECT id, origin_cluster_id, cover_observation_id
        FROM person
        WHERE display_name LIKE '未命名人物 %'
        ORDER BY id ASC
        LIMIT 1
        """
    ).fetchone()
    assert person is not None
    assert person["origin_cluster_id"] is not None
    assert person["cover_observation_id"] is not None

    assign_rows = identity_seed_workspace.conn.execute(
        """
        SELECT assignment_source, threshold_profile_id, diagnostic_json
        FROM person_face_assignment
        WHERE person_id = ? AND active = 1
        """,
        (int(person["id"]),),
    ).fetchall()
    assert assign_rows
    for row in assign_rows:
        assert row["assignment_source"] == "bootstrap"
        assert int(row["threshold_profile_id"]) == identity_seed_workspace.profile_id
        diag = identity_seed_workspace.parse_json(row["diagnostic_json"])
        assert diag["decision_kind"] == "bootstrap_materialize"
        assert diag["auto_cluster_id"] is not None

    seed_count = identity_seed_workspace.conn.execute(
        """
        SELECT COUNT(*) AS c
        FROM person_trusted_sample
        WHERE person_id = ? AND active = 1 AND trust_source = 'bootstrap_seed'
        """,
        (int(person["id"]),),
    ).fetchone()
    assert int(seed_count["c"]) >= 3


def test_seed_insufficient_after_dedup_never_creates_person(identity_seed_workspace):
    identity_seed_workspace.seed_bootstrap_dedup_collision_case()
    svc = IdentityBootstrapService(identity_seed_workspace.conn)
    svc.run_bootstrap(profile_id=identity_seed_workspace.profile_id)

    rejected = identity_seed_workspace.conn.execute(
        """
        SELECT diagnostic_json
        FROM auto_cluster
        WHERE cluster_status = 'review_pending'
          AND json_extract(diagnostic_json, '$.reject_reason') = 'seed_insufficient_after_dedup'
        """
    ).fetchall()
    assert rejected
    for row in rejected:
        diag = identity_seed_workspace.parse_json(row["diagnostic_json"])
        assert diag["pre_dedup_seed_candidate_count"] > diag["selected_seed_count"]
        assert diag["dedup_drop_counts"]["exact_duplicate"] >= 1
        assert diag["dedup_drop_counts"]["burst_duplicate"] >= 1

    leaked = identity_seed_workspace.conn.execute(
        """
        SELECT COUNT(*) AS c
        FROM person p
        JOIN auto_cluster c ON c.resolved_person_id = p.id
        WHERE json_extract(c.diagnostic_json, '$.reject_reason') = 'seed_insufficient_after_dedup'
        """
    ).fetchone()
    assert int(leaked["c"]) == 0


def test_ann_sync_failure_does_not_leave_materialized_half_state(identity_seed_workspace):
    svc = IdentityBootstrapService(identity_seed_workspace.conn)
    identity_seed_workspace.fail_next_ann_sync()

    svc.run_bootstrap(profile_id=identity_seed_workspace.profile_id)

    leaked = identity_seed_workspace.conn.execute(
        """
        SELECT COUNT(*) AS c
        FROM auto_cluster c
        JOIN person p ON p.id = c.resolved_person_id
        WHERE c.cluster_status = 'materialized'
          AND json_extract(c.diagnostic_json, '$.reject_reason') = 'artifact_rebuild_failed'
        """
    ).fetchone()
    assert int(leaked["c"]) == 0

    pending = identity_seed_workspace.conn.execute(
        """
        SELECT COUNT(*) AS c
        FROM auto_cluster
        WHERE cluster_status = 'review_pending'
          AND json_extract(diagnostic_json, '$.reject_reason') = 'artifact_rebuild_failed'
        """
    ).fetchone()
    assert int(pending["c"]) >= 1
```

```python
# tests/people_gallery/test_prototype_from_trusted_samples.py
from hikbox_pictures.services.prototype_service import PrototypeService


def test_prototype_reads_person_trusted_sample_only(identity_seed_workspace):
    service = PrototypeService(identity_seed_workspace.conn, identity_seed_workspace.person_repo, identity_seed_workspace.ann_store)
    rebuilt = service.rebuild_all_person_prototypes(model_key="pipeline-stub-v1")
    assert rebuilt >= 1

    row = identity_seed_workspace.conn.execute(
        "SELECT quality_score FROM person_prototype WHERE active = 1 ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert row is not None
    assert float(row["quality_score"]) > 0.0
```

- [x] **Step 2: 运行测试并确认失败**

Run: `source .venv/bin/activate && PYTHONPATH=src python3 -m pytest tests/people_gallery/test_identity_bootstrap_service.py tests/people_gallery/test_prototype_from_trusted_samples.py -v`
Expected: FAIL（bootstrap/materialize/prototype 尚未改造）。

- [x] **Step 3: 实现算法硬约束 + 事务闭环 + trusted 驱动 prototype**

```python
# src/hikbox_pictures/services/identity_bootstrap_service.py
class IdentityBootstrapService:
    def run_bootstrap(self, *, profile_id: int) -> dict[str, object]:
        profile = self.identity_repo.get_profile(profile_id)
        model_key = self._resolve_model_key_from_profile(profile)
        candidates = self.identity_repo.list_high_quality_observations(profile_id=profile_id, model_key=model_key)

        # 所有边必须经过：互为近邻 + 精确距离复核 + margin + 照片冲突
        edge_result = self._build_mutual_edges(candidates, profile=profile)
        clusters = self._build_clusters_from_edges(candidates, edge_result.accepted_edges)
        batch_id = self.identity_repo.create_bootstrap_batch(profile_id=profile_id, model_key=model_key)

        summary = {
            "materialized_cluster_count": 0,
            "review_pending_cluster_count": 0,
            "discarded_cluster_count": 0,
            "edge_reject_counts": {
                "not_mutual": int(edge_result.reject_counts.get("not_mutual", 0)),
                "distance_recheck_failed": int(edge_result.reject_counts.get("distance_recheck_failed", 0)),
                "photo_conflict": int(edge_result.reject_counts.get("photo_conflict", 0)),
            },
        }
        for cluster in clusters:
            seed_ids, dedup_stats = self._select_seed_after_exact_and_burst_dedup(cluster, profile=profile)
            decision = self._decide_cluster(
                cluster=cluster,
                seed_ids=seed_ids,
                profile=profile,
                dedup_stats=dedup_stats,
                edge_reject_counts=summary["edge_reject_counts"],
            )
            cluster_id = self.identity_repo.insert_cluster_with_members(batch_id=batch_id, cluster=cluster, decision=decision, profile_id=profile_id)

            if decision["cluster_status"] != "materialized":
                summary[f"{decision['cluster_status']}_cluster_count"] += 1
                continue

            # DB 闭环先提交：person + assignment + trusted_sample + cover + cluster 状态
            with self.identity_repo.transaction():
                person_id = self.person_repo.create_anonymous_person(origin_cluster_id=cluster_id, sequence=self.identity_repo.next_anonymous_sequence())
                self.identity_repo.insert_bootstrap_assignments(person_id=person_id, cluster=cluster, cluster_id=cluster_id, profile_id=profile_id)
                self.identity_repo.insert_bootstrap_seeds(person_id=person_id, cluster_id=cluster_id, seed_ids=seed_ids, profile_id=profile_id)
                cover_id = self.identity_repo.pick_cover_from_seed(cluster_id=cluster_id, person_id=person_id)
                self.person_repo.set_cover_observation(person_id=person_id, cover_observation_id=cover_id)
                self.identity_repo.mark_cluster_materialized(cluster_id=cluster_id, person_id=person_id)

            # 文件域不是 DB 事务的一部分，必须做补偿，保证“无半状态”
            try:
                self.prototype_service.rebuild_person_prototype(person_id=person_id, model_key=model_key)
                self.prototype_service.sync_person_ann_entry_atomic(person_id=person_id, model_key=model_key)
            except Exception:
                self.identity_repo.compensate_materialize_failure(
                    cluster_id=cluster_id,
                    person_id=person_id,
                    reject_reason="artifact_rebuild_failed",
                )
                summary["review_pending_cluster_count"] += 1
                continue

            summary["materialized_cluster_count"] += 1

        return summary
```

```python
# src/hikbox_pictures/services/prototype_service.py
# 核心改动：输入源从 person_face_assignment 切到 person_trusted_sample
SELECT pts.person_id, pts.face_observation_id, pts.quality_score_snapshot, pts.trust_score, fe.vector_blob
FROM person_trusted_sample AS pts
JOIN face_embedding AS fe ON fe.face_observation_id = pts.face_observation_id
WHERE pts.active = 1
```

- [x] **Step 4: 运行 bootstrap/prototype 回归**

Run: `source .venv/bin/activate && PYTHONPATH=src python3 -m pytest tests/people_gallery/test_identity_bootstrap_service.py tests/people_gallery/test_prototype_from_trusted_samples.py tests/people_gallery/test_cli_control_plane.py::test_rebuild_artifacts_command -q`
Expected: PASS。

**Task completion action (not a checkbox step): Commit task changes and plan progress**

```bash
git add src/hikbox_pictures/services/identity_bootstrap_service.py src/hikbox_pictures/services/prototype_service.py src/hikbox_pictures/repositories/identity_repo.py src/hikbox_pictures/repositories/person_repo.py src/hikbox_pictures/cli.py tests/people_gallery/test_identity_bootstrap_service.py tests/people_gallery/test_prototype_from_trusted_samples.py tests/people_gallery/fixtures_workspace.py docs/superpowers/plans/2026-04-16-hikbox-pictures-v3-identity-rebuild-phase1.md
git commit -m "feat: implement bootstrap materialize closure and trusted prototype pipeline (Task 4)"
```
### Task 5: 一次性重建脚本主链硬化（真实阶段顺序 + 幂等 + 无 mock 验收）

**Depends on:** Task 4

**Scope Budget:**
- Max files: 20
- Estimated files touched: 6
- Max added lines: 1000
- Estimated added lines: 760

**Files:**
- Create: `src/hikbox_pictures/services/identity_rebuild_service.py`
- Create: `scripts/rebuild_identities_v3.py`
- Create: `tests/people_gallery/test_rebuild_identities_v3_script.py`
- Create: `tests/people_gallery/test_identity_rebuild_v3_real_pipeline.py`
- Modify: `tests/people_gallery/fixtures_workspace.py`
- Modify: `src/hikbox_pictures/services/identity_threshold_profile_service.py`

- [x] **Step 1: 写失败测试，锁定阶段顺序、幂等、profile 输出与真实链路**

```python
# tests/people_gallery/test_rebuild_identities_v3_script.py
from scripts.rebuild_identities_v3 import main


def test_rebuild_script_dry_run_reports_full_clear_scope_and_creates_backup(identity_seed_workspace):
    ws = identity_seed_workspace.root

    rc = main(["--workspace", str(ws), "--dry-run", "--backup-db"])
    assert rc == 0
    summary = identity_seed_workspace.load_last_rebuild_summary()
    clear_targets = summary["dry_run"]["clear_targets"]
    for key in (
        "auto_cluster_batch",
        "auto_cluster",
        "auto_cluster_member",
        "person",
        "person_face_assignment",
        "person_face_exclusion",
        "person_prototype",
        "review_item",
        "export_template",
        "export_template_person",
        "export_run",
        "export_delivery",
    ):
        assert key in clear_targets
    assert identity_seed_workspace.latest_backup_db() is not None


def test_rebuild_script_is_idempotent_and_clears_identity_export_layers(identity_seed_workspace):
    ws = identity_seed_workspace.root

    rc_first = main(["--workspace", str(ws), "--backup-db"])
    assert rc_first == 0
    first_count = identity_seed_workspace.count_person_rows()
    assert identity_seed_workspace.count_table("person_face_exclusion") == 0
    assert identity_seed_workspace.count_table("export_template") == 0
    assert identity_seed_workspace.count_table("export_run") == 0
    assert identity_seed_workspace.count_table("export_delivery") == 0

    rc_second = main(["--workspace", str(ws), "--backup-db"])
    assert rc_second == 0
    second_count = identity_seed_workspace.count_person_rows()
    assert first_count == second_count


def test_rebuild_report_contains_profile_and_cluster_summary(identity_seed_workspace):
    ws = identity_seed_workspace.root
    rc = main(["--workspace", str(ws), "--backup-db"])
    assert rc == 0

    summary = identity_seed_workspace.load_last_rebuild_summary()
    assert summary["active_threshold_profile_id"] is not None
    assert "materialized_cluster_count" in summary
    assert "review_pending_cluster_count" in summary
    assert "discarded_cluster_count" in summary


def test_rebuild_with_threshold_profile_keeps_imported_quantiles(identity_seed_workspace, tmp_path):
    ws = identity_seed_workspace.root
    candidate = identity_seed_workspace.build_profile_candidate()
    candidate["area_log_p10"] = -3.001
    candidate["area_log_p90"] = -1.111
    candidate["sharpness_log_p10"] = 0.123
    candidate["sharpness_log_p90"] = 1.234
    candidate_path = tmp_path / "candidate-thresholds.json"
    identity_seed_workspace.write_json(candidate_path, candidate)

    rc = main(["--workspace", str(ws), "--backup-db", "--threshold-profile", str(candidate_path)])
    assert rc == 0

    active = identity_seed_workspace.get_active_profile()
    assert float(active["area_log_p10"]) == -3.001
    assert float(active["area_log_p90"]) == -1.111
    assert float(active["sharpness_log_p10"]) == 0.123
    assert float(active["sharpness_log_p90"]) == 1.234
```

```python
# tests/people_gallery/test_identity_rebuild_v3_real_pipeline.py
from scripts.rebuild_identities_v3 import main


def test_rebuild_v3_runs_on_real_images_without_mock_engine(identity_real_workspace):
    ws = identity_real_workspace.root
    rc = main(["--workspace", str(ws), "--backup-db"])
    assert rc == 0

    assert identity_real_workspace.count_table("person") > 0
    assert identity_real_workspace.count_table("person_trusted_sample") > 0
    assert identity_real_workspace.count_table("person_prototype") > 0

    rows = identity_real_workspace.list_observation_scores(limit=128)
    sharpness_values = {round(float(row["sharpness_score"]), 6) for row in rows if row["sharpness_score"] is not None}
    quality_values = {round(float(row["quality_score"]), 6) for row in rows if row["quality_score"] is not None}
    assert len(sharpness_values) >= 8
    assert len(quality_values) >= 8

    diag = identity_real_workspace.any_cluster_diagnostic()
    assert "cluster_size" in diag
    assert "distinct_photo_count" in diag
    assert "threshold_profile_id" in diag
```

- [x] **Step 2: 运行测试并确认失败**

Run: `source .venv/bin/activate && PYTHONPATH=src python3 -m pytest tests/people_gallery/test_rebuild_identities_v3_script.py tests/people_gallery/test_identity_rebuild_v3_real_pipeline.py -v`
Expected: FAIL（脚本与 orchestrator 尚不存在）。

- [x] **Step 3: 实现重建编排服务，严格执行 phase1 顺序**

```python
# src/hikbox_pictures/services/identity_rebuild_service.py
class IdentityRebuildService:
    def run(self, *, dry_run: bool, backup_db: bool, skip_ann_rebuild: bool, threshold_profile_path: Path | None) -> RebuildReport:
        if backup_db:
            self.backup_database()

        summary = self.identity_repo.count_rebuild_targets()
        if dry_run:
            return RebuildReport.from_dry_run(summary)

        profile_id, profile_mode = self.profile_service.resolve_profile_for_rebuild(threshold_profile_path)
        self.identity_repo.clear_identity_and_export_layers()
        self.identity_repo.clear_ann_artifacts()

        quality_report = self.quality_backfill_service.backfill_all_observations(
            profile_id=profile_id,
            update_profile_quantiles=(profile_mode == "derived"),
        )
        bootstrap_report = self.bootstrap_service.run_bootstrap(profile_id=profile_id)

        if not skip_ann_rebuild:
            model_key = self.profile_service.get_profile_model_key(profile_id)
            self.prototype_service.rebuild_all_person_prototypes(model_key=model_key)
            self.prototype_service.rebuild_ann_index_from_active_prototypes(model_key=model_key)

        snapshot = self.identity_repo.build_phase1_summary_snapshot(
            profile_id=profile_id,
            profile_mode=profile_mode,
            clear_targets=summary,
            quality_report=quality_report,
            bootstrap_report=bootstrap_report,
        )
        self.identity_repo.persist_rebuild_summary(snapshot)
        return RebuildReport.from_execution(snapshot)
```

```python
# scripts/rebuild_identities_v3.py
if args.threshold_profile is not None:
    candidate_dict = json.loads(args.threshold_profile.read_text(encoding="utf-8"))
    profile_service.validate_candidate_keys(candidate_dict)
```

- [x] **Step 4: 运行脚本回归（包含真实图片链路）**

Run: `source .venv/bin/activate && PYTHONPATH=src python3 -m pytest tests/people_gallery/test_rebuild_identities_v3_script.py tests/people_gallery/test_identity_rebuild_v3_real_pipeline.py tests/people_gallery/test_identity_bootstrap_service.py tests/people_gallery/test_identity_threshold_profile_contract.py -q`
Expected: PASS。

**Task completion action (not a checkbox step): Commit task changes and plan progress**

```bash
git add src/hikbox_pictures/services/identity_rebuild_service.py src/hikbox_pictures/services/identity_threshold_profile_service.py scripts/rebuild_identities_v3.py tests/people_gallery/test_rebuild_identities_v3_script.py tests/people_gallery/test_identity_rebuild_v3_real_pipeline.py tests/people_gallery/fixtures_workspace.py docs/superpowers/plans/2026-04-16-hikbox-pictures-v3-identity-rebuild-phase1.md
git commit -m "feat: add strict phase1 rebuild orchestrator and real-pipeline acceptance (Task 5)"
```

### Task 6: 非破坏阈值评估脚本硬化 + round-trip 真验证

**Depends on:** Task 5

**Scope Budget:**
- Max files: 20
- Estimated files touched: 4
- Max added lines: 1000
- Estimated added lines: 520

**Files:**
- Create: `src/hikbox_pictures/services/identity_threshold_evaluation_service.py`
- Create: `scripts/evaluate_identity_thresholds.py`
- Create: `tests/people_gallery/test_identity_threshold_evaluation_script.py`
- Modify: `tests/people_gallery/fixtures_workspace.py`

- [x] **Step 1: 写失败测试，锁定评估输出维度与 round-trip 验证**

```python
# tests/people_gallery/test_identity_threshold_evaluation_script.py
from pathlib import Path

from scripts.evaluate_identity_thresholds import main as eval_main
from scripts.rebuild_identities_v3 import main as rebuild_main


def test_evaluate_outputs_full_reports_and_does_not_mutate_db(identity_seed_workspace, tmp_path):
    ws = identity_seed_workspace.root
    output_dir = tmp_path / "identity-threshold-tuning" / "run-a"

    checksum_before = identity_seed_workspace.db_checksum()
    rc = eval_main(["--workspace", str(ws), "--output-dir", str(output_dir)])
    assert rc == 0
    checksum_after = identity_seed_workspace.db_checksum()
    assert checksum_before == checksum_after

    summary = identity_seed_workspace.read_json(output_dir / "summary.json")
    assert "bootstrap_estimated_person_count" in summary
    assert "estimated_new_person_review_count" in summary
    assert "estimated_low_confidence_assignment_count" in summary
    assert "cluster_size_distribution" in summary
    assert "distinct_photo_distribution" in summary
    assert "quality_distribution" in summary
    assert "trusted_reject_reason_distribution" in summary
    assert "diff_vs_active_profile" in summary

    candidate = identity_seed_workspace.read_json(output_dir / "candidate-thresholds.json")
    required_keys = set(identity_seed_workspace.identity_profile_roundtrip_columns())
    assert set(candidate.keys()) == required_keys


def test_candidate_profile_can_be_consumed_by_rebuild_script(identity_seed_workspace, tmp_path):
    ws = identity_seed_workspace.root
    output_dir = tmp_path / "identity-threshold-tuning" / "run-b"
    rc_eval = eval_main(["--workspace", str(ws), "--output-dir", str(output_dir)])
    assert rc_eval == 0

    candidate_path = output_dir / "candidate-thresholds.json"
    ws_copy = identity_seed_workspace.copy_workspace(tmp_path / "workspace-copy")
    rc_rebuild = rebuild_main(["--workspace", str(ws_copy), "--backup-db", "--threshold-profile", str(candidate_path)])
    assert rc_rebuild == 0
```

- [x] **Step 2: 运行测试并确认失败**

Run: `source .venv/bin/activate && PYTHONPATH=src python3 -m pytest tests/people_gallery/test_identity_threshold_evaluation_script.py -v`
Expected: FAIL（评估脚本/服务尚不存在）。

- [x] **Step 3: 实现评估脚本，复用同一算法但零业务写入**

```python
# src/hikbox_pictures/services/identity_threshold_evaluation_service.py
class IdentityThresholdEvaluationService:
    def evaluate(self) -> dict[str, object]:
        active_profile = self.profile_service.get_active_profile()
        candidate_profile = self.profile_service.build_candidate_from_active_with_suggestions(active_profile)

        planned = self.bootstrap_service.plan_only(profile=candidate_profile, model_key=self.resolve_model_key())

        return {
            "current_profile": active_profile,
            "candidate_profile": candidate_profile,
            "bootstrap_estimated_person_count": planned.bootstrap_estimated_person_count,
            "estimated_new_person_review_count": planned.estimated_new_person_review_count,
            "estimated_low_confidence_assignment_count": planned.estimated_low_confidence_assignment_count,
            "cluster_size_distribution": planned.cluster_size_distribution,
            "distinct_photo_distribution": planned.distinct_photo_distribution,
            "quality_distribution": planned.quality_distribution,
            "trusted_reject_reason_distribution": planned.trusted_reject_reason_distribution,
            "diff_vs_active_profile": self._diff_profile(active_profile, candidate_profile),
        }
```

```python
# scripts/evaluate_identity_thresholds.py
report = service.evaluate()
output_dir.mkdir(parents=True, exist_ok=True)
(output_dir / "summary.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
(output_dir / "candidate-thresholds.json").write_text(json.dumps(report["candidate_profile"], ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
```

- [x] **Step 4: 运行评估脚本回归 + round-trip 联调**

Run: `source .venv/bin/activate && PYTHONPATH=src python3 -m pytest tests/people_gallery/test_identity_threshold_evaluation_script.py tests/people_gallery/test_rebuild_identities_v3_script.py -q`
Expected: PASS。

**Task completion action (not a checkbox step): Commit task changes and plan progress**

```bash
git add src/hikbox_pictures/services/identity_threshold_evaluation_service.py scripts/evaluate_identity_thresholds.py tests/people_gallery/test_identity_threshold_evaluation_script.py tests/people_gallery/fixtures_workspace.py docs/superpowers/plans/2026-04-16-hikbox-pictures-v3-identity-rebuild-phase1.md
git commit -m "feat: add non-destructive threshold evaluation with true rebuild roundtrip (Task 6)"
```
### Task 7: phase1 只读调参验收页（字段级验收标准）

**Depends on:** Task 4

**Scope Budget:**
- Max files: 20
- Estimated files touched: 7
- Max added lines: 1000
- Estimated added lines: 420

**Files:**
- Modify: `src/hikbox_pictures/api/routes_web.py`
- Modify: `src/hikbox_pictures/services/web_query_service.py`
- Create: `src/hikbox_pictures/web/templates/identity_tuning.html`
- Modify: `src/hikbox_pictures/web/templates/base.html`
- Modify: `src/hikbox_pictures/web/static/style.css`
- Create: `tests/people_gallery/test_web_identity_tuning_page.py`
- Modify: `tests/people_gallery/test_web_navigation.py`

- [ ] **Step 1: 写失败测试，锁定页面必须展示的信息块**

```python
# tests/people_gallery/test_web_identity_tuning_page.py
import json
import re

from fastapi.testclient import TestClient

from hikbox_pictures.api.app import create_app


def test_identity_tuning_page_shows_profile_batch_person_and_cluster_details(identity_seed_workspace):
    client = TestClient(create_app(workspace=identity_seed_workspace.root))
    response = client.get("/identity-tuning")

    assert response.status_code == 200
    html = response.text
    assert "阈值调参与 Bootstrap 验收" in html
    assert "active profile" in html
    assert "bootstrap batch" in html
    assert "匿名人物" in html
    assert "cover observation" in html
    assert "seed 组成" in html
    assert "pending cluster" in html
    assert "distinct_photo_count" in html
    assert "quality_distribution" in html
    assert "external_margin" in html
    assert "reject_reason" in html

    match = re.search(
        r'<script id="identity-tuning-data" type="application/json">(.*?)</script>',
        html,
        re.S,
    )
    assert match is not None
    payload = json.loads(match.group(1))
    expected = identity_seed_workspace.identity_tuning_expected_snapshot()
    assert payload["profile"]["id"] == expected["profile_id"]
    assert payload["summary"]["materialized_cluster_count"] == expected["materialized_cluster_count"]
    assert payload["summary"]["review_pending_cluster_count"] == expected["review_pending_cluster_count"]


def test_identity_tuning_page_is_read_only(identity_seed_workspace):
    client = TestClient(create_app(workspace=identity_seed_workspace.root))
    html = client.get("/identity-tuning").text
    assert "method=\"post\"" not in html.lower()
    assert "resolve-review" not in html
```

- [ ] **Step 2: 运行测试并确认失败**

Run: `source .venv/bin/activate && PYTHONPATH=src python3 -m pytest tests/people_gallery/test_web_identity_tuning_page.py tests/people_gallery/test_web_navigation.py::test_web_navigation_routes_and_static_assets -v`
Expected: FAIL（路由、查询和模板尚不存在）。

- [ ] **Step 3: 实现只读页面查询、路由与模板**

```python
# src/hikbox_pictures/api/routes_web.py
@router.get("/identity-tuning", response_class=HTMLResponse)
def identity_tuning_page(request: Request) -> HTMLResponse:
    conn = connect_db(Path(request.app.state.db_path))
    try:
        service = WebQueryService(conn)
        data = service.get_identity_tuning_page()
        return _get_templates(request).TemplateResponse(
            request=request,
            name="identity_tuning.html",
            context={
                "page_title": "身份重建调参",
                "page_key": "identity_tuning",
                "profile": data["profile"],
                "batch": data["batch"],
                "materialized_people": data["materialized_people"],
                "pending_clusters": data["pending_clusters"],
                "summary": data["summary"],
                "identity_tuning_payload": data,
            },
        )
    finally:
        conn.close()
```

```html
<!-- src/hikbox_pictures/web/templates/identity_tuning.html -->
{% extends "base.html" %}
{% block content %}
<section class="identity-tuning-page">
  <h2>阈值调参与 Bootstrap 验收</h2>
  <script id="identity-tuning-data" type="application/json">{{ identity_tuning_payload | tojson }}</script>
  <article class="panel">
    <h3>cluster 摘要</h3>
    <pre>{{ summary | tojson(indent=2) }}</pre>
  </article>
  <article class="panel">
    <h3>active profile</h3>
    <pre>{{ profile | tojson(indent=2) }}</pre>
  </article>
  <article class="panel">
    <h3>bootstrap batch</h3>
    <pre>{{ batch | tojson(indent=2) }}</pre>
  </article>
  <article class="panel">
    <h3>匿名人物（含 cover 与 seed 组成）</h3>
    <pre>{{ materialized_people | tojson(indent=2) }}</pre>
  </article>
  <article class="panel">
    <h3>pending cluster 诊断</h3>
    <pre>{{ pending_clusters | tojson(indent=2) }}</pre>
  </article>
</section>
{% endblock %}
```

- [ ] **Step 4: 运行 Web 验收回归**

Run: `source .venv/bin/activate && PYTHONPATH=src python3 -m pytest tests/people_gallery/test_web_identity_tuning_page.py tests/people_gallery/test_web_navigation.py -q`
Expected: PASS。

**Task completion action (not a checkbox step): Commit task changes and plan progress**

```bash
git add src/hikbox_pictures/api/routes_web.py src/hikbox_pictures/services/web_query_service.py src/hikbox_pictures/web/templates/identity_tuning.html src/hikbox_pictures/web/templates/base.html src/hikbox_pictures/web/static/style.css tests/people_gallery/test_web_identity_tuning_page.py tests/people_gallery/test_web_navigation.py docs/superpowers/plans/2026-04-16-hikbox-pictures-v3-identity-rebuild-phase1.md
git commit -m "feat: add field-complete read-only identity tuning page for phase1 (Task 7)"
```

### Task 8: README phase1 收口 + 最终回归矩阵

**Depends on:** Task 6, Task 7

**Scope Budget:**
- Max files: 20
- Estimated files touched: 3
- Max added lines: 1000
- Estimated added lines: 240

**Files:**
- Modify: `README.md`
- Modify: `tests/test_repo_samples.py`
- Modify: `docs/superpowers/plans/2026-04-16-hikbox-pictures-v3-identity-rebuild-phase1.md`

- [ ] **Step 1: 写失败测试，锁定 README 的副本试跑与 round-trip 命令**

```python
# tests/test_repo_samples.py
for snippet in (
    "python scripts/rebuild_identities_v3.py --workspace <workspace> --dry-run",
    "python scripts/rebuild_identities_v3.py --workspace <workspace> --backup-db",
    "python scripts/evaluate_identity_thresholds.py --workspace <workspace> --output-dir .tmp/identity-threshold-tuning/<timestamp>/",
    "python scripts/rebuild_identities_v3.py --workspace <workspace-copy> --backup-db --threshold-profile <json>",
    "/identity-tuning",
):
    assert snippet in readme
```

- [ ] **Step 2: 运行测试并确认失败**

Run: `source .venv/bin/activate && PYTHONPATH=src python3 -m pytest tests/test_repo_samples.py::test_readme_mentions_deepface_runtime_basics -v`
Expected: FAIL（README 尚未包含 phase1 新流程）。

- [ ] **Step 3: 更新 README 的 phase1 最小闭环章节**

````markdown
## v3 第一阶段：身份层重建与调参验收（phase1）

```bash
source .venv/bin/activate
python scripts/rebuild_identities_v3.py --workspace <workspace> --dry-run
python scripts/rebuild_identities_v3.py --workspace <workspace> --backup-db
python scripts/evaluate_identity_thresholds.py --workspace <workspace> --output-dir .tmp/identity-threshold-tuning/<timestamp>/
python scripts/rebuild_identities_v3.py --workspace <workspace-copy> --backup-db --threshold-profile <json>
python -m hikbox_pictures.cli serve --workspace <workspace> --host 0.0.0.0 --port 8000
```

- 调参验收入口：`/identity-tuning`（只读）。
- phase1 明确允许 scan/review/actions/export 旧功能暂时失效；不在本阶段做封禁或兼容兜底。
- 主链验收必须包含真实图片路径，不允许只跑 seed/mock 夹具。
````

- [ ] **Step 4: 跑最终最小回归矩阵**

Run: `source .venv/bin/activate && PYTHONPATH=src python3 -m pytest tests/people_gallery/test_identity_v3_schema_migration.py tests/people_gallery/test_identity_threshold_profile_contract.py tests/people_gallery/test_observation_quality_backfill_service.py tests/people_gallery/test_identity_bootstrap_service.py tests/people_gallery/test_prototype_from_trusted_samples.py tests/people_gallery/test_rebuild_identities_v3_script.py tests/people_gallery/test_identity_threshold_evaluation_script.py tests/people_gallery/test_identity_rebuild_v3_real_pipeline.py tests/people_gallery/test_web_identity_tuning_page.py tests/test_repo_samples.py -q`
Expected: PASS。

**Task completion action (not a checkbox step): Commit task changes and plan progress**

```bash
git add README.md tests/test_repo_samples.py docs/superpowers/plans/2026-04-16-hikbox-pictures-v3-identity-rebuild-phase1.md
git commit -m "docs: finalize phase1 rebuild acceptance guide and regression matrix (Task 8)"
```

---

## Spec 覆盖映射（关键硬约束）

- `sharpness_score/quality_score` 真实回填先于 bootstrap，且同步产出并可选回写 `area_log_*` / `sharpness_log_*` 分位点：`Task 3`。
- bootstrap 边约束（互为近邻、精确距离、margin、照片冲突）与 seed 去重 gate 采用行为级可失败断言，不允许注释式占位实现：`Task 4`。
- materialize 完整闭环（person/origin_cluster/cover/assignment/trusted + ANN 失败补偿无半状态）：`Task 4`。
- `identity_threshold_profile` JSON 与表列 round-trip 一致：`Task 2` + `Task 6`。
- 真实旧库升级路径验证（含 `auto_cluster*` 新契约与 exclusion FK 重建）：`Task 1`。
- `--threshold-profile` 导入后的 profile 不被回填阶段覆盖：`Task 3` + `Task 5`。
- 重建脚本 dry-run/backup/清空范围可审计，且不留 mock 验收漏洞：`Task 5`。
- 调参页字段完整度与 DB 快照一致性（非静态模板断言）：`Task 7`。
- README 副本试跑与 `--threshold-profile` 正式入口：`Task 8`。

## 依赖校验结果

- 每个任务都包含 `Depends on`，且至少一个任务（Task 1）可直接启动。
- 无循环依赖。
- `Task 2 -> Task 3` 显式顺序化，避免 `fixtures_workspace.py` 写冲突与 profile 先决条件冲突。
- `Task 2` 不再提前改 `scripts/rebuild_identities_v3.py`；脚本接入统一在 `Task 5` 落地，依赖链自洽。
- 并行任务只在写入集合互斥且依赖满足时标记并行（当前保留 `Task 5` 与 `Task 7` 并行波次）。

## 预算校验结果

- 所有任务估算文件数均 `<= 20`。
- 所有任务估算新增行数均 `<= 1000`。
- 超出 phase1 范围的工作未被隐藏在“后续处理”描述里。
