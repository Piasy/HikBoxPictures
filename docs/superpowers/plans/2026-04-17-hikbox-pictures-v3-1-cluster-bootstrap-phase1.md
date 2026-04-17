# HikBox Pictures v3.1 Cluster Bootstrap Phase 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax. This plan's checkbox state is the persistent progress source of truth; TodoWrite is session-local tracking. Executors may run dependency-free tasks in parallel (default max concurrency: 4).

**Goal:** 交付 v3.1 Phase 1 bootstrap 闭环：observation 预处理独立成快照、cluster run 可反复 rerun 并保留历史、`select-run`/`activate-run` 显式分离 review 与 live owner、`/identity-tuning` 和 neighbor 导出围绕 `run_id` 展示完整证据链，不再以 `auto_cluster*` / `person.origin_cluster_id` / `identity_threshold_profile` 作为运行时真相。

**Architecture:** 采用“schema 真相重置 + observation/cluster 双 profile + run-staged publish”结构。Observation 层负责质量分、分池、去重和可复用 snapshot；cluster run 层负责 raw/cleaned/final lineage、成员角色与 resolution；人物 live 结果只允许由 `activate-run` 从 staging bundle 原子发布。现有 `rebuild_identities_v3.py` 保留为兼容包装入口，但内部必须委托给新 snapshot/run/activate 服务，旧 `evaluate_identity_thresholds.py` 退出主流程。

**Tech Stack:** Python 3.12、SQLite、NumPy、FastAPI/Jinja2、pytest、Playwright、现有 `AnnIndexStore` / `PreviewArtifactService`

---

## 范围裁剪（只做 Phase 1）

- 本 plan 只实现 spec 中的 Phase 1：`Bootstrap Rerun + Review` 闭环；Phase 2 的日常 scan/review/actions/export 恢复不在本轮范围。
- 旧 `auto_cluster*`、`person.origin_cluster_id`、混装参数的 `identity_threshold_profile` 不再作为新代码运行时真相；允许保留为迁移输入或历史审计，但不得继续被查询层、脚本层、页面层当成主语义。
- 调参主路径改为：`build snapshot -> rerun cluster run -> select-run -> /identity-tuning -> export neighbors -> activate-run`。旧 `evaluate_identity_thresholds.py` 只能保留为弃用提示，不再承载调参能力。
- 现有 `people/reviews/exports` 产品页在 Phase 1 不要求恢复到 v3.1 新真相，只要求不阻塞 bootstrap 闭环的测试与文档收口。
- `activate-run` 仅实现发布校验与失败补偿，不引入 activation journal / 自动 crash recovery；进程级异常后的自动恢复不在本轮范围。
- 历史 run 清理策略留到后续显式清理脚本，不在 Phase 1 阻塞项内。

## 防占位实现硬门（全任务生效）

- 任何 `Expected: PASS` 都必须包含至少一条“DB 真值对账断言”，不能只校验 HTTP 200、脚本退出码或字段存在。
- 禁止以固定返回、伪造 summary、跳过算法主链的方式让测试通过；关键口径必须对账到真实持久化数据（`cluster/member/resolution`）。
- `run_status = succeeded` 的前提必须是该 run 的 `summary`、`cluster`、`member`、`resolution` 均已完整落库；脚本测试必须覆盖这一点。
- `activate-run` 失败分支必须落 `publish_failed` 与失败原因审计，且不得出现“published 假阳性”。
- snapshot 复用必须包含反例测试：`candidate_policy_hash` 变化、`required_knn > max_knn_supported`、dataset hash 变化时必须重建。
- 如示例代码与本节冲突，以本节硬门为准，并在同一 Task 内补足测试与实现说明。

## 文件结构设计

### 数据库与迁移

- Create: `src/hikbox_pictures/db/migrations/0005_identity_cluster_bootstrap_v3_1.sql`
- Create: `tools/build_identity_v3_phase1_fixture.py`
- Create: `tests/data/identity-v3-phase1-small.db`
- Modify: `docs/db_schema/README.md`
- 责任：
- 新增 `identity_observation_profile`、`identity_observation_snapshot`、`identity_observation_pool_entry`
- 新增 `identity_cluster_profile`、`identity_cluster_run`、`identity_cluster`、`identity_cluster_lineage`、`identity_cluster_member`、`identity_cluster_resolution`
- 新增 `person_cluster_origin`
- 为 `person_face_assignment`、`person_trusted_sample` 增加 `source_run_id`、`source_cluster_id`
- 重建 `person` 表移除 `origin_cluster_id`
- 将当前 active `identity_threshold_profile` 回填为一对 observation/cluster profile 作为迁移后的初始基线

### 仓储层

- Create: `src/hikbox_pictures/repositories/identity_observation_repo.py`
- Create: `src/hikbox_pictures/repositories/identity_cluster_run_repo.py`
- Create: `src/hikbox_pictures/repositories/identity_cluster_repo.py`
- Create: `src/hikbox_pictures/repositories/identity_publish_repo.py`
- Modify: `src/hikbox_pictures/repositories/person_repo.py`
- Modify: `src/hikbox_pictures/repositories/__init__.py`
- 责任：
- 把 profile/snapshot/run/cluster/resolution/publish 读写拆成 focused repo，避免继续膨胀旧 `identity_repo.py`
- `person_repo.py` 只处理人物层写入、退场与 `person_cluster_origin` 维护

### 服务层

- Create: `src/hikbox_pictures/services/identity_observation_profile_service.py`
- Create: `src/hikbox_pictures/services/identity_observation_snapshot_service.py`
- Create: `src/hikbox_pictures/services/identity_cluster_profile_service.py`
- Create: `src/hikbox_pictures/services/identity_cluster_algorithm.py`
- Create: `src/hikbox_pictures/services/identity_cluster_run_service.py`
- Create: `src/hikbox_pictures/services/identity_cluster_prepare_service.py`
- Create: `src/hikbox_pictures/services/identity_run_activation_service.py`
- Create: `src/hikbox_pictures/services/identity_bootstrap_orchestrator.py`
- Create: `src/hikbox_pictures/services/identity_review_query_service.py`
- Modify: `src/hikbox_pictures/services/observation_quality_backfill_service.py`
- Modify: `src/hikbox_pictures/services/observation_neighbor_export_service.py`
- Modify: `src/hikbox_pictures/services/prototype_service.py`
- Modify: `src/hikbox_pictures/ann/index_store.py`
- 责任：
- observation 预处理与 quality backfill 复用现有能力，但结果必须落到 snapshot 真相
- cluster 算法、prepare、publish、query 明确拆层
- ANN live 切换通过 staging artifact 原子替换，不允许半成品 owner

### 脚本与兼容入口

- Create: `scripts/build_identity_observation_snapshot.py`
- Create: `scripts/rerun_identity_cluster_run.py`
- Create: `scripts/select_identity_cluster_run.py`
- Create: `scripts/activate_identity_cluster_run.py`
- Modify: `scripts/rebuild_identities_v3.py`
- Modify: `scripts/export_observation_neighbors.py`
- Modify: `scripts/evaluate_identity_thresholds.py`
- Modify: `src/hikbox_pictures/services/identity_threshold_evaluation_service.py`
- 责任：
- 新增四个显式入口分别负责 snapshot、rerun、select、activate
- `rebuild_identities_v3.py` 退化为 one-shot convenience wrapper
- `evaluate_identity_thresholds.py` 只输出弃用提示，避免继续暴露混装 profile 语义

### Web/UI 与调试工具

- Modify: `src/hikbox_pictures/api/routes_web.py`
- Create: `src/hikbox_pictures/services/identity_review_query_service.py`
- Modify: `src/hikbox_pictures/web/templates/identity_tuning.html`
- Modify: `src/hikbox_pictures/web/templates/base.html`
- Modify: `src/hikbox_pictures/web/static/style.css`
- Create: `tools/identity_tuning_playwright_check.py`
- Create: `tools/identity_tuning_playwright_capture.cjs`
- 责任：
- `/identity-tuning` 默认展示 current review target run，并允许显式 `run_id`
- 页面与 Playwright 调试工具只读，不承担写操作
- Playwright 调试脚本支持显式 `--run-id`，避免只验证默认 review target

### 测试与夹具

- Create: `tests/people_gallery/fixtures_identity_v3_1.py`
- Create: `tests/people_gallery/test_identity_v3_1_schema_migration.py`
- Create: `tests/people_gallery/test_identity_observation_profile_contract.py`
- Create: `tests/people_gallery/test_identity_observation_snapshot_service.py`
- Create: `tests/people_gallery/test_identity_cluster_profile_contract.py`
- Create: `tests/people_gallery/test_identity_cluster_run_lifecycle.py`
- Create: `tests/people_gallery/test_identity_cluster_algorithm_contract.py`
- Create: `tests/people_gallery/test_identity_cluster_prepare_service.py`
- Create: `tests/people_gallery/test_identity_run_activation_service.py`
- Create: `tests/people_gallery/test_identity_cluster_bootstrap_scripts.py`
- Create: `tests/people_gallery/test_identity_cluster_neighbor_export_service.py`
- Create: `tests/people_gallery/test_identity_cluster_phase1_e2e.py`
- Modify: `tests/people_gallery/test_web_identity_tuning_page.py`
- Modify: `tests/people_gallery/test_export_observation_neighbors_script.py`
- Modify: `tests/people_gallery/test_rebuild_identities_v3_script.py`
- Modify: `tests/people_gallery/test_identity_threshold_evaluation_script.py`
- Modify: `tests/people_gallery/test_web_navigation.py`
- Modify: `tests/test_repo_samples.py`
- Delete: `tests/people_gallery/test_identity_bootstrap_service.py`
- Delete: `tests/people_gallery/test_identity_threshold_profile_contract.py`
- Delete: `tests/people_gallery/test_identity_rebuild_v3_real_pipeline.py`
- 责任：
- 用新的 v3.1 fixture 覆盖 snapshot/run/publish/UI/export 主链
- 清除仍然锚定 `auto_cluster*` / `identity_threshold_profile` 的旧测试假设

## Parallel Execution Plan

### Wave A（地基）

- 顺序执行：`Task 1`
- 阻塞任务：`Task 2`、`Task 3`、`Task 4`、`Task 5`、`Task 6`、`Task 7`、`Task 8`、`Task 9`
- 原因：v3.1 的新表、人物来源字段和 fixture DB 没有落地前，后续所有服务和脚本都没有可信 schema 前提。

### Wave B（顺序：observation 真相 -> run 元数据）

- 顺序执行：`Task 2` -> `Task 3`
- 顺序理由：
- `Task 2` 先固定 observation profile/snapshot 契约和可复用快照
- `Task 3` 再在 snapshot 外键前提上落 run/profile/select-review-target 元数据
- 两任务都需要创建并扩展 `tests/people_gallery/fixtures_identity_v3_1.py`，写入集合冲突，不能并行
- 阻塞任务：`Task 4`

### Wave C（顺序：算法主链 -> prepare/publish）

- 顺序执行：`Task 4` -> `Task 5`
- 顺序理由：
- `Task 4` 负责 raw/cleaned/final lineage、member role、existence/resolution 预判（仅 `review_pending`/`discarded`，不提前写 `materialized/prepared`）
- `Task 5` 必须基于 `Task 4` 已持久化的 final cluster / resolution 候选结果，才能做 cluster-prepare、run-prepare、`materialized/prepared` 状态落库和 activate-run
- 阻塞任务：`Task 6`、`Task 7`、`Task 8`

### Wave D（并行：脚本入口 + review UI + neighbor 导出）

- 可并行任务：`Task 6`、`Task 7`、`Task 8`
- 并行理由：
- `Task 6` 只修改 orchestration/service 与 `scripts/`
- `Task 7` 只修改 route/query/template/style 与页面测试
- `Task 8` 只修改 neighbor export 服务、脚本与导出测试
- 三者依赖都在 `Task 5` 结束后满足，且写入文件集合互斥
- 阻塞任务：`Task 9`（依赖 `Task 6` + `Task 7` + `Task 8`）

### Wave E（README、兼容清理、Playwright 与最终验收）

- 顺序执行：`Task 9`
- 原因：必须等脚本入口、只读 review 页和 neighbor 导出全部定型后，才能统一修改 README、清理旧测试、补 Playwright 调试脚本并跑端到端验收。

---

### Task 1: v3.1 schema 真相落地与迁移夹具

**Depends on:** None

**Scope Budget:**
- Max files: 20
- Estimated files touched: 6
- Max added lines: 1000
- Estimated added lines: 920

**Files:**
- Create: `src/hikbox_pictures/db/migrations/0005_identity_cluster_bootstrap_v3_1.sql`
- Create: `tools/build_identity_v3_phase1_fixture.py`
- Create: `tests/data/identity-v3-phase1-small.db`
- Create: `tests/people_gallery/test_identity_v3_1_schema_migration.py`
- Modify: `tests/people_gallery/test_workspace_bootstrap.py`
- Modify: `docs/db_schema/README.md`

**Execution Guard（必须遵守）:**
- Task 1 不允许以“仅 schema 切换”状态单独合入主干；至少要和 Task 2 同一 PR 合入。
- 若需要先落 Task 1 checkpoint，必须同时补最小 runtime shim（避免任何运行时路径继续强依赖 `person.origin_cluster_id`）。

- [x] **Step 1: 先写失败测试，锁定 migration 后的新真相表、关键状态机约束、回填策略和人物来源字段**

```python
# tests/people_gallery/test_identity_v3_1_schema_migration.py
import shutil
import sqlite3
from pathlib import Path

from hikbox_pictures.db.migrator import apply_migrations


FIXTURE_DB = Path(__file__).resolve().parents[1] / "data" / "identity-v3-phase1-small.db"


def _table_names(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute(
        """
        SELECT name
        FROM sqlite_master
        WHERE type = 'table'
        """
    ).fetchall()
    return {str(row[0]) for row in rows}


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return {str(row[1]) for row in rows}


def test_migrate_phase1_v3_workspace_to_v3_1_runtime_truth(tmp_path):
    db_path = tmp_path / "identity-v3-phase1-small.db"
    shutil.copy2(FIXTURE_DB, db_path)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    legacy_origin_rows = conn.execute(
        """
        SELECT id, origin_cluster_id
        FROM person
        WHERE origin_cluster_id IS NOT NULL
        """
    ).fetchall()
    legacy_origin_person_ids = {int(row["id"]) for row in legacy_origin_rows}

    apply_migrations(conn)

    expected_tables = {
        "identity_observation_profile",
        "identity_observation_snapshot",
        "identity_observation_pool_entry",
        "identity_cluster_profile",
        "identity_cluster_run",
        "identity_cluster",
        "identity_cluster_lineage",
        "identity_cluster_member",
        "identity_cluster_resolution",
        "person_cluster_origin",
    }
    assert expected_tables.issubset(_table_names(conn))

    assert "origin_cluster_id" not in _table_columns(conn, "person")
    assert {"source_run_id", "source_cluster_id", "active"}.issubset(
        _table_columns(conn, "person_face_assignment")
    )
    assert {"source_run_id", "source_cluster_id", "active"}.issubset(
        _table_columns(conn, "person_trusted_sample")
    )

    observation_profiles = conn.execute(
        "SELECT COUNT(*) AS c FROM identity_observation_profile"
    ).fetchone()
    cluster_profiles = conn.execute(
        "SELECT COUNT(*) AS c FROM identity_cluster_profile"
    ).fetchone()
    assert int(observation_profiles["c"]) >= 1
    assert int(cluster_profiles["c"]) >= 1

    origin_rows = conn.execute(
        """
        SELECT person_id, origin_cluster_id, source_run_id, active
        FROM person_cluster_origin
        """
    ).fetchall()
    assert len(origin_rows) >= len(legacy_origin_rows)
    assert {int(row["person_id"]) for row in origin_rows}.issuperset(legacy_origin_person_ids)

    run_table = conn.execute(
        """
        SELECT sql
        FROM sqlite_master
        WHERE type = 'table'
          AND name = 'identity_cluster_run'
        """
    ).fetchone()
    assert run_table is not None
    run_table_sql = str(run_table["sql"])
    assert "created" in run_table_sql
    assert "running" in run_table_sql
    assert "succeeded" in run_table_sql
    assert "failed" in run_table_sql
    assert "cancelled" in run_table_sql

    run_index_rows = conn.execute(
        """
        SELECT sql
        FROM sqlite_master
        WHERE type = 'index'
          AND tbl_name = 'identity_cluster_run'
        """
    ).fetchall()
    run_index_sql = "\n".join(str(row["sql"] or "") for row in run_index_rows)
    assert "is_review_target = 1" in run_index_sql
    assert "is_materialization_owner = 1" in run_index_sql

    snapshot_columns = _table_columns(conn, "identity_observation_snapshot")
    assert {
        "observation_profile_id",
        "dataset_hash",
        "candidate_policy_hash",
        "max_knn_supported",
        "algorithm_version",
    }.issubset(snapshot_columns)

    run_columns = _table_columns(conn, "identity_cluster_run")
    assert {
        "observation_snapshot_id",
        "cluster_profile_id",
        "algorithm_version",
        "run_status",
        "is_review_target",
        "review_selected_at",
        "is_materialization_owner",
        "supersedes_run_id",
        "started_at",
        "finished_at",
        "activated_at",
        "prepared_artifact_root",
        "prepared_ann_manifest_json",
        "summary_json",
        "failure_json",
    }.issubset(run_columns)

    resolution_columns = _table_columns(conn, "identity_cluster_resolution")
    assert {
        "cluster_id",
        "resolution_state",
        "resolution_reason",
        "publish_state",
        "publish_failure_reason",
        "person_id",
        "source_run_id",
        "trusted_seed_count",
        "trusted_seed_candidate_count",
        "trusted_seed_reject_distribution_json",
        "prepared_bundle_manifest_json",
        "prototype_status",
        "ann_status",
    }.issubset(resolution_columns)

    member_columns = _table_columns(conn, "identity_cluster_member")
    assert {
        "cluster_id",
        "observation_id",
        "source_pool_kind",
        "quality_score_snapshot",
        "member_role",
        "decision_status",
        "distance_to_medoid",
        "density_radius",
        "support_ratio",
        "attachment_support_ratio",
        "nearest_competing_cluster_distance",
        "separation_gap",
        "decision_reason_code",
        "is_trusted_seed_candidate",
        "is_selected_trusted_seed",
        "seed_rank",
        "is_representative",
    }.issubset(member_columns)

    resolution_sql = conn.execute(
        """
        SELECT sql
        FROM sqlite_master
        WHERE type = 'table'
          AND name = 'identity_cluster_resolution'
        """
    ).fetchone()
    assert resolution_sql is not None
    resolution_sql_text = str(resolution_sql["sql"])
    assert "materialized" in resolution_sql_text
    assert "review_pending" in resolution_sql_text
    assert "discarded" in resolution_sql_text
    assert "unresolved" in resolution_sql_text
    assert "publish_failed" in resolution_sql_text
    assert "not_applicable" in resolution_sql_text

    cluster_sql = conn.execute(
        """
        SELECT sql
        FROM sqlite_master
        WHERE type = 'table'
          AND name = 'identity_cluster'
        """
    ).fetchone()
    assert cluster_sql is not None
    cluster_sql_text = str(cluster_sql["sql"])
    assert "raw" in cluster_sql_text and "cleaned" in cluster_sql_text and "final" in cluster_sql_text

    cluster_columns = _table_columns(conn, "identity_cluster")
    assert {
        "run_id",
        "cluster_stage",
        "cluster_state",
        "member_count",
        "retained_member_count",
        "anchor_core_count",
        "core_count",
        "boundary_count",
        "attachment_count",
        "excluded_count",
        "distinct_photo_count",
        "compactness_p50",
        "compactness_p90",
        "support_ratio_p10",
        "support_ratio_p50",
        "intra_photo_conflict_ratio",
        "nearest_cluster_distance",
        "separation_gap",
        "boundary_ratio",
        "discard_reason_code",
        "representative_observation_id",
        "summary_json",
    }.issubset(cluster_columns)

    lineage_columns = _table_columns(conn, "identity_cluster_lineage")
    assert {
        "parent_cluster_id",
        "child_cluster_id",
        "relation_kind",
        "reason_code",
    }.issubset(lineage_columns)

    pool_entry_columns = _table_columns(conn, "identity_observation_pool_entry")
    assert {
        "snapshot_id",
        "observation_id",
        "pool_kind",
        "quality_score_snapshot",
        "dedup_group_key",
        "representative_observation_id",
        "excluded_reason",
        "diagnostic_json",
    }.issubset(pool_entry_columns)
```

- [x] **Step 2: 运行测试并确认失败**

Run: `source .venv/bin/activate && PYTHONPATH=src python3 -m pytest tests/people_gallery/test_identity_v3_1_schema_migration.py -q`
Expected: FAIL，提示缺少 `0005_identity_cluster_bootstrap_v3_1.sql` 或断言 `origin_cluster_id` 仍存在。

- [x] **Step 3: 实现 0005 migration，创建 v3.1 新表并把当前 active（若缺失则回退 latest）旧 profile 回填为 observation/cluster profile，并补 run 状态/单例索引约束**

```sql
-- src/hikbox_pictures/db/migrations/0005_identity_cluster_bootstrap_v3_1.sql
PRAGMA foreign_keys = ON;
PRAGMA defer_foreign_keys = ON;

CREATE TABLE identity_observation_profile (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    profile_name TEXT NOT NULL,
    profile_version TEXT NOT NULL,
    embedding_feature_type TEXT NOT NULL,
    embedding_model_key TEXT NOT NULL,
    embedding_distance_metric TEXT NOT NULL,
    embedding_schema_version TEXT NOT NULL,
    quality_formula_version TEXT NOT NULL,
    quality_area_weight REAL NOT NULL,
    quality_sharpness_weight REAL NOT NULL,
    quality_pose_weight REAL NOT NULL,
    core_quality_threshold REAL NOT NULL,
    attachment_quality_threshold REAL NOT NULL,
    exact_duplicate_distance_threshold REAL NOT NULL,
    same_photo_keep_best TEXT NOT NULL,
    burst_window_seconds INTEGER NOT NULL,
    burst_duplicate_distance_threshold REAL NOT NULL,
    pool_exclusion_rules_version TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 0 CHECK (active IN (0, 1)),
    activated_at TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE identity_cluster_profile (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    profile_name TEXT NOT NULL,
    profile_version TEXT NOT NULL,
    discovery_knn_k INTEGER NOT NULL,
    density_min_samples INTEGER NOT NULL,
    raw_cluster_min_size INTEGER NOT NULL,
    raw_cluster_min_distinct_photo_count INTEGER NOT NULL,
    intra_photo_conflict_policy_version TEXT NOT NULL,
    anchor_core_min_support_ratio REAL NOT NULL,
    anchor_core_radius_quantile REAL NOT NULL,
    core_min_support_ratio REAL NOT NULL,
    boundary_min_support_ratio REAL NOT NULL,
    boundary_radius_multiplier REAL NOT NULL,
    split_min_component_size INTEGER NOT NULL,
    split_min_medoid_gap REAL NOT NULL,
    existence_min_retained_count INTEGER NOT NULL,
    existence_min_anchor_core_count INTEGER NOT NULL,
    existence_min_distinct_photo_count INTEGER NOT NULL,
    existence_min_support_ratio_p50 REAL NOT NULL,
    existence_max_intra_photo_conflict_ratio REAL NOT NULL,
    attachment_max_distance REAL NOT NULL,
    attachment_candidate_knn_k INTEGER NOT NULL,
    attachment_min_support_ratio REAL NOT NULL,
    attachment_min_separation_gap REAL NOT NULL,
    materialize_min_anchor_core_count INTEGER NOT NULL,
    materialize_min_distinct_photo_count INTEGER NOT NULL,
    materialize_max_compactness_p90 REAL NOT NULL,
    materialize_min_separation_gap REAL NOT NULL,
    materialize_max_boundary_ratio REAL NOT NULL,
    trusted_seed_min_quality REAL NOT NULL,
    trusted_seed_min_count INTEGER NOT NULL,
    trusted_seed_max_count INTEGER NOT NULL,
    trusted_seed_allow_boundary INTEGER NOT NULL CHECK (trusted_seed_allow_boundary IN (0, 1)),
    active INTEGER NOT NULL DEFAULT 0 CHECK (active IN (0, 1)),
    activated_at TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

WITH selected_legacy_profile AS (
    SELECT *
    FROM identity_threshold_profile
    WHERE active = 1
    ORDER BY id DESC
    LIMIT 1
), fallback_legacy_profile AS (
    SELECT *
    FROM identity_threshold_profile
    WHERE NOT EXISTS (SELECT 1 FROM selected_legacy_profile)
    ORDER BY id DESC
    LIMIT 1
), seed_legacy_profile AS (
    SELECT * FROM selected_legacy_profile
    UNION ALL
    SELECT * FROM fallback_legacy_profile
)
INSERT INTO identity_observation_profile(
    profile_name,
    profile_version,
    embedding_feature_type,
    embedding_model_key,
    embedding_distance_metric,
    embedding_schema_version,
    quality_formula_version,
    quality_area_weight,
    quality_sharpness_weight,
    quality_pose_weight,
    core_quality_threshold,
    attachment_quality_threshold,
    exact_duplicate_distance_threshold,
    same_photo_keep_best,
    burst_window_seconds,
    burst_duplicate_distance_threshold,
    pool_exclusion_rules_version,
    active,
    activated_at
)
SELECT
    profile_name,
    profile_version,
    embedding_feature_type,
    embedding_model_key,
    embedding_distance_metric,
    embedding_schema_version,
    quality_formula_version,
    quality_area_weight,
    quality_sharpness_weight,
    quality_pose_weight,
    high_quality_threshold,
    low_quality_threshold,
    0.005,
    'quality_then_observation_id',
    burst_time_window_seconds,
    0.012,
    'pool_exclusion.v1',
    active,
    activated_at
FROM seed_legacy_profile;

INSERT INTO identity_cluster_profile(
    profile_name,
    profile_version,
    discovery_knn_k,
    density_min_samples,
    raw_cluster_min_size,
    raw_cluster_min_distinct_photo_count,
    intra_photo_conflict_policy_version,
    anchor_core_min_support_ratio,
    anchor_core_radius_quantile,
    core_min_support_ratio,
    boundary_min_support_ratio,
    boundary_radius_multiplier,
    split_min_component_size,
    split_min_medoid_gap,
    existence_min_retained_count,
    existence_min_anchor_core_count,
    existence_min_distinct_photo_count,
    existence_min_support_ratio_p50,
    existence_max_intra_photo_conflict_ratio,
    attachment_max_distance,
    attachment_candidate_knn_k,
    attachment_min_support_ratio,
    attachment_min_separation_gap,
    materialize_min_anchor_core_count,
    materialize_min_distinct_photo_count,
    materialize_max_compactness_p90,
    materialize_min_separation_gap,
    materialize_max_boundary_ratio,
    trusted_seed_min_quality,
    trusted_seed_min_count,
    trusted_seed_max_count,
    trusted_seed_allow_boundary,
    active,
    activated_at
)
SELECT
    profile_name || '-cluster',
    profile_version || '.cluster.v3_1',
    24,
    4,
    3,
    2,
    'same_photo_conflict.v1',
    0.55,
    0.80,
    0.45,
    0.30,
    1.15,
    2,
    0.025,
    3,
    1,
    2,
    0.35,
    0.40,
    0.085,
    16,
    0.30,
    0.015,
    1,
    2,
    0.22,
    0.02,
    0.45,
    low_quality_threshold,
    1,
    6,
    0,
    active,
    activated_at
FROM seed_legacy_profile;

-- identity_cluster_run 的 run_status CHECK 必须包含：
-- ('created', 'running', 'succeeded', 'failed', 'cancelled')
CREATE UNIQUE INDEX ux_identity_cluster_run_single_review_target
ON identity_cluster_run(is_review_target)
WHERE is_review_target = 1;

CREATE UNIQUE INDEX ux_identity_cluster_run_single_materialization_owner
ON identity_cluster_run(is_materialization_owner)
WHERE is_materialization_owner = 1;
```

- [x] **Step 4: 重建 `person`/`person_face_assignment`/`person_trusted_sample` 与文档/fixture，移除 `origin_cluster_id`，补 `person_cluster_origin` 与 schema 文档，并回填 legacy run/cluster/person 来源**

```sql
ALTER TABLE person RENAME TO person_old;

CREATE TABLE person (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    display_name TEXT NOT NULL,
    cover_observation_id INTEGER,
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'merged', 'ignored')),
    notes TEXT,
    confirmed INTEGER NOT NULL DEFAULT 0 CHECK (confirmed IN (0, 1)),
    ignored INTEGER NOT NULL DEFAULT 0 CHECK (ignored IN (0, 1)),
    merged_into_person_id INTEGER,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (cover_observation_id) REFERENCES face_observation(id),
    FOREIGN KEY (merged_into_person_id) REFERENCES person(id)
);

INSERT INTO person(
    id,
    display_name,
    cover_observation_id,
    status,
    notes,
    confirmed,
    ignored,
    merged_into_person_id,
    created_at,
    updated_at
)
SELECT
    id,
    display_name,
    cover_observation_id,
    status,
    notes,
    confirmed,
    ignored,
    merged_into_person_id,
    created_at,
    updated_at
FROM person_old;

-- 为人物归属与 trusted seed 记录补 source_run/source_cluster 来源字段。
-- 注：`active` 列来自 v3 既有 schema（0004 已定义），此处继续沿用并保留唯一索引语义。
ALTER TABLE person_face_assignment RENAME TO person_face_assignment_old;
DROP INDEX IF EXISTS uq_person_face_assignment_active_observation;

CREATE TABLE person_face_assignment (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    person_id INTEGER NOT NULL,
    face_observation_id INTEGER NOT NULL,
    assignment_source TEXT NOT NULL CHECK (assignment_source IN ('bootstrap', 'auto', 'manual', 'merge')),
    diagnostic_json TEXT NOT NULL DEFAULT '{}',
    threshold_profile_id INTEGER,
    locked INTEGER NOT NULL DEFAULT 0 CHECK (locked IN (0, 1)),
    confirmed_at TEXT,
    source_run_id INTEGER,
    source_cluster_id INTEGER,
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (person_id) REFERENCES person(id),
    FOREIGN KEY (face_observation_id) REFERENCES face_observation(id),
    FOREIGN KEY (threshold_profile_id) REFERENCES identity_threshold_profile(id),
    FOREIGN KEY (source_run_id) REFERENCES identity_cluster_run(id),
    FOREIGN KEY (source_cluster_id) REFERENCES identity_cluster(id)
);

INSERT INTO person_face_assignment(
    id,
    person_id,
    face_observation_id,
    assignment_source,
    diagnostic_json,
    threshold_profile_id,
    locked,
    confirmed_at,
    source_run_id,
    source_cluster_id,
    active,
    created_at,
    updated_at
)
SELECT
    id,
    person_id,
    face_observation_id,
    assignment_source,
    diagnostic_json,
    threshold_profile_id,
    locked,
    confirmed_at,
    NULL,
    NULL,
    active,
    created_at,
    updated_at
FROM person_face_assignment_old;

CREATE UNIQUE INDEX uq_person_face_assignment_active_observation
ON person_face_assignment(face_observation_id)
WHERE active = 1;

DROP TABLE person_face_assignment_old;

ALTER TABLE person_trusted_sample RENAME TO person_trusted_sample_old;
DROP INDEX IF EXISTS uq_person_trusted_sample_active_observation;

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
    source_run_id INTEGER,
    source_cluster_id INTEGER,
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (person_id) REFERENCES person(id),
    FOREIGN KEY (face_observation_id) REFERENCES face_observation(id),
    FOREIGN KEY (threshold_profile_id) REFERENCES identity_threshold_profile(id),
    FOREIGN KEY (source_review_id) REFERENCES review_item(id),
    FOREIGN KEY (source_auto_cluster_id) REFERENCES auto_cluster(id),
    FOREIGN KEY (source_run_id) REFERENCES identity_cluster_run(id),
    FOREIGN KEY (source_cluster_id) REFERENCES identity_cluster(id)
);

INSERT INTO person_trusted_sample(
    id,
    person_id,
    face_observation_id,
    trust_source,
    trust_score,
    quality_score_snapshot,
    threshold_profile_id,
    source_review_id,
    source_auto_cluster_id,
    source_run_id,
    source_cluster_id,
    active,
    created_at,
    updated_at
)
SELECT
    id,
    person_id,
    face_observation_id,
    trust_source,
    trust_score,
    quality_score_snapshot,
    threshold_profile_id,
    source_review_id,
    source_auto_cluster_id,
    NULL,
    NULL,
    active,
    created_at,
    updated_at
FROM person_trusted_sample_old;

CREATE UNIQUE INDEX uq_person_trusted_sample_active_observation
ON person_trusted_sample(face_observation_id)
WHERE active = 1;

DROP TABLE person_trusted_sample_old;

CREATE TABLE person_cluster_origin (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    person_id INTEGER NOT NULL,
    origin_cluster_id INTEGER NOT NULL,
    source_run_id INTEGER NOT NULL,
    origin_kind TEXT NOT NULL CHECK (origin_kind IN ('bootstrap_materialize', 'review_materialize', 'merge_adopt')),
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (person_id) REFERENCES person(id),
    FOREIGN KEY (origin_cluster_id) REFERENCES identity_cluster(id),
    FOREIGN KEY (source_run_id) REFERENCES identity_cluster_run(id)
);

-- 先建立 migration 专用 legacy snapshot/run（避免旧人物来源丢失且满足 FK）
INSERT INTO identity_observation_snapshot(
    observation_profile_id,
    dataset_hash,
    candidate_policy_hash,
    max_knn_supported,
    algorithm_version,
    summary_json,
    status,
    started_at,
    finished_at
)
SELECT
    p.id,
    'legacy-migration-dataset-hash',
    'legacy-migration-candidate-policy',
    0,
    'identity.observation_snapshot.legacy_migration.v3_to_v3_1',
    '{"legacy_migration": true}',
    'succeeded',
    CURRENT_TIMESTAMP,
    CURRENT_TIMESTAMP
FROM identity_observation_profile AS p
ORDER BY p.active DESC, p.id DESC
LIMIT 1;

INSERT INTO identity_cluster_run(
    observation_snapshot_id,
    cluster_profile_id,
    algorithm_version,
    run_status,
    summary_json,
    failure_json,
    is_review_target,
    is_materialization_owner
)
SELECT
    (
        SELECT id
        FROM identity_observation_snapshot
        WHERE algorithm_version = 'identity.observation_snapshot.legacy_migration.v3_to_v3_1'
        ORDER BY id DESC
        LIMIT 1
    ),
    (
        SELECT id
        FROM identity_cluster_profile
        ORDER BY active DESC, id DESC
        LIMIT 1
    ),
    'identity.cluster.legacy_migration.v3_to_v3_1',
    'succeeded',
    '{"legacy_migration": true}',
    '{}',
    0,
    0;

-- 把 legacy auto_cluster 映射为 final identity_cluster（id 对齐旧 cluster id，便于人物来源回填）
INSERT INTO identity_cluster(
    id,
    run_id,
    cluster_stage,
    cluster_state
)
SELECT
    ac.id,
    (SELECT id FROM identity_cluster_run WHERE algorithm_version = 'identity.cluster.legacy_migration.v3_to_v3_1' ORDER BY id DESC LIMIT 1),
    'final',
    CASE
        WHEN ac.cluster_status = 'discarded' THEN 'discarded'
        ELSE 'active'
    END
FROM auto_cluster AS ac;

INSERT INTO person_cluster_origin(
    person_id,
    origin_cluster_id,
    source_run_id,
    origin_kind,
    active
)
SELECT
    p_old.id,
    p_old.origin_cluster_id,
    (SELECT id FROM identity_cluster_run WHERE algorithm_version = 'identity.cluster.legacy_migration.v3_to_v3_1' ORDER BY id DESC LIMIT 1),
    'bootstrap_materialize',
    CASE
        WHEN p_old.status = 'active' AND p_old.ignored = 0 THEN 1
        ELSE 0
    END
FROM person_old AS p_old
JOIN auto_cluster AS ac
  ON ac.id = p_old.origin_cluster_id
WHERE p_old.origin_cluster_id IS NOT NULL;
```

```md
### `identity_cluster_run`

| 字段 | 类型 | 约束 | 说明 |
| --- | --- | --- | --- |
| `observation_snapshot_id` | `INTEGER` | 非空，外键到 `identity_observation_snapshot.id` | 本轮 run 复用的 observation 快照。 |
| `is_review_target` | `INTEGER` | 非空，`0/1` | 当前默认 review 对象。 |
| `is_materialization_owner` | `INTEGER` | 非空，`0/1` | 当前 live 物化结果所有者。 |
| `started_at` | `TEXT` | 可空 | run 进入 `running` 的时间。 |
| `finished_at` | `TEXT` | 可空 | run 进入 `succeeded/failed/cancelled` 的时间。 |
| `activated_at` | `TEXT` | 可空 | run 成为 live owner 的时间。 |
| `prepared_artifact_root` | `TEXT` | 可空 | run-scoped staging root。 |
| `prepared_ann_manifest_json` | `TEXT` | 非空，默认 `'{}'` | run 级 ANN prepared manifest。 |
```

- [x] **Step 5: 回跑 migration/fixture 测试并确认通过**

Run: `source .venv/bin/activate && PYTHONPATH=src python3 -m pytest tests/people_gallery/test_workspace_bootstrap.py tests/people_gallery/test_identity_v3_1_schema_migration.py -q`
Expected: PASS，`schema_migration` 记录出现 `0005_identity_cluster_bootstrap_v3_1`，`person.origin_cluster_id` 相关断言全部消失，历史 `origin_cluster_id` 已回填到 `person_cluster_origin`，并且 `identity_cluster_run`/`identity_cluster_resolution`/`identity_observation_snapshot` 的关键列与状态机枚举约束齐全（不是“只有表存在”）；`person_face_assignment`/`person_trusted_sample` 的 `source_run_id`、`source_cluster_id` 均可查询。

**Task completion action (not a checkbox step): Commit task changes and plan progress**

```bash
git add src/hikbox_pictures/db/migrations/0005_identity_cluster_bootstrap_v3_1.sql tools/build_identity_v3_phase1_fixture.py tests/data/identity-v3-phase1-small.db tests/people_gallery/test_identity_v3_1_schema_migration.py tests/people_gallery/test_workspace_bootstrap.py docs/db_schema/README.md docs/superpowers/plans/2026-04-17-hikbox-pictures-v3-1-cluster-bootstrap-phase1.md
git commit -m "feat: add v3.1 cluster bootstrap schema foundation (Task 1)"
```

> 该提交只允许作为与 Task 2 同一 PR 的阶段性 checkpoint，不得单独合并。

### Task 2: Observation profile/snapshot 契约与可复用预处理

**Depends on:** Task 1

**Scope Budget:**
- Max files: 20
- Estimated files touched: 8
- Max added lines: 1000
- Estimated added lines: 860

**Files:**
- Create: `src/hikbox_pictures/repositories/identity_observation_repo.py`
- Create: `src/hikbox_pictures/services/identity_observation_profile_service.py`
- Create: `src/hikbox_pictures/services/identity_observation_snapshot_service.py`
- Create: `tests/people_gallery/fixtures_identity_v3_1.py`
- Create: `tests/people_gallery/test_identity_observation_profile_contract.py`
- Create: `tests/people_gallery/test_identity_observation_snapshot_service.py`
- Modify: `src/hikbox_pictures/services/observation_quality_backfill_service.py`
- Modify: `src/hikbox_pictures/repositories/__init__.py`

- [x] **Step 1: 先写失败测试，固定 observation profile、snapshot reuse 与 pool entry 诊断字段**

```python
# tests/people_gallery/test_identity_observation_snapshot_service.py
from pathlib import Path

from .fixtures_identity_v3_1 import build_identity_phase1_workspace


def test_build_snapshot_persists_pool_counts_and_dedup_metadata(tmp_path: Path) -> None:
    ws = build_identity_phase1_workspace(tmp_path / "snapshot-build")
    try:
        ws.seed_observation_mix_case()
        service = ws.new_observation_snapshot_service()

        snapshot = service.build_snapshot(
            observation_profile_id=ws.observation_profile_id,
            candidate_knn_limit=16,
        )

        assert snapshot["reused"] is False
        assert snapshot["pool_counts"] == {
            "core_discovery": 4,
            "attachment": 2,
            "excluded": 3,
        }
        shadow_rows = ws.list_pool_entries(
            snapshot_id=int(snapshot["snapshot_id"]),
            pool_kind="excluded",
            excluded_reason="duplicate_shadow",
        )
        assert shadow_rows
        assert shadow_rows[0]["representative_observation_id"] is not None
        assert shadow_rows[0]["diagnostic_json"]["dedup_group_key"]
    finally:
        ws.close()


def test_snapshot_reuses_when_profile_dataset_and_candidate_policy_match(tmp_path: Path) -> None:
    ws = build_identity_phase1_workspace(tmp_path / "snapshot-reuse")
    try:
        ws.seed_observation_mix_case()
        service = ws.new_observation_snapshot_service()

        first = service.build_snapshot(
            observation_profile_id=ws.observation_profile_id,
            candidate_knn_limit=24,
        )
        second = service.build_snapshot(
            observation_profile_id=ws.observation_profile_id,
            candidate_knn_limit=12,
        )

        assert first["snapshot_id"] == second["snapshot_id"]
        assert second["reused"] is True
    finally:
        ws.close()


def test_snapshot_rebuilds_when_required_knn_exceeds_max_supported(tmp_path: Path) -> None:
    ws = build_identity_phase1_workspace(tmp_path / "snapshot-rebuild-knn")
    try:
        ws.seed_observation_mix_case()
        service = ws.new_observation_snapshot_service()

        first = service.build_snapshot(
            observation_profile_id=ws.observation_profile_id,
            candidate_knn_limit=12,
        )
        second = service.build_snapshot(
            observation_profile_id=ws.observation_profile_id,
            candidate_knn_limit=24,
        )

        assert first["snapshot_id"] != second["snapshot_id"]
        assert second["reused"] is False
    finally:
        ws.close()


def test_snapshot_rebuilds_when_candidate_policy_or_dataset_changes(tmp_path: Path) -> None:
    ws = build_identity_phase1_workspace(tmp_path / "snapshot-rebuild-policy")
    try:
        ws.seed_observation_mix_case()
        service = ws.new_observation_snapshot_service()

        first = service.build_snapshot(
            observation_profile_id=ws.observation_profile_id,
            candidate_knn_limit=24,
        )
        alt_profile_id = ws.create_observation_profile_variant(
            profile_name="phase1-observation-alt",
            core_quality_threshold=0.42,
            burst_window_seconds=20,
        )
        second = service.build_snapshot(
            observation_profile_id=int(alt_profile_id),
            candidate_knn_limit=24,
        )
        assert first["snapshot_id"] != second["snapshot_id"]
        assert second["reused"] is False

        ws.seed_additional_observation_for_dataset_change()
        third = service.build_snapshot(
            observation_profile_id=int(alt_profile_id),
            candidate_knn_limit=24,
        )
        assert third["snapshot_id"] != second["snapshot_id"]
        assert third["reused"] is False
    finally:
        ws.close()
```

`fixtures_identity_v3_1.py` 中的 `seed_observation_mix_case()` 必须显式构造“可计算距离的真实向量布局”，且至少包含：同图去重、burst 去重、exact duplicate 折叠各 1 例；禁止用全零向量或无法区分距离的占位 embedding 跳过去重逻辑。

- [x] **Step 2: 运行测试并确认失败**

Run: `source .venv/bin/activate && PYTHONPATH=src python3 -m pytest tests/people_gallery/test_identity_observation_profile_contract.py tests/people_gallery/test_identity_observation_snapshot_service.py -q`
Expected: FAIL，提示缺少 `fixtures_identity_v3_1.py`、`IdentityObservationSnapshotService` 或相关表/字段。

- [x] **Step 3: 实现 observation repo/service，并把 quality backfill 接到 snapshot builder 上**

```python
# src/hikbox_pictures/services/identity_observation_snapshot_service.py
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ObservationSnapshotBuildResult:
    snapshot_id: int
    reused: bool
    pool_counts: dict[str, int]


class IdentityObservationSnapshotService:
    def __init__(self, conn, *, observation_repo, quality_backfill_service) -> None:
        self.conn = conn
        self.observation_repo = observation_repo
        self.quality_backfill_service = quality_backfill_service

    def build_snapshot(
        self,
        *,
        observation_profile_id: int,
        candidate_knn_limit: int,
    ) -> dict[str, Any]:
        profile = self.observation_repo.get_observation_profile_required(observation_profile_id)
        dataset_hash = self.observation_repo.compute_observation_dataset_hash(
            model_key=str(profile["embedding_model_key"])
        )
        candidate_policy_hash = self.observation_repo.compute_candidate_policy_hash(
            profile_id=observation_profile_id,
            candidate_knn_limit=candidate_knn_limit,
        )
        reusable = self.observation_repo.find_reusable_snapshot(
            observation_profile_id=observation_profile_id,
            dataset_hash=dataset_hash,
            candidate_policy_hash=candidate_policy_hash,
            required_knn_limit=candidate_knn_limit,
        )
        if reusable is not None:
            return {
                "snapshot_id": int(reusable["id"]),
                "reused": True,
                "pool_counts": dict(reusable["pool_counts"]),
            }

        self.quality_backfill_service.backfill_all_observations(
            profile_id=observation_profile_id,
            update_profile_quantiles=False,
        )
        snapshot_id = self.observation_repo.create_snapshot(
            observation_profile_id=observation_profile_id,
            dataset_hash=dataset_hash,
            candidate_policy_hash=candidate_policy_hash,
            max_knn_supported=candidate_knn_limit,
            algorithm_version="identity.observation_snapshot.v1",
        )
        pool_counts = self.observation_repo.populate_snapshot_entries(
            snapshot_id=snapshot_id,
            observation_profile_id=observation_profile_id,
        )
        return {
            "snapshot_id": int(snapshot_id),
            "reused": False,
            "pool_counts": dict(pool_counts),
        }
```

```python
# src/hikbox_pictures/services/identity_observation_profile_service.py
class IdentityObservationProfileService:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.repo = IdentityObservationRepo(conn)

    def get_active_profile_id(self) -> int:
        profile = self.repo.get_active_profile()
        if profile is None:
            raise ValueError("当前缺少 active observation profile")
        return int(profile["id"])
```

- [x] **Step 4: 创建新的 v3.1 fixture，提供 snapshot/run/publish 全链路可复用 workspace builder**

```python
# tests/people_gallery/fixtures_identity_v3_1.py
@dataclass
class IdentityPhase1Workspace:
    root: Path
    conn: sqlite3.Connection
    observation_profile_id: int
    cluster_profile_id: int

    def new_observation_snapshot_service(self) -> IdentityObservationSnapshotService:
        return IdentityObservationSnapshotService(
            self.conn,
            observation_repo=IdentityObservationRepo(self.conn),
            quality_backfill_service=ObservationQualityBackfillService(self.conn),
        )

    def list_pool_entries(self, *, snapshot_id: int, pool_kind: str, excluded_reason: str):
        rows = self.conn.execute(
            """
            SELECT representative_observation_id, diagnostic_json
            FROM identity_observation_pool_entry
            WHERE snapshot_id = ?
              AND pool_kind = ?
              AND excluded_reason = ?
            ORDER BY observation_id ASC
            """,
            (snapshot_id, pool_kind, excluded_reason),
        ).fetchall()
        return [
            {
                "representative_observation_id": row["representative_observation_id"],
                "diagnostic_json": json.loads(row["diagnostic_json"]),
            }
            for row in rows
        ]

    def create_observation_profile_variant(
        self,
        *,
        profile_name: str,
        core_quality_threshold: float,
        burst_window_seconds: int,
    ) -> int:
        row = self.conn.execute(
            """
            INSERT INTO identity_observation_profile(
                profile_name, profile_version, embedding_feature_type, embedding_model_key,
                embedding_distance_metric, embedding_schema_version, quality_formula_version,
                quality_area_weight, quality_sharpness_weight, quality_pose_weight,
                core_quality_threshold, attachment_quality_threshold,
                exact_duplicate_distance_threshold, same_photo_keep_best,
                burst_window_seconds, burst_duplicate_distance_threshold,
                pool_exclusion_rules_version, active
            )
            SELECT ?, profile_version || '.alt', embedding_feature_type, embedding_model_key,
                   embedding_distance_metric, embedding_schema_version, quality_formula_version,
                   quality_area_weight, quality_sharpness_weight, quality_pose_weight,
                   ?, attachment_quality_threshold, exact_duplicate_distance_threshold,
                   same_photo_keep_best, ?, burst_duplicate_distance_threshold,
                   pool_exclusion_rules_version, 0
            FROM identity_observation_profile
            WHERE id = ?
            """,
            (profile_name, core_quality_threshold, burst_window_seconds, self.observation_profile_id),
        )
        self.conn.commit()
        return int(row.lastrowid)

    def seed_additional_observation_for_dataset_change(self) -> None:
        self.conn.execute(
            """
            INSERT INTO face_observation(
                image_id, face_index, bbox_x, bbox_y, bbox_w, bbox_h, pose_yaw, pose_pitch, pose_roll
            )
            VALUES (999001, 0, 0.1, 0.2, 0.3, 0.3, 0.0, 0.0, 0.0)
            """
        )
        self.conn.commit()
```

- [x] **Step 5: 回跑 observation profile/snapshot 测试与质量回填回归**

Run: `source .venv/bin/activate && PYTHONPATH=src python3 -m pytest tests/people_gallery/test_observation_quality_backfill_service.py tests/people_gallery/test_identity_observation_profile_contract.py tests/people_gallery/test_identity_observation_snapshot_service.py -q`
Expected: PASS，除“可复用”正例外，还覆盖 `required_knn > max_knn_supported`、`candidate_policy_hash` 变化、dataset 变化三类“必须重建”反例；`candidate_policy_hash` / `max_knn_supported` 生效，且不回退到旧 `identity_threshold_profile`。

**Task completion action (not a checkbox step): Commit task changes and plan progress**

```bash
git add src/hikbox_pictures/repositories/identity_observation_repo.py src/hikbox_pictures/services/identity_observation_profile_service.py src/hikbox_pictures/services/identity_observation_snapshot_service.py src/hikbox_pictures/services/observation_quality_backfill_service.py src/hikbox_pictures/repositories/__init__.py tests/people_gallery/fixtures_identity_v3_1.py tests/people_gallery/test_identity_observation_profile_contract.py tests/people_gallery/test_identity_observation_snapshot_service.py docs/superpowers/plans/2026-04-17-hikbox-pictures-v3-1-cluster-bootstrap-phase1.md
git commit -m "feat: add observation snapshot pipeline for v3.1 bootstrap (Task 2)"
```

### Task 3: Cluster profile 与 run 生命周期元数据

**Depends on:** Task 2

**Scope Budget:**
- Max files: 20
- Estimated files touched: 7
- Max added lines: 1000
- Estimated added lines: 760

**Files:**
- Create: `src/hikbox_pictures/repositories/identity_cluster_run_repo.py`
- Create: `src/hikbox_pictures/services/identity_cluster_profile_service.py`
- Create: `src/hikbox_pictures/services/identity_cluster_run_service.py`
- Modify: `src/hikbox_pictures/repositories/__init__.py`
- Modify: `tests/people_gallery/fixtures_identity_v3_1.py`
- Create: `tests/people_gallery/test_identity_cluster_profile_contract.py`
- Create: `tests/people_gallery/test_identity_cluster_run_lifecycle.py`

- [x] **Step 1: 先写失败测试，锁定 `succeeded` 才能选 review target、首个成功 run 自动选中与单例约束**

```python
# tests/people_gallery/test_identity_cluster_run_lifecycle.py
from pathlib import Path

import pytest

from .fixtures_identity_v3_1 import build_identity_phase1_workspace


def test_first_succeeded_run_auto_selected_as_review_target(tmp_path: Path) -> None:
    ws = build_identity_phase1_workspace(tmp_path / "run-lifecycle")
    try:
        ws.seed_observation_mix_case()
        snapshot = ws.new_observation_snapshot_service().build_snapshot(
            observation_profile_id=ws.observation_profile_id,
            candidate_knn_limit=24,
        )
        service = ws.new_cluster_run_service()

        run_a = service.create_run(
            observation_snapshot_id=int(snapshot["snapshot_id"]),
            cluster_profile_id=ws.cluster_profile_id,
            algorithm_version="identity.cluster.v3_1",
            supersedes_run_id=None,
        )
        service.mark_run_succeeded(
            run_id=int(run_a["run_id"]),
            summary_json={"cluster_count": 0},
            select_as_review_target=False,
        )

        first = ws.get_cluster_run(int(run_a["run_id"]))
        assert bool(first["is_review_target"]) is True
        assert first["review_selected_at"] == first["finished_at"]
    finally:
        ws.close()


def test_select_review_target_switches_default_run_without_touching_owner(tmp_path: Path) -> None:
    ws = build_identity_phase1_workspace(tmp_path / "run-lifecycle-switch")
    try:
        ws.seed_observation_mix_case()
        snapshot = ws.new_observation_snapshot_service().build_snapshot(
            observation_profile_id=ws.observation_profile_id,
            candidate_knn_limit=24,
        )
        service = ws.new_cluster_run_service()

        run_a = service.create_run(
            observation_snapshot_id=int(snapshot["snapshot_id"]),
            cluster_profile_id=ws.cluster_profile_id,
            algorithm_version="identity.cluster.v3_1",
            supersedes_run_id=None,
        )
        service.mark_run_succeeded(
            run_id=int(run_a["run_id"]),
            summary_json={"cluster_count": 0},
            select_as_review_target=False,
        )

        run_b = service.create_run(
            observation_snapshot_id=int(snapshot["snapshot_id"]),
            cluster_profile_id=ws.cluster_profile_id,
            algorithm_version="identity.cluster.v3_1",
            supersedes_run_id=int(run_a["run_id"]),
        )
        service.mark_run_succeeded(
            run_id=int(run_b["run_id"]),
            summary_json={"cluster_count": 0},
            select_as_review_target=False,
        )
        service.select_review_target(run_id=int(run_b["run_id"]))

        selected = ws.get_cluster_run(int(run_b["run_id"]))
        previous = ws.get_cluster_run(int(run_a["run_id"]))
        assert bool(selected["is_review_target"]) is True
        assert bool(previous["is_review_target"]) is False
        assert bool(selected["is_materialization_owner"]) is False
        assert bool(previous["is_materialization_owner"]) is False
    finally:
        ws.close()


def test_select_review_target_rejects_non_succeeded_run(tmp_path: Path) -> None:
    ws = build_identity_phase1_workspace(tmp_path / "run-lifecycle-guard")
    try:
        ws.seed_observation_mix_case()
        snapshot = ws.new_observation_snapshot_service().build_snapshot(
            observation_profile_id=ws.observation_profile_id,
            candidate_knn_limit=24,
        )
        service = ws.new_cluster_run_service()
        created = service.create_run(
            observation_snapshot_id=int(snapshot["snapshot_id"]),
            cluster_profile_id=ws.cluster_profile_id,
            algorithm_version="identity.cluster.v3_1",
            supersedes_run_id=None,
        )
        with pytest.raises(ValueError, match="succeeded"):
            service.select_review_target(run_id=int(created["run_id"]))
    finally:
        ws.close()


def test_cancelled_run_cannot_be_selected_as_review_target(tmp_path: Path) -> None:
    ws = build_identity_phase1_workspace(tmp_path / "run-lifecycle-cancelled")
    try:
        ws.seed_observation_mix_case()
        snapshot = ws.new_observation_snapshot_service().build_snapshot(
            observation_profile_id=ws.observation_profile_id,
            candidate_knn_limit=24,
        )
        service = ws.new_cluster_run_service()
        created = service.create_run(
            observation_snapshot_id=int(snapshot["snapshot_id"]),
            cluster_profile_id=ws.cluster_profile_id,
            algorithm_version="identity.cluster.v3_1",
            supersedes_run_id=None,
        )
        service.mark_run_cancelled(
            run_id=int(created["run_id"]),
            reason="operator_cancelled_for_rerun",
        )
        cancelled = ws.get_cluster_run(int(created["run_id"]))
        assert cancelled["run_status"] == "cancelled"
        with pytest.raises(ValueError, match="succeeded"):
            service.select_review_target(run_id=int(created["run_id"]))
    finally:
        ws.close()
```

- [x] **Step 2: 运行测试并确认失败**

Run: `source .venv/bin/activate && PYTHONPATH=src python3 -m pytest tests/people_gallery/test_identity_cluster_profile_contract.py tests/people_gallery/test_identity_cluster_run_lifecycle.py -q`
Expected: FAIL，提示缺少 `IdentityClusterProfileService`、`IdentityClusterRunService` 或 run 表相关读写接口。

- [x] **Step 3: 实现 cluster profile service 与 run repo，先把 created/running/succeeded/failed/cancelled/select-review-target 契约写稳**

```python
# src/hikbox_pictures/services/identity_cluster_run_service.py
from datetime import datetime, timezone


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class IdentityClusterRunService:
    def __init__(self, conn, *, cluster_run_repo, cluster_repo=None) -> None:
        self.conn = conn
        self.cluster_run_repo = cluster_run_repo
        self.cluster_repo = cluster_repo

    def create_run(
        self,
        *,
        observation_snapshot_id: int,
        cluster_profile_id: int,
        algorithm_version: str,
        supersedes_run_id: int | None,
    ) -> dict[str, int]:
        run_id = self.cluster_run_repo.insert_run(
            observation_snapshot_id=observation_snapshot_id,
            cluster_profile_id=cluster_profile_id,
            algorithm_version=algorithm_version,
            run_status="created",
            supersedes_run_id=supersedes_run_id,
        )
        return {"run_id": int(run_id)}

    def mark_run_succeeded(
        self,
        *,
        run_id: int,
        summary_json: dict[str, object],
        select_as_review_target: bool,
    ) -> None:
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            self.cluster_run_repo.update_run_status(
                run_id=run_id,
                run_status="succeeded",
                summary_json=summary_json,
                failure_json={},
            )
            run_row = self.cluster_run_repo.get_run_required(run_id)
            has_existing_target = self.cluster_run_repo.exists_review_target()
            should_select = bool(select_as_review_target) or (not has_existing_target)
            if should_select:
                self.cluster_run_repo.clear_review_target()
                self.cluster_run_repo.set_review_target(
                    run_id=run_id,
                    review_selected_at=str(run_row["finished_at"]),
                )
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    def mark_run_cancelled(self, *, run_id: int, reason: str) -> None:
        self.cluster_run_repo.update_run_status(
            run_id=run_id,
            run_status="cancelled",
            summary_json={},
            failure_json={"reason": str(reason)},
        )

    def select_review_target(self, *, run_id: int) -> None:
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            target = self.cluster_run_repo.get_run_required(run_id)
            if str(target["run_status"]) != "succeeded":
                raise ValueError(f"只能选择 succeeded run 作为 review target: {run_id}")
            self.cluster_run_repo.clear_review_target()
            self.cluster_run_repo.set_review_target(
                run_id=run_id,
                review_selected_at=_utc_now_iso(),
            )
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
```

```python
# src/hikbox_pictures/services/identity_cluster_profile_service.py
class IdentityClusterProfileService:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.repo = IdentityClusterRunRepo(conn)

    def get_active_profile_id(self) -> int:
        profile = self.repo.get_active_cluster_profile()
        if profile is None:
            raise ValueError("当前缺少 active cluster profile")
        return int(profile["id"])
```

- [x] **Step 4: 扩展 fixture helper，补 run/profile 查询方法并回跑测试**

```python
# tests/people_gallery/fixtures_identity_v3_1.py
def get_cluster_run(self, run_id: int) -> dict[str, Any]:
    row = self.conn.execute(
        """
        SELECT id, observation_snapshot_id, cluster_profile_id, run_status, is_review_target, is_materialization_owner, supersedes_run_id, review_selected_at, finished_at
        FROM identity_cluster_run
        WHERE id = ?
        """,
        (run_id,),
    ).fetchone()
    assert row is not None
    return dict(row)

def new_cluster_run_service(self) -> IdentityClusterRunService:
    return IdentityClusterRunService(
        self.conn,
        cluster_run_repo=IdentityClusterRunRepo(self.conn),
    )

def count_clusters(self, *, run_id: int) -> int:
    row = self.conn.execute(
        "SELECT COUNT(*) AS c FROM identity_cluster WHERE run_id = ?",
        (run_id,),
    ).fetchone()
    return int(row["c"])


def count_cluster_members(self, *, run_id: int) -> int:
    row = self.conn.execute(
        """
        SELECT COUNT(*) AS c
        FROM identity_cluster_member AS m
        JOIN identity_cluster AS c ON c.id = m.cluster_id
        WHERE c.run_id = ?
        """,
        (run_id,),
    ).fetchone()
    return int(row["c"])


def count_cluster_resolutions(self, *, run_id: int) -> int:
    row = self.conn.execute(
        """
        SELECT COUNT(*) AS c
        FROM identity_cluster_resolution AS r
        JOIN identity_cluster AS c ON c.id = r.cluster_id
        WHERE c.run_id = ?
        """,
        (run_id,),
    ).fetchone()
    return int(row["c"])
```

Run: `source .venv/bin/activate && PYTHONPATH=src python3 -m pytest tests/people_gallery/test_identity_cluster_profile_contract.py tests/people_gallery/test_identity_cluster_run_lifecycle.py -q`
Expected: PASS，仅 `succeeded` 可选为 review target，`cancelled` run 不能被选中；`select_review_target` 在同一事务中 clear+set 并写 `review_selected_at`；`is_review_target` 全库唯一，首个成功 run 自动成为 target 且 `review_selected_at=finished_at`，并保持 `is_materialization_owner` 与 review target 分离。

**Task completion action (not a checkbox step): Commit task changes and plan progress**

```bash
git add src/hikbox_pictures/repositories/identity_cluster_run_repo.py src/hikbox_pictures/services/identity_cluster_profile_service.py src/hikbox_pictures/services/identity_cluster_run_service.py src/hikbox_pictures/repositories/__init__.py tests/people_gallery/fixtures_identity_v3_1.py tests/people_gallery/test_identity_cluster_profile_contract.py tests/people_gallery/test_identity_cluster_run_lifecycle.py docs/superpowers/plans/2026-04-17-hikbox-pictures-v3-1-cluster-bootstrap-phase1.md
git commit -m "feat: add cluster profile and run lifecycle services (Task 3)"
```

### Task 4: 密度聚类算法、lineage 与成员证据落库

**Depends on:** Task 2, Task 3

**Scope Budget:**
- Max files: 20
- Estimated files touched: 6
- Max added lines: 1000
- Estimated added lines: 980
- 风险备注：该任务算法复杂度最高，允许在同一 PR 内超出估算行数，但必须用“已知拓扑断言 + DB 对账”证明不是占位实现。

**Files:**
- Create: `src/hikbox_pictures/repositories/identity_cluster_repo.py`
- Create: `src/hikbox_pictures/services/identity_cluster_algorithm.py`
- Modify: `src/hikbox_pictures/services/identity_cluster_run_service.py`
- Modify: `tests/people_gallery/fixtures_identity_v3_1.py`
- Create: `tests/people_gallery/test_identity_cluster_algorithm_contract.py`
- Create: `tests/people_gallery/test_identity_cluster_run_service.py`

- [x] **Step 1: 先写失败测试，锁定 raw -> cleaned -> final lineage、成员角色、关键指标口径与 existence gate 原因**

```python
# tests/people_gallery/test_identity_cluster_run_service.py
from pathlib import Path

from .fixtures_identity_v3_1 import build_identity_phase1_workspace


def test_execute_run_persists_lineage_member_roles_and_resolution_states(tmp_path: Path) -> None:
    ws = build_identity_phase1_workspace(tmp_path / "cluster-execute")
    try:
        ws.seed_split_and_attachment_case()
        snapshot = ws.new_observation_snapshot_service().build_snapshot(
            observation_profile_id=ws.observation_profile_id,
            candidate_knn_limit=24,
        )
        run = ws.new_cluster_run_service().execute_run(
            observation_snapshot_id=int(snapshot["snapshot_id"]),
            cluster_profile_id=ws.cluster_profile_id,
            supersedes_run_id=None,
            select_as_review_target=True,
        )

        assert run["run_status"] == "succeeded"
        lineage = ws.list_cluster_lineage(run_id=int(run["run_id"]))
        assert any(item["relation_kind"] == "split" for item in lineage)

        final_clusters = ws.list_clusters(run_id=int(run["run_id"]), cluster_stage="final")
        assert final_clusters
        assert any(item["cluster_state"] == "discarded" for item in final_clusters)
        resolutions = ws.list_cluster_resolutions(run_id=int(run["run_id"]))
        assert resolutions
        assert all(item["resolution_state"] in {"unresolved", "review_pending", "discarded"} for item in resolutions)
        assert all(item["resolution_state"] != "materialized" for item in resolutions)

        members = ws.list_cluster_members(run_id=int(run["run_id"]))
        roles = {item["member_role"] for item in members if item["decision_status"] == "retained"}
        assert {"anchor_core", "core", "boundary"}.issubset(roles)
        assert "attachment" in roles
        assert any(item["decision_reason_code"] == "split_into_other_child" for item in members)
        assert any(item["decision_reason_code"] == "outside_boundary_radius" for item in members)
        ws.assert_member_support_ratio_formula(run_id=int(run["run_id"]), sample_size=6)
        ws.assert_intra_photo_conflict_ratio_formula(run_id=int(run["run_id"]))
        ws.assert_existence_gate_reason_consistent(run_id=int(run["run_id"]))
    finally:
        ws.close()


def test_execute_run_persists_gate_metrics_and_discard_reason_alignment(tmp_path: Path) -> None:
    ws = build_identity_phase1_workspace(tmp_path / "cluster-metrics-audit")
    try:
        ws.seed_split_and_attachment_case()
        snapshot = ws.new_observation_snapshot_service().build_snapshot(
            observation_profile_id=ws.observation_profile_id,
            candidate_knn_limit=24,
        )
        run = ws.new_cluster_run_service().execute_run(
            observation_snapshot_id=int(snapshot["snapshot_id"]),
            cluster_profile_id=ws.cluster_profile_id,
            supersedes_run_id=None,
            select_as_review_target=True,
        )
        ws.assert_final_gate_metrics_frozen_before_attachment(run_id=int(run["run_id"]))
        ws.assert_cluster_discard_reason_equals_resolution_reason(run_id=int(run["run_id"]))
    finally:
        ws.close()
```

```python
# tests/people_gallery/test_identity_cluster_algorithm_contract.py
def test_algorithm_respects_mutual_knn_density_and_anchor_quantile_on_known_topology(tmp_path: Path) -> None:
    ws = build_identity_phase1_workspace(tmp_path / "cluster-known-topology")
    try:
        ws.seed_known_topology_case()  # 8-10 个点，固定向量拓扑，mutual-kNN 结构可预期
        snapshot = ws.new_observation_snapshot_service().build_snapshot(
            observation_profile_id=ws.observation_profile_id,
            candidate_knn_limit=24,
        )
        run = ws.new_cluster_run_service().execute_run(
            observation_snapshot_id=int(snapshot["snapshot_id"]),
            cluster_profile_id=ws.cluster_profile_id,
            supersedes_run_id=None,
            select_as_review_target=True,
        )
        ws.assert_known_topology_contract(run_id=int(run["run_id"]))
    finally:
        ws.close()
```

```python
# tests/people_gallery/fixtures_identity_v3_1.py
def assert_known_topology_contract(self, *, run_id: int) -> None:
    # 最小骨架（必须全部落地为精确断言，禁止只断言 cluster 数量）
    # 1) medoid：验证 cluster 内距离和最小 observation_id 与落库 representative/medoid 一致
    # 2) density_radius：验证等于第 density_min_samples 个近邻距离
    # 3) anchor_core_radius：验证为 distance_to_medoid 的 profile.quantile 分位值
    # 4) support_ratio：验证分子/分母口径（排除 self/shadow/conflict）
    # 5) mutual kNN：仅双向 kNN 邻边可参与连通
    ...
```

`fixtures_identity_v3_1.py` 中必须新增并固定下列夹具约束，避免算法被弱化实现“碰巧过测”：
- `seed_split_and_attachment_case()`：至少包含“两组在向量空间明显分离但被桥接点连通”的样本、attachment pool 候选样本、以及至少 1 个应触发 existence gate 失败的 cluster。
- 同图冲突样本至少 1 对，且冲突过滤必须在 split graph 边过滤或 attachment 拒绝中生效（不能只影响 support_ratio）。
- `seed_known_topology_case()`：提供固定小图拓扑与可重复距离结构，供 `mutual-kNN`、`density_radius`、`medoid`、`anchor_core_radius_quantile` 的精确断言使用。

- [x] **Step 2: 运行测试并确认失败**

Run: `source .venv/bin/activate && PYTHONPATH=src python3 -m pytest tests/people_gallery/test_identity_cluster_algorithm_contract.py tests/people_gallery/test_identity_cluster_run_service.py -q`
Expected: FAIL，`execute_run` 不存在，或缺少 `identity_cluster` / `identity_cluster_member` / `identity_cluster_resolution` 写入。

- [x] **Step 3: 实现密度聚类、split、existence gate、attachment 与 resolution 预判（prepare 前不写 `materialized`）**

```python
# src/hikbox_pictures/services/identity_cluster_algorithm.py
@dataclass(frozen=True)
class ClusterMemberEvidence:
    observation_id: int
    source_pool_kind: str
    member_role: str
    decision_status: str
    distance_to_medoid: float
    density_radius: float
    support_ratio: float
    attachment_support_ratio: float | None
    nearest_competing_cluster_distance: float | None
    separation_gap: float | None
    decision_reason_code: str | None


def _support_ratio(
    *,
    observation_id: int,
    candidate_neighbor_ids: list[int],
    cluster_member_ids: set[int],
    shadow_ids: set[int],
    conflicting_ids: set[int],
    knn_k: int,
) -> float:
    effective = [
        candidate_id
        for candidate_id in candidate_neighbor_ids
        if candidate_id != observation_id
        and candidate_id not in shadow_ids
        and candidate_id not in conflicting_ids
    ]
    effective_neighbor_count = min(knn_k, len(effective))
    cluster_neighbor_count = sum(1 for candidate_id in effective[:knn_k] if candidate_id in cluster_member_ids)
    return cluster_neighbor_count / max(1, effective_neighbor_count)


def _existence_reason(metrics: dict[str, float | int], profile: dict[str, Any]) -> str | None:
    if int(metrics["retained_member_count"]) < int(profile["existence_min_retained_count"]):
        return "retained_too_small"
    if int(metrics["anchor_core_count"]) < int(profile["existence_min_anchor_core_count"]):
        return "anchor_core_insufficient"
    if int(metrics["distinct_photo_count"]) < int(profile["existence_min_distinct_photo_count"]):
        return "distinct_photo_insufficient"
    if float(metrics["support_ratio_p50"]) < float(profile["existence_min_support_ratio_p50"]):
        return "support_ratio_too_low"
    if float(metrics["intra_photo_conflict_ratio"]) > float(profile["existence_max_intra_photo_conflict_ratio"]):
        return "intra_photo_conflict_too_high"
    return None
```

- [x] **Step 4: 在 run service 中串起 `create_run -> running -> algorithm -> persist lineage/member/resolution -> succeeded/failed`，并补 `cancelled` 分支入口**

```python
# src/hikbox_pictures/services/identity_cluster_run_service.py
def execute_run(
    self,
    *,
    observation_snapshot_id: int,
    cluster_profile_id: int,
    supersedes_run_id: int | None,
    select_as_review_target: bool,
) -> dict[str, Any]:
    created = self.create_run(
        observation_snapshot_id=observation_snapshot_id,
        cluster_profile_id=cluster_profile_id,
        algorithm_version="identity.cluster_run.v3_1",
        supersedes_run_id=supersedes_run_id,
    )
    run_id = int(created["run_id"])
    self.cluster_run_repo.update_run_status(run_id=run_id, run_status="running", summary_json={}, failure_json={})
    try:
        plan = self.algorithm.build_run_plan(
            observation_snapshot_id=observation_snapshot_id,
            cluster_profile_id=cluster_profile_id,
        )
        self.cluster_repo.persist_run_plan(run_id=run_id, run_plan=plan)
        self.mark_run_succeeded(
            run_id=run_id,
            summary_json=plan.run_summary,
            select_as_review_target=select_as_review_target,
        )
        return {"run_id": run_id, "run_status": "succeeded"}
    except Exception as exc:
        self.cluster_run_repo.update_run_status(
            run_id=run_id,
            run_status="failed",
            summary_json={},
            failure_json={"error": str(exc)},
        )
        raise
```

```python
# tests/people_gallery/fixtures_identity_v3_1.py
def assert_member_support_ratio_formula(self, *, run_id: int, sample_size: int) -> None:
    rows = self.conn.execute(
        """
        SELECT m.support_ratio, m.observation_id, m.cluster_id
        FROM identity_cluster_member AS m
        JOIN identity_cluster AS c ON c.id = m.cluster_id
        WHERE c.run_id = ?
          AND c.cluster_stage IN ('raw', 'cleaned', 'final')
          AND m.decision_status = 'retained'
        ORDER BY m.observation_id ASC
        LIMIT ?
        """,
        (run_id, sample_size),
    ).fetchall()
    assert rows
    for row in rows:
        expected = self.recompute_member_support_ratio(
            cluster_id=int(row["cluster_id"]),
            observation_id=int(row["observation_id"]),
        )
        assert abs(float(row["support_ratio"]) - float(expected)) <= 1e-6


def assert_intra_photo_conflict_ratio_formula(self, *, run_id: int) -> None:
    rows = self.conn.execute(
        """
        SELECT c.id, c.intra_photo_conflict_ratio
        FROM identity_cluster AS c
        WHERE c.run_id = ?
          AND c.cluster_stage = 'final'
          AND c.cluster_state = 'active'
        ORDER BY c.id ASC
        """,
        (run_id,),
    ).fetchall()
    assert rows
    saw_conflict_cluster = False
    for row in rows:
        members = self.conn.execute(
            """
            SELECT fo.image_id
            FROM identity_cluster_member AS m
            JOIN face_observation AS fo ON fo.id = m.observation_id
            WHERE m.cluster_id = ?
              AND m.decision_status = 'retained'
            """,
            (int(row["id"]),),
        ).fetchall()
        total = len(members)
        if total < 2:
            expected = 0.0
        else:
            conflict_pairs = 0
            total_pairs = total * (total - 1) // 2
            image_ids = [int(member["image_id"]) for member in members]
            for i in range(total):
                for j in range(i + 1, total):
                    if image_ids[i] == image_ids[j]:
                        conflict_pairs += 1
            expected = float(conflict_pairs) / float(total_pairs)
            if conflict_pairs > 0:
                saw_conflict_cluster = True
        assert abs(float(row["intra_photo_conflict_ratio"]) - expected) <= 1e-6
    assert saw_conflict_cluster


def assert_existence_gate_reason_consistent(self, *, run_id: int) -> None:
    rows = self.conn.execute(
        """
        SELECT c.id, c.cluster_state, c.discard_reason_code, r.resolution_state, r.resolution_reason
        FROM identity_cluster AS c
        JOIN identity_cluster_resolution AS r ON r.cluster_id = c.id
        WHERE c.run_id = ?
          AND c.cluster_stage = 'final'
        """,
        (run_id,),
    ).fetchall()
    for row in rows:
        if row["cluster_state"] == "discarded":
            assert row["resolution_state"] == "discarded"
            assert row["discard_reason_code"] == row["resolution_reason"]


def assert_final_gate_metrics_frozen_before_attachment(self, *, run_id: int) -> None:
    rows = self.conn.execute(
        """
        SELECT id, retained_member_count, anchor_core_count, core_count, boundary_count, attachment_count
        FROM identity_cluster
        WHERE run_id = ?
          AND cluster_stage = 'final'
        """,
        (run_id,),
    ).fetchall()
    for row in rows:
        retained = int(row["retained_member_count"])
        assert retained == int(row["anchor_core_count"]) + int(row["core_count"]) + int(row["boundary_count"])
        assert int(row["attachment_count"]) >= 0


def assert_cluster_discard_reason_equals_resolution_reason(self, *, run_id: int) -> None:
    self.assert_existence_gate_reason_consistent(run_id=run_id)
```

- [x] **Step 5: 回跑算法与 run 持久化测试**

Run: `source .venv/bin/activate && PYTHONPATH=src python3 -m pytest tests/people_gallery/test_identity_cluster_algorithm_contract.py tests/people_gallery/test_identity_cluster_run_service.py tests/people_gallery/test_identity_cluster_run_lifecycle.py -q`
Expected: PASS，能看到 `raw/cleaned/final` 节点、`split` lineage，以及 `unresolved/review_pending/discarded` 的 prepare 前 resolution 预判（不提前写 `materialized/prepared`），且不写 live person；同时对账 `support_ratio` 口径、`intra_photo_conflict_ratio` 公式、`discard_reason_code == resolution_reason`、`final gate metrics` 在 attachment 前冻结，并通过 known-topology 合同约束 `mutual-kNN/density_radius/medoid/anchor_core_radius`，防止用占位 summary 冒充算法落库。

**Task completion action (not a checkbox step): Commit task changes and plan progress**

```bash
git add src/hikbox_pictures/repositories/identity_cluster_repo.py src/hikbox_pictures/services/identity_cluster_algorithm.py src/hikbox_pictures/services/identity_cluster_run_service.py tests/people_gallery/fixtures_identity_v3_1.py tests/people_gallery/test_identity_cluster_algorithm_contract.py tests/people_gallery/test_identity_cluster_run_service.py docs/superpowers/plans/2026-04-17-hikbox-pictures-v3-1-cluster-bootstrap-phase1.md
git commit -m "feat: persist v3.1 cluster lineage and member evidence (Task 4)"
```

### Task 5: cluster-prepare、run-prepare 与 activate-run 发布

**Depends on:** Task 4

**Scope Budget:**
- Max files: 20
- Estimated files touched: 9
- Max added lines: 1000
- Estimated added lines: 930

**Files:**
- Create: `src/hikbox_pictures/repositories/identity_publish_repo.py`
- Create: `src/hikbox_pictures/services/identity_cluster_prepare_service.py`
- Create: `src/hikbox_pictures/services/identity_run_activation_service.py`
- Modify: `src/hikbox_pictures/repositories/person_repo.py`
- Modify: `src/hikbox_pictures/services/prototype_service.py`
- Modify: `src/hikbox_pictures/ann/index_store.py`
- Modify: `tests/people_gallery/fixtures_identity_v3_1.py`
- Create: `tests/people_gallery/test_identity_cluster_prepare_service.py`
- Create: `tests/people_gallery/test_identity_run_activation_service.py`

**原子性边界提示（Phase 1 简化约束）:**
- 本任务不实现 activation journal / 自动 crash recovery。
- `activate-run` 的 DB 与 ANN 切换不是单事务；若进程在两者之间异常，允许出现短暂不一致，执行者必须通过人工重试 `activate-run` 收敛并落审计日志。

- [x] **Step 1: 先写失败测试，锁定 checksum/manifest、`materialized->prepared->published` 状态切换、`publish_failed` 审计与发布失败回滚**

```python
# tests/people_gallery/test_identity_cluster_prepare_service.py
from pathlib import Path

from .fixtures_identity_v3_1 import build_identity_phase1_workspace


def test_prepare_run_writes_verified_manifests_and_only_then_marks_materialized_prepared(tmp_path: Path) -> None:
    ws = build_identity_phase1_workspace(tmp_path / "prepare-run")
    try:
        ws.seed_materialize_candidate_case()
        snapshot = ws.new_observation_snapshot_service().build_snapshot(
            observation_profile_id=ws.observation_profile_id,
            candidate_knn_limit=24,
        )
        run = ws.new_cluster_run_service().execute_run(
            observation_snapshot_id=int(snapshot["snapshot_id"]),
            cluster_profile_id=ws.cluster_profile_id,
            supersedes_run_id=None,
            select_as_review_target=True,
        )
        before_states = ws.list_cluster_resolutions(run_id=int(run["run_id"]))
        assert all(item["resolution_state"] != "materialized" for item in before_states)

        result = ws.new_cluster_prepare_service().prepare_run(run_id=int(run["run_id"]))

        active_people = ws.conn.execute(
            """
            SELECT COUNT(*) AS c
            FROM person
            WHERE status = 'active'
              AND ignored = 0
            """
        ).fetchone()
        prepared_clusters = ws.conn.execute(
            """
            SELECT COUNT(*) AS c
            FROM identity_cluster_resolution
            WHERE source_run_id = ?
              AND resolution_state = 'materialized'
              AND publish_state = 'prepared'
            """,
            (int(run["run_id"]),),
        ).fetchone()
        ann_manifest = ws.get_run_ann_manifest(int(run["run_id"]))
        assert int(result["prepared_cluster_count"]) >= 1
        assert int(active_people["c"]) == 0
        assert int(prepared_clusters["c"]) >= 1
        assert ann_manifest["artifact_checksum"]
        assert ann_manifest["artifact_path"]

        ranked = ws.conn.execute(
            """
            SELECT
                m.observation_id,
                m.member_role,
                m.seed_rank,
                m.support_ratio,
                m.distance_to_medoid,
                m.cluster_id,
                fo.quality_score
            FROM identity_cluster_member AS m
            JOIN identity_cluster AS c ON c.id = m.cluster_id
            JOIN face_observation AS fo ON fo.id = m.observation_id
            WHERE c.run_id = ?
              AND c.cluster_stage = 'final'
              AND m.is_selected_trusted_seed = 1
            ORDER BY m.cluster_id ASC, m.seed_rank ASC
            """,
            (int(run["run_id"]),),
        ).fetchall()
        assert ranked
        role_order = {"anchor_core": 0, "core": 1, "boundary": 2}
        by_cluster: dict[int, list] = {}
        for row in ranked:
            by_cluster.setdefault(int(row["cluster_id"]), []).append(row)
        for rows in by_cluster.values():
            expected = sorted(
                rows,
                key=lambda row: (
                    role_order.get(str(row["member_role"]), 9),
                    -float(row["quality_score"] or 0.0),
                    -float(row["support_ratio"] or 0.0),
                    float(row["distance_to_medoid"] or 0.0),
                    int(row["observation_id"]),
                ),
            )
            assert [int(row["observation_id"]) for row in rows] == [
                int(row["observation_id"]) for row in expected
            ]
    finally:
        ws.close()


def test_prepare_run_rolls_all_candidates_back_to_review_pending_when_run_ann_bundle_failed(tmp_path: Path) -> None:
    ws = build_identity_phase1_workspace(tmp_path / "prepare-run-ann-failed")
    try:
        ws.seed_materialize_candidate_case()
        snapshot = ws.new_observation_snapshot_service().build_snapshot(
            observation_profile_id=ws.observation_profile_id,
            candidate_knn_limit=24,
        )
        run = ws.new_cluster_run_service().execute_run(
            observation_snapshot_id=int(snapshot["snapshot_id"]),
            cluster_profile_id=ws.cluster_profile_id,
            supersedes_run_id=None,
            select_as_review_target=True,
        )
        ws.stub_run_ann_prepare_failure(run_id=int(run["run_id"]))
        result = ws.new_cluster_prepare_service().prepare_run(run_id=int(run["run_id"]))
        states = ws.list_cluster_resolutions(run_id=int(run["run_id"]))
        assert int(result["prepared_cluster_count"]) == 0
        assert all(item["resolution_state"] in {"review_pending", "discarded"} for item in states)
        assert all(item["publish_state"] == "not_applicable" for item in states if item["resolution_state"] != "discarded")
    finally:
        ws.close()


def test_prepare_run_does_not_materialize_cluster_below_gate_thresholds(tmp_path: Path) -> None:
    ws = build_identity_phase1_workspace(tmp_path / "prepare-run-gate-negative")
    try:
        ws.seed_materialize_gate_negative_case(
            scenario="anchor_core_below_materialize_min",
        )
        snapshot = ws.new_observation_snapshot_service().build_snapshot(
            observation_profile_id=ws.observation_profile_id,
            candidate_knn_limit=24,
        )
        run = ws.new_cluster_run_service().execute_run(
            observation_snapshot_id=int(snapshot["snapshot_id"]),
            cluster_profile_id=ws.cluster_profile_id,
            supersedes_run_id=None,
            select_as_review_target=True,
        )
        result = ws.new_cluster_prepare_service().prepare_run(run_id=int(run["run_id"]))

        assert int(result["prepared_cluster_count"]) >= 0
        rows = ws.conn.execute(
            """
            SELECT c.id, r.resolution_state, r.publish_state
            FROM identity_cluster AS c
            JOIN identity_cluster_resolution AS r ON r.cluster_id = c.id
            WHERE c.run_id = ?
              AND c.cluster_stage = 'final'
              AND c.cluster_state = 'active'
            """,
            (int(run["run_id"]),),
        ).fetchall()
        assert rows
        assert any(
            row["resolution_state"] == "review_pending" and row["publish_state"] == "not_applicable"
            for row in rows
        )
    finally:
        ws.close()
```

`fixtures_identity_v3_1.py` 中的 `seed_materialize_candidate_case()` 也必须显式描述向量布局：至少包含 `anchor_core`、`core`、`boundary` 三类 retained 成员，并保证 trusted seed 排序在测试中可复现，不允许靠 observation_id 偶然过测。
同时新增 `seed_materialize_gate_negative_case()`：至少覆盖“通过 existence gate 但未通过 materialize gate”的场景（如 `anchor_core_count < materialize_min_anchor_core_count`）。

```python
# tests/people_gallery/test_identity_run_activation_service.py
from pathlib import Path

import pytest

from .fixtures_identity_v3_1 import build_identity_phase1_workspace


def test_activate_run_switches_owner_and_live_assignment_seed_prototype_ann(tmp_path: Path) -> None:
    ws = build_identity_phase1_workspace(tmp_path / "activate-run")
    try:
        ws.seed_materialize_candidate_case()
        snapshot = ws.new_observation_snapshot_service().build_snapshot(
            observation_profile_id=ws.observation_profile_id,
            candidate_knn_limit=24,
        )
        service = ws.new_cluster_run_service()
        run_a = service.execute_run(
            observation_snapshot_id=int(snapshot["snapshot_id"]),
            cluster_profile_id=ws.cluster_profile_id,
            supersedes_run_id=None,
            select_as_review_target=True,
        )
        ws.new_cluster_prepare_service().prepare_run(run_id=int(run_a["run_id"]))
        ws.new_run_activation_service().activate_run(run_id=int(run_a["run_id"]))

        first_owner = ws.get_cluster_run(int(run_a["run_id"]))
        first_live_count = ws.conn.execute(
            """
            SELECT COUNT(*) AS c
            FROM person_cluster_origin
            WHERE source_run_id = ?
              AND active = 1
            """,
            (int(run_a["run_id"]),),
        ).fetchone()
        first_live_assignments = ws.conn.execute(
            """
            SELECT COUNT(*) AS c
            FROM person_face_assignment
            WHERE active = 1
              AND source_run_id = ?
            """,
            (int(run_a["run_id"]),),
        ).fetchone()
        first_live_seeds = ws.conn.execute(
            """
            SELECT COUNT(*) AS c
            FROM person_trusted_sample
            WHERE active = 1
              AND source_run_id = ?
            """,
            (int(run_a["run_id"]),),
        ).fetchone()
        assert bool(first_owner["is_materialization_owner"]) is True
        assert int(first_live_count["c"]) >= 1
        assert int(first_live_assignments["c"]) >= 1
        assert int(first_live_seeds["c"]) >= 1
        assert ws.get_live_prototype_owner_run_id() == int(run_a["run_id"])
        assert ws.get_live_ann_owner_run_id() == int(run_a["run_id"])

        run_b = service.execute_run(
            observation_snapshot_id=int(snapshot["snapshot_id"]),
            cluster_profile_id=ws.cluster_profile_id,
            supersedes_run_id=int(run_a["run_id"]),
            select_as_review_target=True,
        )
        ws.new_cluster_prepare_service().prepare_run(run_id=int(run_b["run_id"]))
        ws.new_run_activation_service().activate_run(run_id=int(run_b["run_id"]))

        old_live_count = ws.conn.execute(
            """
            SELECT COUNT(*) AS c
            FROM person_cluster_origin
            WHERE source_run_id = ?
              AND active = 1
            """,
            (int(run_a["run_id"]),),
        ).fetchone()
        assert bool(ws.get_cluster_run(int(run_a["run_id"]))["is_materialization_owner"]) is False
        assert bool(ws.get_cluster_run(int(run_b["run_id"]))["is_materialization_owner"]) is True
        assert int(old_live_count["c"]) == 0
        assert ws.get_live_prototype_owner_run_id() == int(run_b["run_id"])
        assert ws.get_live_ann_owner_run_id() == int(run_b["run_id"])
    finally:
        ws.close()


def test_activate_run_rejects_checksum_mismatch_and_rolls_back_live_side_effects(tmp_path: Path) -> None:
    ws = build_identity_phase1_workspace(tmp_path / "activate-run-checksum")
    try:
        ws.seed_materialize_candidate_case()
        snapshot = ws.new_observation_snapshot_service().build_snapshot(
            observation_profile_id=ws.observation_profile_id,
            candidate_knn_limit=24,
        )
        run = ws.new_cluster_run_service().execute_run(
            observation_snapshot_id=int(snapshot["snapshot_id"]),
            cluster_profile_id=ws.cluster_profile_id,
            supersedes_run_id=None,
            select_as_review_target=True,
        )
        ws.new_cluster_prepare_service().prepare_run(run_id=int(run["run_id"]))
        ws.corrupt_prepared_ann_artifact(run_id=int(run["run_id"]))

        with pytest.raises(ValueError, match="checksum"):
            ws.new_run_activation_service().activate_run(run_id=int(run["run_id"]))

        owner = ws.conn.execute(
            "SELECT COUNT(*) AS c FROM identity_cluster_run WHERE is_materialization_owner = 1"
        ).fetchone()
        live_people = ws.conn.execute(
            "SELECT COUNT(*) AS c FROM person_cluster_origin WHERE source_run_id = ? AND active = 1",
            (int(run["run_id"]),),
        ).fetchone()
        assert int(owner["c"]) == 0
        assert int(live_people["c"]) == 0
        assert ws.get_live_ann_owner_run_id() is None
    finally:
        ws.close()


def test_activate_run_marks_publish_failed_and_no_false_published_when_publish_stage_errors(tmp_path: Path) -> None:
    ws = build_identity_phase1_workspace(tmp_path / "activate-run-publish-failed")
    try:
        ws.seed_materialize_candidate_case()
        snapshot = ws.new_observation_snapshot_service().build_snapshot(
            observation_profile_id=ws.observation_profile_id,
            candidate_knn_limit=24,
        )
        run = ws.new_cluster_run_service().execute_run(
            observation_snapshot_id=int(snapshot["snapshot_id"]),
            cluster_profile_id=ws.cluster_profile_id,
            supersedes_run_id=None,
            select_as_review_target=True,
        )
        ws.new_cluster_prepare_service().prepare_run(run_id=int(run["run_id"]))
        ws.stub_publish_stage_failure(run_id=int(run["run_id"]), reason="person_publish_plan_invalid")

        with pytest.raises(RuntimeError, match="publish"):
            ws.new_run_activation_service().activate_run(run_id=int(run["run_id"]))

        failed = ws.list_cluster_resolutions(run_id=int(run["run_id"]))
        assert any(item["publish_state"] == "publish_failed" for item in failed)
        assert all(item["publish_state"] != "published" for item in failed if item["publish_state"] == "publish_failed")
        assert all(item["publish_failure_reason"] for item in failed if item["publish_state"] == "publish_failed")
        run_live_people = ws.conn.execute(
            "SELECT COUNT(*) AS c FROM person_cluster_origin WHERE source_run_id = ? AND active = 1",
            (int(run["run_id"]),),
        ).fetchone()
        assert int(run_live_people["c"]) == 0
    finally:
        ws.close()


def test_activate_run_failure_keeps_previous_owner_live_state_unchanged(tmp_path: Path) -> None:
    ws = build_identity_phase1_workspace(tmp_path / "activate-run-keep-owner")
    try:
        ws.seed_materialize_candidate_case()
        snapshot = ws.new_observation_snapshot_service().build_snapshot(
            observation_profile_id=ws.observation_profile_id,
            candidate_knn_limit=24,
        )
        service = ws.new_cluster_run_service()

        run_a = service.execute_run(
            observation_snapshot_id=int(snapshot["snapshot_id"]),
            cluster_profile_id=ws.cluster_profile_id,
            supersedes_run_id=None,
            select_as_review_target=True,
        )
        ws.new_cluster_prepare_service().prepare_run(run_id=int(run_a["run_id"]))
        ws.new_run_activation_service().activate_run(run_id=int(run_a["run_id"]))

        run_b = service.execute_run(
            observation_snapshot_id=int(snapshot["snapshot_id"]),
            cluster_profile_id=ws.cluster_profile_id,
            supersedes_run_id=int(run_a["run_id"]),
            select_as_review_target=True,
        )
        ws.new_cluster_prepare_service().prepare_run(run_id=int(run_b["run_id"]))
        ws.corrupt_prepared_ann_artifact(run_id=int(run_b["run_id"]))
        with pytest.raises(ValueError, match="checksum"):
            ws.new_run_activation_service().activate_run(run_id=int(run_b["run_id"]))

        assert bool(ws.get_cluster_run(int(run_a["run_id"]))["is_materialization_owner"]) is True
        assert bool(ws.get_cluster_run(int(run_b["run_id"]))["is_materialization_owner"]) is False
        assert ws.get_live_ann_owner_run_id() == int(run_a["run_id"])
        assert ws.get_live_prototype_owner_run_id() == int(run_a["run_id"])
        run_b_live_people = ws.conn.execute(
            "SELECT COUNT(*) AS c FROM person_cluster_origin WHERE source_run_id = ? AND active = 1",
            (int(run_b["run_id"]),),
        ).fetchone()
        assert int(run_b_live_people["c"]) == 0
    finally:
        ws.close()


```

- [x] **Step 2: 运行测试并确认失败**

Run: `source .venv/bin/activate && PYTHONPATH=src python3 -m pytest tests/people_gallery/test_identity_cluster_prepare_service.py tests/people_gallery/test_identity_run_activation_service.py -q`
Expected: FAIL，缺少 `prepare_run` / `activate_run`，或 manifest checksum、`materialized/prepared/published/publish_failed` 状态机、live assignment/trusted seed/prototype/ANN 切换、existing owner 失败保持、run-ann 失败全量回退未实现。

- [x] **Step 3: 实现 cluster-prepare 与 run-prepare，按 manifest + checksum 完整性决定 `materialized/prepared`**

```python
# src/hikbox_pictures/services/identity_cluster_prepare_service.py
class IdentityClusterPrepareService:
    def __init__(self, conn, *, publish_repo, person_repo, prototype_service, ann_index_store) -> None:
        self.conn = conn
        self.publish_repo = publish_repo
        self.person_repo = person_repo
        self.prototype_service = prototype_service
        self.ann_index_store = ann_index_store

    def prepare_run(self, *, run_id: int) -> dict[str, int]:
        prepared_cluster_ids: list[int] = []
        candidate_cluster_ids: list[int] = []
        for resolution in self.publish_repo.list_prepare_candidates(run_id=run_id):
            candidate_cluster_ids.append(int(resolution["cluster_id"]))
            manifest = self.publish_repo.prepare_cluster_bundle(
                cluster_id=int(resolution["cluster_id"]),
                run_id=run_id,
            )
            if not self.publish_repo.verify_cluster_bundle_manifest(manifest):
                self.publish_repo.mark_review_pending(
                    cluster_id=int(resolution["cluster_id"]),
                    resolution_reason="cluster_bundle_incomplete_or_checksum_mismatch",
                )
                continue
            prepared_cluster_ids.append(int(resolution["cluster_id"]))

        ann_manifest = self.publish_repo.prepare_run_ann_bundle(
            run_id=run_id,
            prepared_cluster_ids=prepared_cluster_ids,
        )
        if not self.publish_repo.verify_run_ann_manifest(ann_manifest):
            self.publish_repo.mark_run_prepare_failed_and_rollback_candidates(
                run_id=run_id,
                candidate_cluster_ids=candidate_cluster_ids,
                reason="run_ann_bundle_failed_or_checksum_mismatch",
            )
            return {"prepared_cluster_count": 0}
        self.publish_repo.mark_run_prepared(
            run_id=run_id,
            cluster_ids=prepared_cluster_ids,
            ann_manifest=ann_manifest,
        )
        return {"prepared_cluster_count": len(prepared_cluster_ids)}
```

- [x] **Step 4: 实现 activate-run 发布流程（不引入 activation journal / 自动 crash recovery）**

```python
# src/hikbox_pictures/services/identity_run_activation_service.py
class IdentityRunActivationService:
    def __init__(self, conn, *, publish_repo, person_repo, prototype_service, ann_index_store) -> None:
        self.conn = conn
        self.publish_repo = publish_repo
        self.person_repo = person_repo
        self.prototype_service = prototype_service
        self.ann_index_store = ann_index_store

    def activate_run(self, *, run_id: int) -> None:
        target = self.publish_repo.get_prepared_run_required_with_verified_manifest(run_id)
        previous_owner = self.publish_repo.get_materialization_owner()
        self.ann_index_store.verify_prepared_artifact(
            artifact_path=Path(target["prepared_ann_path"]),
            expected_checksum=str(target["prepared_ann_checksum"]),
        )

        self.conn.execute("BEGIN IMMEDIATE")
        try:
            if previous_owner is not None:
                self.person_repo.retire_bootstrap_people(source_run_id=int(previous_owner["id"]))
                self.publish_repo.clear_materialization_owner()

            for bundle in self.publish_repo.list_prepared_publish_bundles(run_id=run_id):
                person_id = self.person_repo.create_anonymous_person(sequence=self.person_repo.next_anonymous_sequence())
                self.person_repo.apply_person_publish_plan(
                    person_id=person_id,
                    publish_plan=bundle["person_publish_plan"],
                    source_run_id=run_id,
                    source_cluster_id=int(bundle["cluster_id"]),
                )
                self.prototype_service.activate_prepared_cluster_prototype(
                    run_id=run_id,
                    cluster_id=int(bundle["cluster_id"]),
                    person_id=person_id,
                )
                self.publish_repo.mark_cluster_published(
                    cluster_id=int(bundle["cluster_id"]),
                    person_id=person_id,
                )

            self.publish_repo.set_materialization_owner(run_id=run_id)
            self.publish_repo.mark_run_activated(run_id=run_id)
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            self.publish_repo.mark_clusters_publish_failed_for_activation(
                run_id=run_id,
                reason="publish_transaction_failed",
            )
            raise

        try:
            self.ann_index_store.activate_verified_artifact(
                artifact_path=Path(target["prepared_ann_path"]),
                expected_checksum=str(target["prepared_ann_checksum"]),
                source_run_id=run_id,
            )
        except Exception as exc:
            # Phase 1 简化：不做 journal 恢复；ANN 切换失败时立即回滚本轮 owner，并恢复旧 owner（若存在）。
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                self.person_repo.retire_bootstrap_people(source_run_id=run_id)
                self.publish_repo.clear_materialization_owner()
                self.publish_repo.mark_clusters_publish_failed_for_activation(
                    run_id=run_id,
                    reason=f"live_ann_switch_failed:{exc}",
                )
                if previous_owner is not None:
                    self.publish_repo.set_materialization_owner(run_id=int(previous_owner["id"]))
                self.conn.commit()
            except Exception:
                self.conn.rollback()
            self.publish_repo.mark_clusters_publish_failed_for_activation(
                run_id=run_id,
                reason=f"live_ann_switch_failed_compensation_required:{exc}",
            )
            raise
```

- [x] **Step 5: 回跑 prepare/publish 测试并确认无半成品 live 状态**

Run: `source .venv/bin/activate && PYTHONPATH=src python3 -m pytest tests/people_gallery/test_identity_cluster_prepare_service.py tests/people_gallery/test_identity_run_activation_service.py -q`
Expected: PASS，`prepare` 前没有 live person，`run-prepare` 成功后才出现 `materialized + prepared`；低于 materialize gate 的 cluster 在 prepare 后仍保持 `review_pending + not_applicable`；`activate_run` 后只有一个 owner run 且 live assignment/trusted seed/prototype/ANN 同步切到该 run；激活失败时若已有旧 owner 则保持旧 owner/live 指针不变；run-ann 失败时候选 cluster 统一回退 `review_pending`；publish 阶段错误必须落 `publish_failed + publish_failure_reason` 且无 `published` 假阳性。

**Task completion action (not a checkbox step): Commit task changes and plan progress**

```bash
git add src/hikbox_pictures/repositories/identity_publish_repo.py src/hikbox_pictures/services/identity_cluster_prepare_service.py src/hikbox_pictures/services/identity_run_activation_service.py src/hikbox_pictures/repositories/person_repo.py src/hikbox_pictures/services/prototype_service.py src/hikbox_pictures/ann/index_store.py tests/people_gallery/fixtures_identity_v3_1.py tests/people_gallery/test_identity_cluster_prepare_service.py tests/people_gallery/test_identity_run_activation_service.py docs/superpowers/plans/2026-04-17-hikbox-pictures-v3-1-cluster-bootstrap-phase1.md
git commit -m "feat: add prepared publish and run activation flow (Task 5)"
```

### Task 6: 显式 snapshot/rerun/select/activate 脚本与兼容 wrapper

**Depends on:** Task 2, Task 3, Task 4, Task 5

**Scope Budget:**
- Max files: 20
- Estimated files touched: 7
- Max added lines: 1000
- Estimated added lines: 780

**Files:**
- Create: `src/hikbox_pictures/services/identity_bootstrap_orchestrator.py`
- Create: `scripts/build_identity_observation_snapshot.py`
- Create: `scripts/rerun_identity_cluster_run.py`
- Create: `scripts/select_identity_cluster_run.py`
- Create: `scripts/activate_identity_cluster_run.py`
- Modify: `scripts/rebuild_identities_v3.py`
- Create: `tests/people_gallery/test_identity_cluster_bootstrap_scripts.py`

- [ ] **Step 1: 先写失败测试，固定显式脚本入口、rerun 保留历史 run 行为，以及 `succeeded` 前置落库完整性**

```python
# tests/people_gallery/test_identity_cluster_bootstrap_scripts.py
import os
import subprocess
import sys
from pathlib import Path

from .fixtures_identity_v3_1 import build_identity_phase1_workspace


REPO_ROOT = Path(__file__).resolve().parents[2]


def _run_script(script_name: str, *args: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(REPO_ROOT / "src")
    return subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / script_name), *args],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def test_scripts_create_snapshot_rerun_history_and_switch_review_target(tmp_path: Path) -> None:
    ws = build_identity_phase1_workspace(tmp_path / "cluster-scripts")
    try:
        ws.seed_materialize_candidate_case()

        build = _run_script(
            "build_identity_observation_snapshot.py",
            "--workspace",
            str(ws.root),
        )
        assert build.returncode == 0
        snapshot_id = int(
            ws.conn.execute(
                "SELECT id FROM identity_observation_snapshot ORDER BY id DESC LIMIT 1"
            ).fetchone()["id"]
        )
        secondary_cluster_profile_id = ws.create_cluster_profile_variant(
            profile_name="phase1-alt-profile",
            discovery_knn_k=12,
            density_min_samples=3,
        )

        first = _run_script(
            "rerun_identity_cluster_run.py",
            "--workspace",
            str(ws.root),
            "--snapshot-id",
            str(snapshot_id),
        )
        assert first.returncode == 0
        first_run_id = int(
            ws.conn.execute(
                "SELECT id FROM identity_cluster_run ORDER BY id DESC LIMIT 1"
            ).fetchone()["id"]
        )
        first_run = ws.get_cluster_run(first_run_id)
        assert first_run["run_status"] == "succeeded"
        assert int(ws.count_clusters(run_id=first_run_id)) > 0
        assert int(ws.count_cluster_members(run_id=first_run_id)) > 0
        assert int(ws.count_cluster_resolutions(run_id=first_run_id)) > 0

        second = _run_script(
            "rerun_identity_cluster_run.py",
            "--workspace",
            str(ws.root),
            "--snapshot-id",
            str(snapshot_id),
            "--cluster-profile-id",
            str(secondary_cluster_profile_id),
            "--supersedes-run-id",
            str(first_run_id),
            "--no-select-review-target",
        )
        assert second.returncode == 0
        second_run_id = int(
            ws.conn.execute(
                "SELECT id FROM identity_cluster_run ORDER BY id DESC LIMIT 1"
            ).fetchone()["id"]
        )

        switched = _run_script(
            "select_identity_cluster_run.py",
            "--workspace",
            str(ws.root),
            "--run-id",
            str(second_run_id),
        )
        assert switched.returncode == 0
        second_run = ws.get_cluster_run(second_run_id)
        assert int(first_run["observation_snapshot_id"]) == int(second_run["observation_snapshot_id"])
        assert int(first_run["cluster_profile_id"]) != int(second_run["cluster_profile_id"])
        assert bool(ws.get_cluster_run(first_run_id)["is_review_target"]) is False
        assert bool(ws.get_cluster_run(second_run_id)["is_review_target"]) is True
    finally:
        ws.close()


def test_activate_script_sets_materialization_owner_and_rejects_unprepared_run(tmp_path: Path) -> None:
    ws = build_identity_phase1_workspace(tmp_path / "cluster-activate-script")
    try:
        ws.seed_materialize_candidate_case()
        snapshot = ws.new_observation_snapshot_service().build_snapshot(
            observation_profile_id=ws.observation_profile_id,
            candidate_knn_limit=24,
        )
        run = ws.new_cluster_run_service().execute_run(
            observation_snapshot_id=int(snapshot["snapshot_id"]),
            cluster_profile_id=ws.cluster_profile_id,
            supersedes_run_id=None,
            select_as_review_target=True,
        )

        failed = _run_script(
            "activate_identity_cluster_run.py",
            "--workspace",
            str(ws.root),
            "--run-id",
            str(int(run["run_id"])),
        )
        assert failed.returncode != 0
        assert bool(ws.get_cluster_run(int(run["run_id"]))["is_materialization_owner"]) is False

        ws.new_cluster_prepare_service().prepare_run(run_id=int(run["run_id"]))
        ok = _run_script(
            "activate_identity_cluster_run.py",
            "--workspace",
            str(ws.root),
            "--run-id",
            str(int(run["run_id"])),
        )
        assert ok.returncode == 0
        assert bool(ws.get_cluster_run(int(run["run_id"]))["is_materialization_owner"]) is True
    finally:
        ws.close()
```

- [ ] **Step 2: 运行测试并确认失败**

Run: `source .venv/bin/activate && PYTHONPATH=src python3 -m pytest tests/people_gallery/test_identity_cluster_bootstrap_scripts.py -q`
Expected: FAIL，缺少新脚本文件或 `identity_bootstrap_orchestrator.py`。

- [ ] **Step 3: 实现 orchestrator 与四个显式脚本入口，`rebuild_identities_v3.py` 改为兼容 wrapper**

```python
# src/hikbox_pictures/services/identity_bootstrap_orchestrator.py
from hikbox_pictures.ann import AnnIndexStore
from hikbox_pictures.services.prototype_service import PrototypeService
from hikbox_pictures.workspace import load_workspace_paths


class IdentityBootstrapOrchestrator:
    def __init__(self, workspace: Path) -> None:
        self.workspace = Path(workspace).expanduser().resolve()
        self.conn = connect_db(self.workspace / ".hikbox" / "library.db")

    def build_snapshot(self, *, observation_profile_id: int | None, candidate_knn_limit: int) -> dict[str, Any]:
        snapshot_service = IdentityObservationSnapshotService(
            self.conn,
            observation_repo=IdentityObservationRepo(self.conn),
            quality_backfill_service=ObservationQualityBackfillService(self.conn),
        )
        resolved_profile_id = observation_profile_id or IdentityObservationProfileService(self.conn).get_active_profile_id()
        return snapshot_service.build_snapshot(
            observation_profile_id=int(resolved_profile_id),
            candidate_knn_limit=int(candidate_knn_limit),
        )

    def rerun_cluster_run(
        self,
        *,
        snapshot_id: int,
        cluster_profile_id: int | None,
        supersedes_run_id: int | None,
        select_as_review_target: bool,
    ) -> dict[str, Any]:
        run_service = IdentityClusterRunService(
            self.conn,
            cluster_run_repo=IdentityClusterRunRepo(self.conn),
            cluster_repo=IdentityClusterRepo(self.conn),
        )
        resolved_cluster_profile_id = cluster_profile_id or IdentityClusterProfileService(self.conn).get_active_profile_id()
        result = run_service.execute_run(
            observation_snapshot_id=int(snapshot_id),
            cluster_profile_id=int(resolved_cluster_profile_id),
            supersedes_run_id=supersedes_run_id,
            select_as_review_target=select_as_review_target,
        )
        IdentityClusterPrepareService(
            self.conn,
            publish_repo=IdentityPublishRepo(self.conn),
            person_repo=PersonRepo(self.conn),
            prototype_service=PrototypeService(
                self.conn,
                PersonRepo(self.conn),
                AnnIndexStore(load_workspace_paths(self.workspace).artifacts_dir / "ann" / "prototype_index.npz"),
            ),
            ann_index_store=AnnIndexStore(load_workspace_paths(self.workspace).artifacts_dir / "ann" / "prototype_index.npz"),
        ).prepare_run(run_id=int(result["run_id"]))
        return result

    def select_review_target(self, *, run_id: int) -> None:
        IdentityClusterRunService(
            self.conn,
            cluster_run_repo=IdentityClusterRunRepo(self.conn),
            cluster_repo=IdentityClusterRepo(self.conn),
        ).select_review_target(run_id=run_id)

    def activate_run(self, *, run_id: int) -> dict[str, Any]:
        activation_service = IdentityRunActivationService(
            self.conn,
            publish_repo=IdentityPublishRepo(self.conn),
            person_repo=PersonRepo(self.conn),
            prototype_service=PrototypeService(
                self.conn,
                PersonRepo(self.conn),
                AnnIndexStore(load_workspace_paths(self.workspace).artifacts_dir / "ann" / "prototype_index.npz"),
            ),
            ann_index_store=AnnIndexStore(load_workspace_paths(self.workspace).artifacts_dir / "ann" / "prototype_index.npz"),
        )
        activation_service.activate_run(run_id=run_id)
        return {"activated_run_id": int(run_id)}
```

```python
# scripts/rerun_identity_cluster_run.py
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="在指定 observation snapshot 上执行新的 cluster run。")
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--snapshot-id", type=int, required=True)
    parser.add_argument("--cluster-profile-id", type=int, default=None)
    parser.add_argument("--supersedes-run-id", type=int, default=None)
    parser.add_argument("--no-select-review-target", action="store_true")
    return parser
```

```python
# scripts/select_identity_cluster_run.py
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="切换默认 review target run。")
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--run-id", type=int, required=True)
    args = parser.parse_args(argv)
    orchestrator = IdentityBootstrapOrchestrator(Path(args.workspace))
    orchestrator.select_review_target(run_id=int(args.run_id))
    print(json.dumps({"selected_run_id": int(args.run_id)}, ensure_ascii=False))
    return 0
```

```python
# scripts/activate_identity_cluster_run.py
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="把 prepared run 发布为 live materialization owner。")
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--run-id", type=int, required=True)
    args = parser.parse_args(argv)
    orchestrator = IdentityBootstrapOrchestrator(Path(args.workspace))
    summary = orchestrator.activate_run(run_id=int(args.run_id))
    print(json.dumps(summary, ensure_ascii=False))
    return 0
```

- [ ] **Step 4: 回跑脚本测试，并手动验证 `rebuild_identities_v3.py` 仍可一键跑通 snapshot + rerun**

Run: `source .venv/bin/activate && PYTHONPATH=src python3 -m pytest tests/people_gallery/test_identity_cluster_bootstrap_scripts.py -q`
Expected: PASS，`build snapshot -> rerun -> select -> activate` 全部可执行，且第二轮 run 不覆盖第一轮历史；同一 `snapshot_id` 上可指定不同 `cluster_profile_id` 生成独立 run；脚本级验证未 `prepare` 的 run 不能激活、`prepare` 后可成功激活；并验证 `rerun` 产出的 `succeeded` run 已有 `cluster/member/resolution` 完整落库，不允许只写 run 壳记录。

Run: `source .venv/bin/activate && PYTHONPATH=src python3 scripts/rebuild_identities_v3.py --workspace <workspace>`
Expected: 返回 JSON 摘要，至少包含 `snapshot_id`、`run_id`、`prepared_cluster_count`，不再提 `auto_cluster_batch`。

**Task completion action (not a checkbox step): Commit task changes and plan progress**

```bash
git add src/hikbox_pictures/services/identity_bootstrap_orchestrator.py scripts/build_identity_observation_snapshot.py scripts/rerun_identity_cluster_run.py scripts/select_identity_cluster_run.py scripts/activate_identity_cluster_run.py scripts/rebuild_identities_v3.py tests/people_gallery/test_identity_cluster_bootstrap_scripts.py docs/superpowers/plans/2026-04-17-hikbox-pictures-v3-1-cluster-bootstrap-phase1.md
git commit -m "feat: add explicit snapshot and cluster run scripts (Task 6)"
```

### Task 7: `/identity-tuning` 升级为 run 级证据页

**Depends on:** Task 4, Task 5

**Scope Budget:**
- Max files: 20
- Estimated files touched: 7
- Max added lines: 1000
- Estimated added lines: 740

**Files:**
- Create: `src/hikbox_pictures/services/identity_review_query_service.py`
- Modify: `src/hikbox_pictures/api/routes_web.py`
- Modify: `src/hikbox_pictures/web/templates/identity_tuning.html`
- Modify: `src/hikbox_pictures/web/templates/base.html`
- Modify: `src/hikbox_pictures/web/static/style.css`
- Modify: `tests/people_gallery/test_web_identity_tuning_page.py`
- Modify: `tests/people_gallery/test_web_navigation.py`

- [x] **Step 1: 先写失败测试，锁定默认 review target、显式 `run_id` 与完整 review 证据面**

```python
# tests/people_gallery/test_web_identity_tuning_page.py
def test_identity_tuning_page_defaults_to_review_target_and_accepts_run_id(tmp_path: Path) -> None:
    ws = build_identity_phase1_workspace(tmp_path / "identity-tuning-v3_1")
    try:
        ws.seed_split_and_attachment_case()
        snapshot = ws.new_observation_snapshot_service().build_snapshot(
            observation_profile_id=ws.observation_profile_id,
            candidate_knn_limit=24,
        )
        first = ws.new_cluster_run_service().execute_run(
            observation_snapshot_id=int(snapshot["snapshot_id"]),
            cluster_profile_id=ws.cluster_profile_id,
            supersedes_run_id=None,
            select_as_review_target=True,
        )
        second = ws.new_cluster_run_service().execute_run(
            observation_snapshot_id=int(snapshot["snapshot_id"]),
            cluster_profile_id=ws.cluster_profile_id,
            supersedes_run_id=int(first["run_id"]),
            select_as_review_target=False,
        )

        client = TestClient(create_app(workspace=ws.root))

        default_response = client.get("/identity-tuning")
        explicit_response = client.get(f"/identity-tuning?run_id={int(second['run_id'])}")

        default_payload = _extract_embedded_json(default_response.text)
        explicit_payload = _extract_embedded_json(explicit_response.text)

        assert int(default_payload["review_run"]["id"]) == int(first["run_id"])
        assert int(explicit_payload["review_run"]["id"]) == int(second["run_id"])
        assert explicit_payload["review_run"]["observation_snapshot_id"] == default_payload["review_run"]["observation_snapshot_id"]
        run_summary = explicit_payload["run_summary"]
        assert "observation_total" in run_summary
        assert "pool_counts" in run_summary
        assert "final_cluster_counts" in run_summary
        assert "resolution_counts" in run_summary

        cluster = explicit_payload["clusters"][0]
        cluster_id = int(cluster["cluster_id"])
        assert cluster["lineage"]
        assert "publish_state" in cluster["resolution"]
        assert "prototype_status" in cluster["resolution"]
        assert "ann_status" in cluster["resolution"]
        assert cluster["metrics"]["compactness_p90"] is not None
        assert cluster["metrics"]["separation_gap"] is not None
        assert cluster["metrics"]["boundary_ratio"] is not None
        assert cluster["metrics"]["support_ratio_p10"] is not None
        assert cluster["metrics"]["support_ratio_p50"] is not None
        assert cluster["metrics"]["intra_photo_conflict_ratio"] is not None
        assert cluster["metrics"]["nearest_cluster_distance"] is not None
        assert cluster["seed_audit"]["trusted_seed_candidate_count"] >= cluster["seed_audit"]["trusted_seed_count"]
        assert cluster["members"]["representative"]
        assert cluster["members"]["retained"]
        assert cluster["members"]["excluded"]
        assert "excluded_reason_distribution" in cluster["members"]
        assert "trusted_seed_reject_distribution" in cluster["seed_audit"]

        db_resolution = ws.get_cluster_resolution(cluster_id=cluster_id)
        assert cluster["resolution"]["resolution_reason"] == db_resolution["resolution_reason"]
        assert cluster["resolution"]["publish_state"] == db_resolution["publish_state"]
        assert (
            cluster["seed_audit"]["trusted_seed_reject_distribution"]
            == db_resolution["trusted_seed_reject_distribution"]
        )
        retained_seed_ranks = [
            member["seed_rank"]
            for member in cluster["members"]["retained"]
            if bool(member.get("is_selected_trusted_seed"))
        ]
        assert retained_seed_ranks
        assert all(int(rank) >= 1 for rank in retained_seed_ranks if rank is not None)
        assert run_summary["dedup_drop_distribution"] == ws.get_snapshot_dedup_distribution(
            int(explicit_payload["review_run"]["observation_snapshot_id"])
        )
    finally:
        ws.close()


def test_identity_tuning_page_returns_integrity_error_when_review_target_missing(tmp_path: Path) -> None:
    ws = build_identity_phase1_workspace(tmp_path / "identity-tuning-missing-review-target")
    try:
        ws.seed_split_and_attachment_case()
        snapshot = ws.new_observation_snapshot_service().build_snapshot(
            observation_profile_id=ws.observation_profile_id,
            candidate_knn_limit=24,
        )
        ws.new_cluster_run_service().execute_run(
            observation_snapshot_id=int(snapshot["snapshot_id"]),
            cluster_profile_id=ws.cluster_profile_id,
            supersedes_run_id=None,
            select_as_review_target=True,
        )
        ws.conn.execute("UPDATE identity_cluster_run SET is_review_target = 0, review_selected_at = NULL")
        ws.conn.commit()

        client = TestClient(create_app(workspace=ws.root))
        response = client.get("/identity-tuning")
        assert response.status_code == 409
        assert "review target" in response.text
    finally:
        ws.close()
```

- [x] **Step 2: 运行测试并确认失败**

Run: `source .venv/bin/activate && PYTHONPATH=src python3 -m pytest tests/people_gallery/test_web_identity_tuning_page.py tests/people_gallery/test_web_navigation.py -q`
Expected: FAIL，页面 payload 仍然是旧 `bootstrap_batch` / `anonymous_people` 结构，且不支持 `run_id`。

- [x] **Step 3: 新增 query service，并让 route 直接围绕 run 组织 run/cluster/member 全量证据**

```python
# src/hikbox_pictures/services/identity_review_query_service.py
class IdentityReviewQueryService:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def get_identity_tuning_page(self, *, run_id: int | None) -> dict[str, Any]:
        selected_run = self._resolve_run(run_id=run_id)
        clusters = self._load_final_clusters(run_id=int(selected_run["id"]))
        return {
            "review_run": selected_run,
            "observation_snapshot": self._load_snapshot(int(selected_run["observation_snapshot_id"])),
            "observation_profile": self._load_observation_profile(int(selected_run["observation_profile_id"])),
            "cluster_profile": self._load_cluster_profile(int(selected_run["cluster_profile_id"])),
            "run_summary": self._load_run_summary(int(selected_run["id"])),
            "clusters": clusters,
        }

    def _load_final_clusters(self, *, run_id: int) -> list[dict[str, Any]]:
        rows = self._query_final_clusters(run_id=run_id)
        return [
            {
                **row,
                "lineage": self._load_cluster_lineage(int(row["cluster_id"])),
                "metrics": self._load_cluster_metrics(int(row["cluster_id"])),
                "seed_audit": self._load_seed_audit(int(row["cluster_id"])),
                "resolution": self._load_cluster_resolution(int(row["cluster_id"])),
                "members": self._load_cluster_members_partitioned(int(row["cluster_id"])),
            }
            for row in rows
        ]
```

```python
# src/hikbox_pictures/api/routes_web.py
@router.get("/identity-tuning", response_class=HTMLResponse)
def identity_tuning_page(request: Request, run_id: int | None = None) -> HTMLResponse:
    conn = connect_db(Path(request.app.state.db_path))
    try:
        service = IdentityReviewQueryService(conn)
        page_data = service.get_identity_tuning_page(run_id=run_id)
        return _get_templates(request).TemplateResponse(
            request=request,
            name="identity_tuning.html",
            context={
                "page_title": "Bootstrap Run Review",
                "page_key": "identity_tuning",
                "identity_tuning": page_data,
            },
        )
    finally:
        conn.close()
```

- [x] **Step 4: 回跑页面测试，确认页面不再依赖 `auto_cluster*`/`origin_cluster_id` 且证据字段完整**

Run: `source .venv/bin/activate && PYTHONPATH=src python3 -m pytest tests/people_gallery/test_web_identity_tuning_page.py tests/people_gallery/test_web_navigation.py -q`
Expected: PASS，页面 JSON 顶层包含 `review_run`、`observation_snapshot`、`observation_profile`、`cluster_profile`、`run_summary`、`clusters`；cluster 级包含 lineage/metrics/seed_audit/resolution(prototype+ANN 状态)/members(代表+retained+excluded+原因分布)，且 metrics 至少覆盖 `compactness_p90`、`separation_gap`、`boundary_ratio`、`support_ratio_p10`、`support_ratio_p50`、`intra_photo_conflict_ratio`、`nearest_cluster_distance`；并对 `resolution_reason`、`publish_state`、`trusted_seed_reject_distribution`、`seed_rank`、`dedup_drop_distribution` 做 DB 对账断言；若缺少 `is_review_target=1`，接口返回数据完整性错误（不做隐式回退）。

**Task completion action (not a checkbox step): Commit task changes and plan progress**

```bash
git add src/hikbox_pictures/services/identity_review_query_service.py src/hikbox_pictures/api/routes_web.py src/hikbox_pictures/web/templates/identity_tuning.html src/hikbox_pictures/web/templates/base.html src/hikbox_pictures/web/static/style.css tests/people_gallery/test_web_identity_tuning_page.py tests/people_gallery/test_web_navigation.py docs/superpowers/plans/2026-04-17-hikbox-pictures-v3-1-cluster-bootstrap-phase1.md
git commit -m "feat: turn identity tuning into run review page (Task 7)"
```

### Task 8: Neighbor 导出升级为 run 语境证据工具

**Depends on:** Task 5

**Scope Budget:**
- Max files: 20
- Estimated files touched: 4
- Max added lines: 1000
- Estimated added lines: 620

**Files:**
- Modify: `src/hikbox_pictures/services/observation_neighbor_export_service.py`
- Modify: `scripts/export_observation_neighbors.py`
- Create: `tests/people_gallery/test_identity_cluster_neighbor_export_service.py`
- Modify: `tests/people_gallery/test_export_observation_neighbors_script.py`

- [x] **Step 1: 先写失败测试，锁定默认 review target、`--run-id`/`--cluster-id` 与完整导出证据面**

```python
# tests/people_gallery/test_export_observation_neighbors_script.py
def test_export_neighbors_manifest_contains_run_context_and_member_evidence(tmp_path: Path) -> None:
    ws = build_identity_phase1_workspace(tmp_path / "neighbors-v3_1")
    try:
        ws.seed_split_and_attachment_case()
        snapshot = ws.new_observation_snapshot_service().build_snapshot(
            observation_profile_id=ws.observation_profile_id,
            candidate_knn_limit=24,
        )
        run = ws.new_cluster_run_service().execute_run(
            observation_snapshot_id=int(snapshot["snapshot_id"]),
            cluster_profile_id=ws.cluster_profile_id,
            supersedes_run_id=None,
            select_as_review_target=True,
        )
        cluster_id = int(
            ws.conn.execute(
                """
                SELECT id
                FROM identity_cluster
                WHERE run_id = ?
                  AND cluster_stage = 'final'
                ORDER BY id ASC
                LIMIT 1
                """,
                (int(run["run_id"]),),
            ).fetchone()["id"]
        )
        output_dir = tmp_path / "neighbors-output"

        rc = export_main(
            [
                "--workspace",
                str(ws.root),
                "--run-id",
                str(run["run_id"]),
                "--cluster-id",
                str(cluster_id),
                "--neighbor-count",
                "3",
                "--output-root",
                str(output_dir),
            ]
        )

        assert rc == 0
        manifest = json.loads(next(output_dir.glob("*/manifest.json")).read_text(encoding="utf-8"))
        targets = manifest["targets"]
        assert targets
        target = targets[0]["target"]
        assert int(target["run_id"]) == int(run["run_id"])
        assert int(target["cluster_id"]) == int(cluster_id)
        assert int(target["observation_profile_id"]) == int(ws.observation_profile_id)
        assert int(target["cluster_profile_id"]) == int(ws.cluster_profile_id)
        assert target["cluster_stage"] == "final"
        assert target["member_role"] in {"anchor_core", "core", "boundary", "attachment"}
        assert "publish_state" in target
        assert "seed_rank" in target
        assert isinstance(target["is_selected_trusted_seed"], bool)
        assert target["decision_status"] in {"retained", "excluded"}
        assert any(item["target"]["decision_status"] == "retained" for item in targets)
        assert any(item["target"]["decision_status"] == "excluded" for item in targets)
        assert any(item["target"].get("is_representative_observation") is True for item in targets)
        assert any(item["neighbors"] for item in targets)
        assert any(
            neighbor.get("competition_rank") is not None
            for item in targets
            for neighbor in item["neighbors"]
        )
    finally:
        ws.close()


def test_export_neighbors_defaults_to_review_target_when_run_id_not_given(tmp_path: Path) -> None:
    ws = build_identity_phase1_workspace(tmp_path / "neighbors-default-review")
    try:
        ws.seed_split_and_attachment_case()
        snapshot = ws.new_observation_snapshot_service().build_snapshot(
            observation_profile_id=ws.observation_profile_id,
            candidate_knn_limit=24,
        )
        run_review = ws.new_cluster_run_service().execute_run(
            observation_snapshot_id=int(snapshot["snapshot_id"]),
            cluster_profile_id=ws.cluster_profile_id,
            supersedes_run_id=None,
            select_as_review_target=True,
        )
        run_latest = ws.new_cluster_run_service().execute_run(
            observation_snapshot_id=int(snapshot["snapshot_id"]),
            cluster_profile_id=ws.cluster_profile_id,
            supersedes_run_id=int(run_review["run_id"]),
            select_as_review_target=False,
        )
        assert int(run_latest["run_id"]) > int(run_review["run_id"])
        output_dir = tmp_path / "neighbors-output-default"
        rc = export_main(
            [
                "--workspace",
                str(ws.root),
                "--neighbor-count",
                "3",
                "--output-root",
                str(output_dir),
            ]
        )
        assert rc == 0
        manifest = json.loads(next(output_dir.glob("*/manifest.json")).read_text(encoding="utf-8"))
        assert int(manifest["targets"][0]["target"]["run_id"]) == int(run_review["run_id"])
    finally:
        ws.close()
```

- [x] **Step 2: 运行测试并确认失败**

Run: `source .venv/bin/activate && PYTHONPATH=src python3 -m pytest tests/people_gallery/test_identity_cluster_neighbor_export_service.py tests/people_gallery/test_export_observation_neighbors_script.py -q`
Expected: FAIL，脚本还只支持 `--observation-ids`，manifest 里也没有 run/cluster/member 语境字段。

- [x] **Step 3: 扩展导出服务和脚本参数，manifest 显式覆盖 retained/excluded/representative/竞争近邻证据**

```python
# scripts/export_observation_neighbors.py
def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="导出带 run 语境的 observation 邻域证据")
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--run-id", type=int, default=None)
    parser.add_argument("--cluster-id", type=int, default=None)
    parser.add_argument("--observation-ids", type=str, default=None)
    parser.add_argument("--neighbor-count", type=int, default=8)
    parser.add_argument("--output-root", type=Path, default=None)
    return parser
```

```python
# src/hikbox_pictures/services/observation_neighbor_export_service.py
resolved_run_id = run_id or self.cluster_run_repo.get_review_target_run_id_required()
records = self._load_target_records(run_id=int(resolved_run_id), cluster_id=cluster_id, observation_ids=observation_ids)

summary["targets"].append(
    {
        "target": {
            "run_id": int(record.run_id),
            "observation_snapshot_id": int(record.observation_snapshot_id),
            "observation_profile_id": int(record.observation_profile_id),
            "cluster_profile_id": int(record.cluster_profile_id),
            "cluster_id": int(record.cluster_id) if record.cluster_id is not None else None,
            "cluster_stage": record.cluster_stage,
            "member_role": record.member_role,
            "decision_status": record.decision_status,
            "exclusion_reason": record.exclusion_reason,
            "publish_state": record.publish_state,
            "decision_reason_code": record.decision_reason_code,
            "support_ratio": record.support_ratio,
            "distance_to_medoid": record.distance_to_medoid,
            "is_representative_observation": bool(record.is_representative_observation),
            "is_selected_trusted_seed": bool(record.is_selected_trusted_seed),
            "seed_rank": record.seed_rank,
            "distance": record.distance,
            "quality_score": record.quality_score,
        },
        "neighbors": neighbor_payloads,
    }
)
```

- [x] **Step 4: 回跑导出测试并人工检查 HTML/manifest 证据是否对齐**

Run: `source .venv/bin/activate && PYTHONPATH=src python3 -m pytest tests/people_gallery/test_identity_cluster_neighbor_export_service.py tests/people_gallery/test_export_observation_neighbors_script.py -q`
Expected: PASS，manifest 里有 `run_id`、`observation_profile_id`、`cluster_profile_id`、`cluster_stage`、`member_role`、`decision_status`、`publish_state`、`is_selected_trusted_seed`、`seed_rank`，并覆盖 representative/retained/excluded 与竞争近邻字段；未显式传 `--run-id` 时在“review_target != latest”场景下仍默认使用 review target。

**Task completion action (not a checkbox step): Commit task changes and plan progress**

```bash
git add src/hikbox_pictures/services/observation_neighbor_export_service.py scripts/export_observation_neighbors.py tests/people_gallery/test_identity_cluster_neighbor_export_service.py tests/people_gallery/test_export_observation_neighbors_script.py docs/superpowers/plans/2026-04-17-hikbox-pictures-v3-1-cluster-bootstrap-phase1.md
git commit -m "feat: add run-aware observation neighbor export (Task 8)"
```

### Task 9: README、弃用清理、Playwright 与端到端验收

**Depends on:** Task 6, Task 7, Task 8

**Scope Budget:**
- Max files: 20
- Estimated files touched: 12
- Max added lines: 1000
- Estimated added lines: 860

**Files:**
- Modify: `README.md`
- Modify: `scripts/evaluate_identity_thresholds.py`
- Modify: `src/hikbox_pictures/services/identity_threshold_evaluation_service.py`
- Modify: `tests/people_gallery/test_identity_threshold_evaluation_script.py`
- Modify: `tests/people_gallery/test_rebuild_identities_v3_script.py`
- Create: `tools/identity_tuning_playwright_check.py`
- Create: `tools/identity_tuning_playwright_capture.cjs`
- Create: `tests/people_gallery/test_identity_cluster_phase1_e2e.py`
- Modify: `tests/test_repo_samples.py`
- Delete: `tests/people_gallery/test_identity_bootstrap_service.py`
- Delete: `tests/people_gallery/test_identity_threshold_profile_contract.py`
- Delete: `tests/people_gallery/test_identity_rebuild_v3_real_pipeline.py`

- [ ] **Step 1: 先写失败测试，固定 README 新流程、旧评估脚本弃用提示与端到端闭环验收**

```python
# tests/people_gallery/test_identity_threshold_evaluation_script.py
import os
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]


def test_evaluate_identity_thresholds_script_returns_deprecation_hint(tmp_path: Path) -> None:
    workspace = tmp_path / "deprecated-eval"
    workspace.mkdir(parents=True, exist_ok=True)

    env = os.environ.copy()
    env["PYTHONPATH"] = str(REPO_ROOT / "src")
    result = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "scripts" / "evaluate_identity_thresholds.py"),
            "--workspace",
            str(workspace),
            "--output-dir",
            str(tmp_path / "out"),
        ],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 2
    assert "已弃用" in result.stderr
```

```python
# tests/people_gallery/test_identity_cluster_phase1_e2e.py
import html
import json
import re

from fastapi.testclient import TestClient

from hikbox_pictures.api.app import create_app

from .fixtures_identity_v3_1 import build_identity_phase1_workspace


def _extract_payload(html_text: str) -> dict[str, object]:
    match = re.search(
        r'<script id="identity-tuning-data" type="application/json">\s*(.*?)\s*</script>',
        html_text,
        re.DOTALL,
    )
    assert match is not None
    return json.loads(html.unescape(match.group(1)).strip())


def test_phase1_e2e_build_snapshot_rerun_select_activate_and_review(tmp_path: Path) -> None:
    ws = build_identity_phase1_workspace(tmp_path / "phase1-e2e")
    try:
        ws.seed_split_and_attachment_case()
        snapshot = ws.new_observation_snapshot_service().build_snapshot(
            observation_profile_id=ws.observation_profile_id,
            candidate_knn_limit=24,
        )
        run_a = ws.new_cluster_run_service().execute_run(
            observation_snapshot_id=int(snapshot["snapshot_id"]),
            cluster_profile_id=ws.cluster_profile_id,
            supersedes_run_id=None,
            select_as_review_target=True,
        )
        cluster_profile_b = ws.create_cluster_profile_variant(
            profile_name="phase1-e2e-alt",
            discovery_knn_k=12,
            density_min_samples=3,
        )
        run_b = ws.new_cluster_run_service().execute_run(
            observation_snapshot_id=int(snapshot["snapshot_id"]),
            cluster_profile_id=int(cluster_profile_b),
            supersedes_run_id=int(run_a["run_id"]),
            select_as_review_target=False,
        )
        ws.new_cluster_prepare_service().prepare_run(run_id=int(run_a["run_id"]))
        ws.new_run_activation_service().activate_run(run_id=int(run_a["run_id"]))

        client = TestClient(create_app(workspace=ws.root))
        response = client.get("/identity-tuning")
        payload = _extract_payload(response.text)

        assert response.status_code == 200
        assert int(payload["review_run"]["id"]) == int(run_a["run_id"])
        assert int(run_b["run_id"]) > int(run_a["run_id"])
        assert int(ws.get_cluster_run(int(run_a["run_id"]))["cluster_profile_id"]) != int(
            ws.get_cluster_run(int(run_b["run_id"]))["cluster_profile_id"]
        )
        assert bool(payload["review_run"]["is_materialization_owner"]) is True
        assert payload["clusters"]
    finally:
        ws.close()
```

- [ ] **Step 2: 运行测试并确认失败**

Run: `source .venv/bin/activate && PYTHONPATH=src python3 -m pytest tests/people_gallery/test_identity_threshold_evaluation_script.py tests/people_gallery/test_rebuild_identities_v3_script.py tests/people_gallery/test_identity_cluster_phase1_e2e.py tests/test_repo_samples.py -q`
Expected: FAIL，README 仍是旧 phase1 指令，`evaluate_identity_thresholds.py` 还在执行旧逻辑，wrapper/e2e 验收尚未对齐（包括“同一 snapshot + 不同 cluster profile”场景）。

- [ ] **Step 3: 更新 README、添加弃用提示、补 Playwright 调试脚本并清理旧 v3-only 测试**

```python
# scripts/evaluate_identity_thresholds.py
def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    parser.parse_args(argv)
    print(
        "identity 阈值评估脚本已弃用；请改用 build_identity_observation_snapshot.py + "
        "rerun_identity_cluster_run.py + /identity-tuning + export_observation_neighbors.py",
        file=sys.stderr,
    )
    return 2
```

```python
# src/hikbox_pictures/services/identity_threshold_evaluation_service.py
class IdentityThresholdEvaluationService:
    def __init__(self, workspace: Path) -> None:
        self.workspace = Path(workspace).expanduser().resolve()

    def evaluate(self) -> dict[str, Any]:
        raise RuntimeError(
            "v3.1 Phase 1 已改为 snapshot + rerun + review 流程；不再支持 evaluate_identity_thresholds。"
        )
```

```python
# tools/identity_tuning_playwright_check.py
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="用 Playwright 检查 /identity-tuning 只读 review 页面。")
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--run-id", type=int, default=None)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--runner-dir", type=Path, default=None)
    parser.add_argument("--install-browser", action="store_true")
    return parser
```

```javascript
// tools/identity_tuning_playwright_capture.cjs
const { webkit } = require("playwright");

(async () => {
  const targetUrl = process.argv[2];
  const screenshotPath = process.argv[3];
  const browser = await webkit.launch({ headless: true });
  const page = await browser.newPage({ viewport: { width: 1440, height: 1024 } });
  await page.goto(targetUrl, { waitUntil: "networkidle" });
  await page.screenshot({ path: screenshotPath, fullPage: true });
  await browser.close();
})();
```

~~~~md
## v3.1 第一阶段：cluster bootstrap rerun + review（phase1）

```bash
source .venv/bin/activate
PYTHONPATH=src python3 scripts/build_identity_observation_snapshot.py --workspace <workspace>
PYTHONPATH=src python3 scripts/rerun_identity_cluster_run.py --workspace <workspace> --snapshot-id <snapshot_id>
PYTHONPATH=src python3 scripts/select_identity_cluster_run.py --workspace <workspace> --run-id <run_id>
PYTHONPATH=src python3 scripts/export_observation_neighbors.py --workspace <workspace> --run-id <run_id> --cluster-id <cluster_id>
PYTHONPATH=src python3 scripts/activate_identity_cluster_run.py --workspace <workspace> --run-id <run_id>
```

- 默认 review 页入口：`/identity-tuning`
- 默认 review 对象：`is_review_target = 1`
- live 物化结果 owner：`is_materialization_owner = 1`
- `scripts/evaluate_identity_thresholds.py` 已弃用
~~~~

- [ ] **Step 4: 回跑文档/脚本/e2e 测试，并做一次 Playwright 调试验收**

Run: `source .venv/bin/activate && PYTHONPATH=src python3 -m pytest tests/people_gallery/test_identity_threshold_evaluation_script.py tests/people_gallery/test_rebuild_identities_v3_script.py tests/people_gallery/test_identity_cluster_phase1_e2e.py tests/test_repo_samples.py -q`
Expected: PASS，README 指令收敛到 snapshot/rerun/select/activate 流程，旧评估脚本稳定返回弃用提示，且 e2e 覆盖“同一 snapshot + 不同 cluster profile”双 run 场景。

Run: `source .venv/bin/activate && PYTHONPATH=src python3 tools/identity_tuning_playwright_check.py --workspace <workspace> --run-id <run_id> --output-dir .tmp/identity-tuning-playwright/report --install-browser`
Expected: 在 `.tmp/identity-tuning-playwright/report/` 下生成截图、JSON 报告和本地服务日志，且报告内记录本次检查使用的 `run_id` 与最终请求 URL。

**Task completion action (not a checkbox step): Commit task changes and plan progress**

```bash
git add README.md scripts/evaluate_identity_thresholds.py src/hikbox_pictures/services/identity_threshold_evaluation_service.py tests/people_gallery/test_identity_threshold_evaluation_script.py tests/people_gallery/test_rebuild_identities_v3_script.py tools/identity_tuning_playwright_check.py tools/identity_tuning_playwright_capture.cjs tests/people_gallery/test_identity_cluster_phase1_e2e.py tests/test_repo_samples.py docs/superpowers/plans/2026-04-17-hikbox-pictures-v3-1-cluster-bootstrap-phase1.md
git rm tests/people_gallery/test_identity_bootstrap_service.py tests/people_gallery/test_identity_threshold_profile_contract.py tests/people_gallery/test_identity_rebuild_v3_real_pipeline.py
git commit -m "docs: finalize v3.1 phase1 bootstrap workflow and cleanup legacy tests (Task 9)"
```
