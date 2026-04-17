# HikBox Pictures v3.1 Phase 2 身份系统收口 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax. This plan's checkbox state is the persistent progress source of truth; TodoWrite is session-local tracking. Executors may run dependency-free tasks in parallel (default max concurrency: 4).

**Goal:** 在 phase1 owner run / cluster 真相之上，交付 phase2 可长期运行的身份系统：runtime profile、scan 增量发现、review 三类动作、people 四类动作、export、WebUI、API、CLI、README 与验收矩阵全部统一到同一套运行时语义，且无 mock/占位成功路径。

**Architecture:** 采用“同版本一次切断迁移 + runtime profile 独立生命周期 + `scan_session` 驱动 incremental snapshot/run + 显式动作写路径 + ANN 双阶段发布与恢复 + 动作后即时 scoped regeneration”的结构。phase2 仅消费 phase1 不可变真相，不再保留 `possible_split/split/resolve/dismiss/lock-assignment` 过渡语义。

**Tech Stack:** Python 3.12、SQLite、FastAPI/Jinja2、pytest、Playwright（桌面 Safari 近似）、NumPy、`AnnIndexStore`

---

## 范围边界

- 本计划仅覆盖 phase2，默认 phase1 的 observation/cluster/run 契约已经可用。
- 本计划不实现“已有 phase2 live delta 后重新切 owner run 并自动 reconciliation”；仅实现 `activate-run` 护栏硬拒绝。
- 所有临时产物必须写入 `.tmp/<task-name>/`。
- 所有 schema 变更必须同步更新 `docs/db_schema/README.md`。

## 防占位实现硬门（全任务强制）

1. 每个任务至少一条 DB 真值断言；禁止仅靠 HTTP 200、CLI 退出码、页面可打开作为通过条件。
2. 所有日常 decision 仅允许读取 active `identity_runtime_profile`；禁止回退旧 `identity_threshold_profile` 日常字段。
3. 禁止写入或暴露旧语义：`possible_split`、`dismissed`、`split`、`lock-assignment`、`resolve/dismiss`。
4. `identity_cluster_run.run_kind='incremental'` 必须 DB 级保证 `is_review_target=0` 且 `is_materialization_owner=0`。
5. `review_item.status IN ('resolved','ignored')` 时 `resolution_action` 必须非空。
6. `activate-run` 护栏必须在 run 激活链路生效；检测到 phase2 delta 必须返回 `phase2_delta_present`。
7. 所有会影响 prototype/ANN 的写动作必须执行 `prepare -> switch -> epoch commit`；失败写 `ann_publish_state='failed'` + `ann_last_error`。
8. `ann_publish_state='failed'` 时所有身份写路径（scan assignment、review、people）必须 fail-closed，拒绝继续写 DB 真相。
9. 动作提交成功后必须立即执行 scoped regeneration（同请求链路内或紧随其后的同步步骤），不能依赖离线 CLI。
10. `resolution_state='materialized'` 必须与 `publish_state='published'`、`published_at` 非空耦合，禁止“已 materialized 但未 published”。
11. runtime profile `activate` 在 `scan_session.status='running'` 时必须拒绝并返回 `scan_session_running`。
12. Task 5/6/7 的身份写路径必须显式使用 `BEGIN IMMEDIATE + CAS`；CAS 未命中时仅允许返回“幂等成功”或“并发冲突”，禁止盲写覆盖。

## Runtime Profile 字段级契约（Task 2 必须逐项落地）

最小字段全集（必须存在并在 create/validate 覆盖）：
- `id`
- `profile_name`
- `profile_version`
- `source_materialization_owner_run_id`
- `embedding_feature_type`
- `embedding_model_key`
- `embedding_distance_metric`
- `embedding_schema_version`
- `assignment_recall_top_k`
- `assignment_auto_min_quality`
- `assignment_auto_max_distance`
- `assignment_auto_min_margin`
- `assignment_review_max_distance`
- `assignment_require_photo_conflict_free`
- `trusted_min_quality`
- `trusted_centroid_max_distance`
- `trusted_min_margin`
- `trusted_block_exact_duplicate`
- `trusted_block_burst_duplicate`
- `burst_window_seconds`
- `manual_confirm_bootstrap_min_samples`
- `manual_confirm_bootstrap_min_photos`
- `possible_merge_max_distance`
- `possible_merge_min_margin`
- `possible_merge_min_trusted_sample_count`
- `active`
- `created_at`
- `activated_at`

跨字段约束（必须在 validate + DB 断言双重覆盖）：
- embedding 绑定必须与 `source_materialization_owner_run_id` 对应 owner run 的 embedding 空间完全一致。
- `assignment_auto_max_distance <= assignment_review_max_distance`。
- `assignment_recall_top_k >= 1`。
- 所有 quality/distance/margin 阈值必须非负，且 quality 阈值在 `[0,1]`。
- `manual_confirm_bootstrap_min_samples >= 1`。
- `manual_confirm_bootstrap_min_photos >= 1`。
- 任一时刻仅一个 `active=1`。

## `activate-run` Phase2 Delta 判定（Task 4 必须落地）

run 激活链路必须检查以下三类 live delta（最小判定集合）：
1. `person_cluster_origin.origin_kind IN ('incremental_materialize', 'review_materialize', 'review_adopt', 'merge_adopt')` 的 active 记录存在。
2. `person_face_assignment.assignment_source IN ('incremental', 'manual', 'merge')` 的 active 记录存在。
3. `person_trusted_sample.trust_source IN ('incremental_seed', 'review_seed', 'manual_confirm')` 的 active 记录存在。

命中任一类时：
- `activate-run` 必须硬拒绝，返回 `phase2_delta_present`。
- 不得修改 `is_materialization_owner`。
- 记录审计事件用于回放。

## Margin 统一口径（Task 5/6/7）

- direct auto / low confidence：缺 `top2` 时 `margin=+inf`，并写 `second_candidate_missing=true`。
- manual_confirm 严格门：缺 second person 时 `margin=+inf`，并写 `second_person_missing=true`。
- possible_merge 生成门：任一侧缺 second neighbor 时该侧 margin=`+inf`，并写 `second_neighbor_missing=true`。

## ANN Fail-Closed 与恢复口径（Task 6）

- 写路径前统一检查 `identity_artifact_state.ann_publish_state`。
- `failed`：直接拒绝写路径，返回显式错误，不得“先写 DB、后补 ANN”。
- `preparing`：启动恢复逻辑。
  - 分支 A（一致）：live ANN 与 pending manifest 在 `epoch/model_key/embedding_dimension/index_sha256` 四字段一致，补事务 B 提交 epoch 并回 `idle`。
  - 分支 B（不一致）：标记 `failed` 并拒绝写路径，等待显式修复。

## `resolution_reason` 契约（Task 1 + Task 6）

最小枚举集合（schema CHECK 与动作断言同时覆盖）：
- `incremental_materialized`
- `review_created_person`
- `review_adopted_into_person`
- `review_ignored`
- `discarded_by_system`

写入规则：
- incremental auto materialize 成功：写 `resolution_reason='incremental_materialized'`。
- `new_person create-person`：写 `resolution_reason='review_created_person'`。
- `new_person assign-person`：写 `resolution_reason='review_adopted_into_person'`。
- review `ignore`：写 `resolution_reason='review_ignored'`。
- 系统在成团阶段丢弃：写 `resolution_reason='discarded_by_system'`（仅系统路径可写）。

## 文件结构设计

### 数据与迁移

- Create: `src/hikbox_pictures/db/migrations/0006_identity_phase2_runtime_system.sql`
- Modify: `src/hikbox_pictures/db/migrator.py`
- Modify: `docs/db_schema/README.md`
- Create: `tools/build_identity_v3_phase2_fixture.py`
- Create: `tests/data/identity-v3-phase2-small.db`

### 仓储层

- Create: `src/hikbox_pictures/repositories/runtime_profile_repo.py`
- Create: `src/hikbox_pictures/repositories/artifact_state_repo.py`
- Create: `src/hikbox_pictures/repositories/run_activation_guard_repo.py`
- Modify: `src/hikbox_pictures/repositories/scan_repo.py`
- Modify: `src/hikbox_pictures/repositories/review_repo.py`
- Modify: `src/hikbox_pictures/repositories/person_repo.py`
- Modify: `src/hikbox_pictures/repositories/export_repo.py`
- Modify: `src/hikbox_pictures/repositories/identity_repo.py`
- Modify: `src/hikbox_pictures/repositories/__init__.py`

### 服务层

- Create: `src/hikbox_pictures/services/runtime_profile_service.py`
- Create: `src/hikbox_pictures/services/incremental_scan_closure_service.py`
- Create: `src/hikbox_pictures/services/runtime_assignment_decision_service.py`
- Create: `src/hikbox_pictures/services/incremental_cluster_run_service.py`
- Create: `src/hikbox_pictures/services/review_action_service.py`
- Create: `src/hikbox_pictures/services/review_regeneration_service.py`
- Create: `src/hikbox_pictures/services/ann_publish_state_service.py`
- Create: `src/hikbox_pictures/services/run_activation_guard_service.py`
- Modify: `src/hikbox_pictures/services/scan_execution_service.py`
- Modify: `src/hikbox_pictures/services/scan_orchestrator.py`
- Modify: `src/hikbox_pictures/services/scan_recovery.py`
- Modify: `src/hikbox_pictures/services/asset_stage_runner.py`
- Modify: `src/hikbox_pictures/services/ann_assignment_service.py`
- Modify: `src/hikbox_pictures/services/review_workflow_service.py`
- Modify: `src/hikbox_pictures/services/person_truth_service.py`
- Modify: `src/hikbox_pictures/services/prototype_service.py`
- Modify: `src/hikbox_pictures/services/action_service.py`
- Modify: `src/hikbox_pictures/services/web_query_service.py`
- Modify: `src/hikbox_pictures/services/runtime.py`

### API / CLI / WebUI

- Create: `src/hikbox_pictures/api/routes_runtime_profiles.py`
- Modify: `src/hikbox_pictures/api/app.py`
- Modify: `src/hikbox_pictures/api/routes_scan.py`
- Modify: `src/hikbox_pictures/api/routes_reviews.py`
- Modify: `src/hikbox_pictures/api/routes_people.py`
- Modify: `src/hikbox_pictures/api/routes_export.py`
- Modify: `src/hikbox_pictures/api/routes_web.py`
- Modify: `src/hikbox_pictures/cli.py`
- Modify: `src/hikbox_pictures/web/templates/review_queue.html`
- Modify: `src/hikbox_pictures/web/templates/people.html`
- Modify: `src/hikbox_pictures/web/templates/person_detail.html`
- Modify: `src/hikbox_pictures/web/templates/export_templates.html`
- Modify: `src/hikbox_pictures/web/static/app.js`
- Modify: `src/hikbox_pictures/web/static/style.css`

### 测试与文档

- Create: `tests/people_gallery/test_identity_phase2_schema_cutover.py`
- Create: `tests/people_gallery/test_runtime_profile_lifecycle.py`
- Create: `tests/people_gallery/test_runtime_profile_api.py`
- Create: `tests/people_gallery/test_runtime_profile_cli.py`
- Create: `tests/people_gallery/test_scan_incremental_snapshot_run_contract.py`
- Create: `tests/people_gallery/test_scan_single_running_session_guard.py`
- Create: `tests/people_gallery/test_activate_run_phase2_guard.py`
- Create: `tests/people_gallery/test_phase2_assignment_decision_pipeline.py`
- Create: `tests/people_gallery/test_incremental_run_resolution_states.py`
- Create: `tests/people_gallery/test_new_person_review_cluster_backed.py`
- Create: `tests/people_gallery/test_review_actions_phase2_contract.py`
- Create: `tests/people_gallery/test_review_actions_idempotency.py`
- Create: `tests/people_gallery/test_possible_merge_gate_and_resolution.py`
- Create: `tests/people_gallery/test_ann_publish_recovery.py`
- Create: `tests/people_gallery/test_people_phase2_actions_contract.py`
- Create: `tests/people_gallery/test_confirm_assignments_bootstrap_gate.py`
- Create: `tests/people_gallery/test_exclude_assignments_requeue.py`
- Create: `tests/people_gallery/test_merge_export_template_rewrite.py`
- Create: `tests/people_gallery/test_rebuild_artifacts_contract.py`
- Create: `tests/people_gallery/test_review_regeneration_service.py`
- Create: `tests/people_gallery/test_reviews_regenerate_cli.py`
- Create: `tests/people_gallery/test_review_fingerprint_reentry_control.py`
- Create: `tests/people_gallery/test_phase2_acceptance_matrix.py`
- Modify: `tests/people_gallery/fixtures_workspace.py`
- Modify: `tests/people_gallery/test_api_contract.py`
- Modify: `tests/people_gallery/test_review_actions_contract.py`
- Modify: `tests/people_gallery/test_person_truth_actions.py`
- Modify: `tests/people_gallery/test_webui_content.py`
- Modify: `tests/people_gallery/test_webui_actions_e2e.py`
- Modify: `tests/people_gallery/test_export_matching_and_ledger.py`
- Modify: `tests/people_gallery/test_cli_control_plane.py`
- Modify: `tests/people_gallery/test_web_navigation.py`
- Modify: `README.md`

## Parallel Execution Plan

### Wave A（基础切断）

- 可并行任务：无
- 执行任务：`Task 1`
- 阻塞任务：`Task 2`~`Task 11`
- 解锁条件：`Task 1` 通过。

### Wave B（并行：runtime profile 与 scan closure）

- 可并行任务：`Task 2`、`Task 3`
- 可并行原因：
  - 依赖均只需 `Task 1`。
  - 写集合不冲突：`Task 2` 聚焦 runtime profile；`Task 3` 聚焦 scan/incremental closure。
- 阻塞任务：`Task 4`~`Task 11`
- 解锁条件：`Task 2` 和 `Task 3` 均完成。

### Wave C（并行：run 激活护栏 与 assignment 决策）

- 可并行任务：`Task 4`、`Task 5`
- 可并行原因：
  - `Task 4` 聚焦 run 激活链路护栏；`Task 5` 聚焦 observation 决策与 low-confidence 契约。
  - 写集合不冲突。
- 阻塞任务：`Task 6`~`Task 11`
- 解锁条件：`Task 4` 和 `Task 5` 均完成。

### Wave D（核心动作引擎）

- 可并行任务：无
- 执行任务：`Task 6`
- 阻塞任务：`Task 7`~`Task 11`
- 解锁条件：`Task 6` 完成。

### Wave E（顺序：people 动作 -> regenerate 入口）

- 可并行任务：无
- 顺序任务：`Task 7` -> `Task 8`
- 顺序原因：
  - `Task 7` 与 `Task 8` 共享写文件（`src/hikbox_pictures/services/review_regeneration_service.py`），并行会产生写冲突。
  - `Task 7` 先落地在线即时 scoped regeneration；`Task 8` 再补离线运维 CLI，避免语义回退。
- 阻塞任务：`Task 9`、`Task 10`、`Task 11`
- 解锁条件：`Task 8` 完成。

### Wave F（artifact 专项）

- 可并行任务：无
- 执行任务：`Task 9`
- 阻塞任务：`Task 10`、`Task 11`
- 解锁条件：`Task 9` 完成。

### Wave G（产品面收口）

- 可并行任务：无
- 执行任务：`Task 10`
- 阻塞任务：`Task 11`
- 解锁条件：`Task 10` 完成。

### Wave H（README 与验收矩阵）

- 可并行任务：无
- 执行任务：`Task 11`

## 验收矩阵映射（A01-A25）

| 验收ID | 需求摘要 | 责任任务 | 关键验证 |
| --- | --- | --- | --- |
| A01 | scan 完成 quality/direct-auto/low-confidence/candidate | Task 3/5 | assignment + review + candidate 状态一致 |
| A02 | 扫描收口生成 incremental snapshot/run | Task 3 | `snapshot_kind`/`run_kind`/`trigger_scan_session_id`/`base_owner_run_id` |
| A03 | review_pending 生成 cluster-backed new_person | Task 6 | `review_item.cluster_id` 非空且每 cluster 仅一条 open |
| A04 | low-confidence confirm 必须传 `target_person_id` | Task 6 | 缺参失败且 DB 无副作用 |
| A05 | create-person 写 review_seed/prototype/ANN/origin | Task 6 | trusted/prototype/origin/ann_epoch 同步变化 |
| A06 | assign-person 写 adopted 且 publish_state=not_applicable | Task 6 | cluster resolution 精确匹配 |
| A07 | confirm-assignments 支持部分成功与逐条原因 | Task 7 | 同批 observation 成功/失败混合 |
| A08 | exclude 失活 assignment/trusted 并回候选池 | Task 7 | `active=0` + candidate 重入 |
| A09 | merge 迁移 assignment/trusted/export 且审计齐全 | Task 7 | `merge_from_person_id/source_review_id` 可追溯 |
| A10 | 页面/API 无 possible_split 与 split/lock | Task 1/10 | 路由与 UI 无旧入口 |
| A11 | ignore 不再落 dismissed | Task 1/6 | `status='ignored'` 且旧枚举不可写 |
| A12 | rebuild-artifacts 仅影响 prototype/ANN | Task 9 | assignment/review/cluster 行数不变 |
| A13 | materialized 必须 published | Task 1/6 | materialized 行均有 published + published_at |
| A14 | review 幂等 + ANN 不损坏 | Task 6 | 重放请求不重复建人/样本；epoch 一致 |
| A15 | 无 prototype 人物可通过启动门恢复 trusted | Task 7 | 达标后最小启动子集入池 |
| A16 | keep-separate 后同指纹不重复 open | Task 6/8 | same fingerprint 不再出队 |
| A17 | 旧写路径在 API/WebUI/README/测试全部退场 | Task 1/10/11 | grep + 契约测试双重通过 |
| A18 | phase2 delta 存在时 activate-run 硬拒绝 | Task 4 | 返回 `phase2_delta_present`，owner 不变 |
| A19 | ANN preparing 恢复双分支正确 | Task 6 | 一致分支按 `epoch/model_key/embedding_dimension/index_sha256` 比对后补事务B；不一致分支转 failed |
| A20 | resolved/ignored 必有 resolution_action | Task 1/6 | 非法插入失败 |
| A21 | incremental 判定复用 phase1 gate | Task 6 | gate 指标回放一致 |
| A22 | candidate 池空时 no-op 且无脏 snapshot/run | Task 3 | no-op 事件存在，snapshot/run 数不增长 |
| A23 | second running scan 被拒绝 | Task 3 | running 始终 <= 1 |
| A24 | assign-person 后 publish_state=not_applicable | Task 6 | adopted 行 publish_state 精确值 |
| A25 | runtime profile 管理入口可用，且 scan running 时禁止激活 | Task 2 | API + CLI + 字段域校验 + `scan_session_running` 断言通过 |

---

### Task 1: 一次切断迁移与 DB 级契约加固

**Depends on:** None

**Scope Budget:**
- Max files: 20
- Estimated files touched: 11
- Max added lines: 1000
- Estimated added lines: 980

**Files:**
- Create: `src/hikbox_pictures/db/migrations/0006_identity_phase2_runtime_system.sql`
- Modify: `src/hikbox_pictures/db/migrator.py`
- Modify: `docs/db_schema/README.md`
- Create: `tools/build_identity_v3_phase2_fixture.py`
- Create: `tests/data/identity-v3-phase2-small.db`
- Create: `tests/people_gallery/test_identity_phase2_schema_cutover.py`
- Modify: `tests/people_gallery/fixtures_workspace.py`
- Modify: `tests/people_gallery/test_identity_v3_schema_migration.py`
- Modify: `tests/people_gallery/test_review_actions_contract.py`
- Modify: `tests/people_gallery/test_api_contract.py`
- Modify: `tests/people_gallery/test_repository_contract.py`

- [ ] **Step 1: 写失败测试，锁定 DB 级强约束与非法插入失败**
  - 在 `test_identity_phase2_schema_cutover.py` 覆盖以下失败断言：
    - `review_item` 分型 CHECK：
      - `new_person` 必须 `cluster_id` 非空。
      - `low_confidence_assignment` 必须 `face_observation_id` 非空。
      - `possible_merge` 必须 `pair_person_low_id/pair_person_high_id` 非空且 `low < high`。
    - `pair` 互斥 CHECK：非 `possible_merge` 行 `pair_person_low_id/pair_person_high_id` 必须同时为 NULL。
    - open 唯一索引：
      - `new_person` 每 `cluster_id` 最多一条 open。
      - `low_confidence_assignment` 每 `face_observation_id` 最多一条 open。
      - `possible_merge` 每 `(pair_low,pair_high)` 最多一条 open。
    - `resolution_state` 与 `publish_state` 耦合 CHECK：
      - `resolution_state!='materialized'` 时 `publish_state='not_applicable'`。
      - `resolution_state='materialized'` 时 `publish_state IN ('prepared','published','publish_failed')`。
    - `resolution_reason` 枚举 CHECK 仅允许：
      - `incremental_materialized`
      - `review_created_person`
      - `review_adopted_into_person`
      - `review_ignored`
      - `discarded_by_system`
    - 非法插入断言：尝试写 `review_type='possible_split'`、`status='dismissed'`、`assignment_source='split'`、`resolution_state='unresolved'` 必须失败。
    - 非法插入断言：尝试写未定义 `resolution_reason`（如 `manual_override`）必须失败。

- [ ] **Step 2: 运行失败测试**
  - Run: `source .venv/bin/activate && PYTHONPATH=src python3 -m pytest tests/people_gallery/test_identity_phase2_schema_cutover.py -v`
  - Expected: FAIL，失败点至少命中一个 CHECK 或唯一索引缺失。

- [ ] **Step 3: 实现 migration（expand -> backfill -> contract）**
  - expand：新增 `identity_runtime_profile` 字段、`review_item` 新字段、`identity_artifact_state` ANN 字段、`identity_cluster_run.run_kind`、`identity_observation_snapshot.snapshot_kind/trigger_scan_session_id`。
  - expand：为 `person_trusted_sample` 新增 `runtime_profile_id/source_run_id/source_cluster_id/merge_from_person_id/source_review_id`。
  - backfill：`possible_split` 收敛、`dismissed->ignored`、`unresolved->review_pending/discarded`，补齐 `pair_*`、`evidence_fingerprint`、`resolution_action`。
  - contract：收紧 CHECK/NOT NULL/枚举，固化 run_kind 与 owner/review_target 的 DB 约束；补齐 `resolution_reason` CHECK 与状态写入规则约束。

- [ ] **Step 4: 运行通过测试并验证 DB 真值**
  - Run: `source .venv/bin/activate && PYTHONPATH=src python3 -m pytest tests/people_gallery/test_identity_phase2_schema_cutover.py tests/people_gallery/test_identity_v3_schema_migration.py tests/people_gallery/test_repository_contract.py -v`
  - Expected: PASS，且断言：
    - `SELECT COUNT(*) FROM review_item WHERE review_type='possible_split' = 0`
    - `SELECT COUNT(*) FROM review_item WHERE status='dismissed' = 0`
    - `SELECT COUNT(*) FROM identity_cluster_resolution WHERE resolution_state='materialized' AND publish_state='not_applicable' = 0`
    - `SELECT COUNT(*) FROM identity_cluster_resolution WHERE resolution_reason NOT IN ('incremental_materialized','review_created_person','review_adopted_into_person','review_ignored','discarded_by_system') = 0`
    - 非法插入 `trust_source='merge_seed'` 失败（仅允许 `bootstrap_seed/incremental_seed/review_seed/manual_confirm`）

**Task completion action (not a checkbox step): Commit task changes and plan progress**

```bash
git add src/hikbox_pictures/db/migrations/0006_identity_phase2_runtime_system.sql src/hikbox_pictures/db/migrator.py docs/db_schema/README.md tools/build_identity_v3_phase2_fixture.py tests/data/identity-v3-phase2-small.db tests/people_gallery/test_identity_phase2_schema_cutover.py tests/people_gallery/fixtures_workspace.py tests/people_gallery/test_identity_v3_schema_migration.py tests/people_gallery/test_review_actions_contract.py tests/people_gallery/test_api_contract.py tests/people_gallery/test_repository_contract.py docs/superpowers/plans/2026-04-17-hikbox-pictures-v3-1-phase2-identity-system.md
git commit -m "feat: enforce phase2 db contracts and cutover migration (Task 1)"
```

### Task 2: Runtime Profile 生命周期与字段级校验（API + CLI）

**Depends on:** Task 1

**Scope Budget:**
- Max files: 20
- Estimated files touched: 10
- Max added lines: 1000
- Estimated added lines: 900

**Files:**
- Create: `src/hikbox_pictures/repositories/runtime_profile_repo.py`
- Create: `src/hikbox_pictures/services/runtime_profile_service.py`
- Create: `src/hikbox_pictures/api/routes_runtime_profiles.py`
- Modify: `src/hikbox_pictures/api/app.py`
- Modify: `src/hikbox_pictures/cli.py`
- Modify: `src/hikbox_pictures/repositories/__init__.py`
- Create: `tests/people_gallery/test_runtime_profile_lifecycle.py`
- Create: `tests/people_gallery/test_runtime_profile_api.py`
- Create: `tests/people_gallery/test_runtime_profile_cli.py`
- Modify: `tests/people_gallery/conftest.py`

- [ ] **Step 1: 写失败测试，逐项锁定字段与跨字段约束**
  - 对“字段全集”逐项断言 create/list/get 可见。
  - 对跨字段约束逐项断言 validate 失败：
    - embedding 绑定与 owner run 不一致。
    - `assignment_auto_max_distance > assignment_review_max_distance`。
    - `assignment_recall_top_k < 1`。
    - 负数阈值或 quality 超出 `[0,1]`。
    - `manual_confirm_bootstrap_min_samples < 1` 或 `manual_confirm_bootstrap_min_photos < 1`。
  - 激活原子性断言：激活后始终只有一个 active profile。
  - 激活拒绝断言：存在 `scan_session.status='running'` 时返回 `scan_session_running`。

- [ ] **Step 2: 运行失败测试**
  - Run: `source .venv/bin/activate && PYTHONPATH=src python3 -m pytest tests/people_gallery/test_runtime_profile_lifecycle.py tests/people_gallery/test_runtime_profile_api.py tests/people_gallery/test_runtime_profile_cli.py -v`
  - Expected: FAIL，至少命中字段缺失或校验未生效。

- [ ] **Step 3: 实现 runtime profile 仓储/服务/API/CLI**
  - 实现 `create/validate/activate/list/get`。
  - `activate` 仅负责 runtime profile 切换，不承载 `activate-run` delta 护栏。
  - API：`GET/POST /api/runtime-profiles...`；CLI：`runtime-profile list/create/validate/activate`。

- [ ] **Step 4: 运行通过测试并验证 DB 真值**
  - Run: `source .venv/bin/activate && PYTHONPATH=src python3 -m pytest tests/people_gallery/test_runtime_profile_lifecycle.py tests/people_gallery/test_runtime_profile_api.py tests/people_gallery/test_runtime_profile_cli.py tests/people_gallery/test_cli_control_plane.py -v`
  - Expected: PASS，且断言：
    - `SELECT COUNT(*) FROM identity_runtime_profile WHERE active=1 = 1`
    - active profile 的 `source_materialization_owner_run_id` 与 owner run 一致
    - 非法阈值组合写入失败
    - running scan 场景下 activate 返回 `scan_session_running` 且 active profile 不变化

**Task completion action (not a checkbox step): Commit task changes and plan progress**

```bash
git add src/hikbox_pictures/repositories/runtime_profile_repo.py src/hikbox_pictures/services/runtime_profile_service.py src/hikbox_pictures/api/routes_runtime_profiles.py src/hikbox_pictures/api/app.py src/hikbox_pictures/cli.py src/hikbox_pictures/repositories/__init__.py tests/people_gallery/test_runtime_profile_lifecycle.py tests/people_gallery/test_runtime_profile_api.py tests/people_gallery/test_runtime_profile_cli.py tests/people_gallery/conftest.py docs/superpowers/plans/2026-04-17-hikbox-pictures-v3-1-phase2-identity-system.md
git commit -m "feat: add runtime profile field-level lifecycle validation (Task 2)"
```

### Task 3: `scan_session` 生命周期与 incremental snapshot/run 契约

**Depends on:** Task 1

**Scope Budget:**
- Max files: 20
- Estimated files touched: 10
- Max added lines: 1000
- Estimated added lines: 940

**Files:**
- Create: `src/hikbox_pictures/services/incremental_scan_closure_service.py`
- Modify: `src/hikbox_pictures/services/scan_orchestrator.py`
- Modify: `src/hikbox_pictures/services/scan_execution_service.py`
- Modify: `src/hikbox_pictures/services/scan_recovery.py`
- Modify: `src/hikbox_pictures/repositories/scan_repo.py`
- Modify: `src/hikbox_pictures/repositories/identity_repo.py`
- Create: `tests/people_gallery/test_scan_incremental_snapshot_run_contract.py`
- Create: `tests/people_gallery/test_scan_single_running_session_guard.py`
- Modify: `tests/people_gallery/test_scan_resume_semantics.py`
- Modify: `tests/people_gallery/test_scan_abort_and_restart.py`

- [ ] **Step 1: 写失败测试，锁定会话边界与 no-op 行为**
  - 覆盖断言：
    - 同时仅一个 running scan。
    - 仅终态会话可触发 incremental snapshot/run。
    - snapshot 写 `snapshot_kind='incremental'`、`trigger_scan_session_id`。
    - run 写 `run_kind='incremental'`、`trigger_scan_session_id`、`base_owner_run_id`，且 owner/review_target 标志为 0。
    - `identity_cluster_run.trigger_scan_session_id` 一经写入不可回填修改（后续 UPDATE 必须失败或 rowcount=0）。
    - candidate 池为空时写 no-op 事件，不创建 snapshot/run。

- [ ] **Step 2: 运行失败测试**
  - Run: `source .venv/bin/activate && PYTHONPATH=src python3 -m pytest tests/people_gallery/test_scan_incremental_snapshot_run_contract.py tests/people_gallery/test_scan_single_running_session_guard.py -v`
  - Expected: FAIL，至少命中字段写入或状态机不满足。

- [ ] **Step 3: 实现 closure 与恢复收敛逻辑**
  - incremental snapshot/run 统一走 `incremental_scan_closure_service`。
  - 进程重启先收敛陈旧 running 会话为 interrupted，再恢复或放弃。
  - run 入库时同步写 `trigger_scan_session_id`，并在仓储层禁止后续回填更新。

- [ ] **Step 4: 运行通过测试并验证 DB 真值**
  - Run: `source .venv/bin/activate && PYTHONPATH=src python3 -m pytest tests/people_gallery/test_scan_incremental_snapshot_run_contract.py tests/people_gallery/test_scan_single_running_session_guard.py tests/people_gallery/test_scan_resume_semantics.py tests/people_gallery/test_scan_abort_and_restart.py -v`
  - Expected: PASS，且断言：
    - `SELECT COUNT(*) FROM scan_session WHERE status='running' <= 1`
    - snapshot 的 `trigger_scan_session_id` 与触发会话一致
    - run 的 `trigger_scan_session_id` 与触发会话一致
    - 对既有 run 执行 `UPDATE identity_cluster_run SET trigger_scan_session_id=?` 不生效（失败或 rowcount=0）
    - candidate 空集时 snapshot/run 数不增长

**Task completion action (not a checkbox step): Commit task changes and plan progress**

```bash
git add src/hikbox_pictures/services/incremental_scan_closure_service.py src/hikbox_pictures/services/scan_orchestrator.py src/hikbox_pictures/services/scan_execution_service.py src/hikbox_pictures/services/scan_recovery.py src/hikbox_pictures/repositories/scan_repo.py src/hikbox_pictures/repositories/identity_repo.py tests/people_gallery/test_scan_incremental_snapshot_run_contract.py tests/people_gallery/test_scan_single_running_session_guard.py tests/people_gallery/test_scan_resume_semantics.py tests/people_gallery/test_scan_abort_and_restart.py docs/superpowers/plans/2026-04-17-hikbox-pictures-v3-1-phase2-identity-system.md
git commit -m "feat: enforce scan session contract for incremental closure (Task 3)"
```

### Task 4: `activate-run` 护栏落地到 run 激活链路

**Depends on:** Task 1

**Scope Budget:**
- Max files: 20
- Estimated files touched: 9
- Max added lines: 1000
- Estimated added lines: 760

**Files:**
- Create: `src/hikbox_pictures/repositories/run_activation_guard_repo.py`
- Create: `src/hikbox_pictures/services/run_activation_guard_service.py`
- Modify: `src/hikbox_pictures/services/identity_bootstrap_service.py`
- Modify: `src/hikbox_pictures/services/action_service.py`
- Modify: `src/hikbox_pictures/cli.py`
- Modify: `src/hikbox_pictures/api/routes_web.py`
- Create: `tests/people_gallery/test_activate_run_phase2_guard.py`
- Modify: `tests/people_gallery/test_cli_control_plane.py`
- Modify: `tests/people_gallery/test_web_identity_tuning_page.py`

- [ ] **Step 1: 写失败测试，固定护栏必须在 activate-run 生效**
  - 构造三类 delta 各自独立存在场景：
    - `person_cluster_origin.origin_kind` 命中。
    - `person_face_assignment.assignment_source` 命中。
    - `person_trusted_sample.trust_source` 命中。
  - 对每个场景执行 `activate-run`，断言返回 `phase2_delta_present`。
  - 对照断言：runtime profile activate 不受该护栏影响（仅做 profile 切换）。

- [ ] **Step 2: 运行失败测试**
  - Run: `source .venv/bin/activate && PYTHONPATH=src python3 -m pytest tests/people_gallery/test_activate_run_phase2_guard.py -v`
  - Expected: FAIL，至少一个 delta 场景未被拒绝。

- [ ] **Step 3: 实现 run 激活护栏服务并接入命令链路**
  - 护栏接入 `activate-run` 执行路径（CLI/Web 入口）。
  - 命中护栏时阻断 owner 切换并记录审计事件。
  - 返回明确错误码 `phase2_delta_present`。

- [ ] **Step 4: 运行通过测试并验证 DB 真值**
  - Run: `source .venv/bin/activate && PYTHONPATH=src python3 -m pytest tests/people_gallery/test_activate_run_phase2_guard.py tests/people_gallery/test_cli_control_plane.py tests/people_gallery/test_web_identity_tuning_page.py -v`
  - Expected: PASS，且断言：
    - 触发拒绝后 `is_materialization_owner` 不变
    - 错误码为 `phase2_delta_present`
    - 三类 delta 至少各有一个独立断言样本

**Task completion action (not a checkbox step): Commit task changes and plan progress**

```bash
git add src/hikbox_pictures/repositories/run_activation_guard_repo.py src/hikbox_pictures/services/run_activation_guard_service.py src/hikbox_pictures/services/identity_bootstrap_service.py src/hikbox_pictures/services/action_service.py src/hikbox_pictures/cli.py src/hikbox_pictures/api/routes_web.py tests/people_gallery/test_activate_run_phase2_guard.py tests/people_gallery/test_cli_control_plane.py tests/people_gallery/test_web_identity_tuning_page.py docs/superpowers/plans/2026-04-17-hikbox-pictures-v3-1-phase2-identity-system.md
git commit -m "feat: enforce phase2-delta guard on activate-run chain (Task 4)"
```

### Task 5: Observation 决策顺序、margin 统一口径与 low-confidence payload 契约

**Depends on:** Task 2, Task 3

**Scope Budget:**
- Max files: 20
- Estimated files touched: 9
- Max added lines: 1000
- Estimated added lines: 930

**Files:**
- Create: `src/hikbox_pictures/services/runtime_assignment_decision_service.py`
- Modify: `src/hikbox_pictures/services/ann_assignment_service.py`
- Modify: `src/hikbox_pictures/services/asset_stage_runner.py`
- Modify: `src/hikbox_pictures/repositories/review_repo.py`
- Modify: `src/hikbox_pictures/repositories/person_repo.py`
- Create: `tests/people_gallery/test_phase2_assignment_decision_pipeline.py`
- Modify: `tests/people_gallery/test_assignment_with_ann_thresholds.py`
- Modify: `tests/people_gallery/test_asset_stage_idempotency.py`
- Modify: `tests/people_gallery/fixtures_workspace.py`

- [ ] **Step 1: 写失败测试，锁定决策顺序与 payload 最小字段**
  - 决策顺序固定：`quality/excluded -> low-confidence 资格 -> direct-auto -> incremental candidate`。
  - margin 口径：缺 top2 时 `margin=+inf` 且 `second_candidate_missing=true`。
  - scan 入口前置条件失败路径：
    - 缺 active runtime profile 时，metadata/faces/embeddings 继续，assignment 子阶段显式失败并记录错误码。
    - 缺 owner run 时，metadata/faces/embeddings 继续，assignment 子阶段显式失败并记录错误码。
    - ANN 读异常时，metadata/faces/embeddings 继续，assignment 子阶段显式失败并记录错误码。
  - `ann_publish_state='failed'` 时 assignment 写路径 fail-closed，禁止写 `auto` assignment 与 low-confidence review。
  - 在线 review 生成冲突复用策略（`low_confidence_assignment`）：
    - 触发唯一冲突时复用既有 open review，不抛“请重试”错误。
    - 复用时刷新 `payload_json/priority/runtime_profile_id/evidence_fingerprint`。
  - 并发反例：同一 observation 在并发下 CAS 未命中时，不得覆盖已提交的 `manual/merge/bootstrap/incremental` assignment。
  - `low_confidence_assignment.payload_json` 必须包含：
    - `runtime_profile_id`
    - `model_key`
    - `top1_person_id`
    - `top1_distance`
    - `top2_person_id`
    - `top2_distance`
    - `margin`
    - `quality_score`
    - `photo_conflict`
    - `candidate_people`
    - `source_run_id`

- [ ] **Step 2: 运行失败测试**
  - Run: `source .venv/bin/activate && PYTHONPATH=src python3 -m pytest tests/people_gallery/test_phase2_assignment_decision_pipeline.py tests/people_gallery/test_assignment_with_ann_thresholds.py -v`
  - Expected: FAIL，至少命中 payload 字段缺失或决策顺序不一致。

- [ ] **Step 3: 实现 decision service 并接入 assignment stage**
  - `asset_stage_runner.assignment` 只允许使用 active runtime profile + owner observation profile。
  - assignment 子阶段进入写操作前统一执行入口前置条件校验；前置条件失败仅终止 assignment 子阶段，不回滚 metadata/faces/embeddings 已完成结果。
  - `ann_publish_state='failed'` 时 assignment 子阶段直接拒绝写入并返回显式错误。
  - low-confidence review 生成遇唯一冲突时执行“复用 + 刷新字段”策略，不向调用方抛重试错误。
  - assignment 写路径采用 `BEGIN IMMEDIATE + CAS`：CAS 未命中仅返回幂等成功或并发冲突，不得盲写覆盖。
  - `person_face_assignment` 写入补齐 `runtime_profile_id/source_run_id/source_cluster_id/diagnostic_json`。
  - `diagnostic_json` 标准字段至少包含 `decision_path/decision_result/runtime_profile_id/model_key/evaluated_at`。

- [ ] **Step 4: 运行通过测试并验证 DB 真值**
  - Run: `source .venv/bin/activate && PYTHONPATH=src python3 -m pytest tests/people_gallery/test_phase2_assignment_decision_pipeline.py tests/people_gallery/test_assignment_with_ann_thresholds.py tests/people_gallery/test_asset_stage_idempotency.py -v`
  - Expected: PASS，且断言：
    - low-confidence open 行每 observation 最多 1 条
    - `candidate_people` 与 `source_run_id` 在 payload 中非空
    - 已有 `manual/merge/bootstrap/incremental` assignment 不被自动覆盖
    - 缺 runtime/owner/ANN 读异常时 assignment 子阶段失败，但 metadata/faces/embeddings 进度继续推进
    - `ann_publish_state='failed'` 时 assignment 子阶段无新增 `auto` assignment 与 low-confidence review
    - low-confidence 唯一冲突场景下复用既有 open review 且四字段已刷新
    - CAS 未命中场景下后到写请求返回幂等成功或并发冲突，且 DB 无盲写覆盖

**Task completion action (not a checkbox step): Commit task changes and plan progress**

```bash
git add src/hikbox_pictures/services/runtime_assignment_decision_service.py src/hikbox_pictures/services/ann_assignment_service.py src/hikbox_pictures/services/asset_stage_runner.py src/hikbox_pictures/repositories/review_repo.py src/hikbox_pictures/repositories/person_repo.py tests/people_gallery/test_phase2_assignment_decision_pipeline.py tests/people_gallery/test_assignment_with_ann_thresholds.py tests/people_gallery/test_asset_stage_idempotency.py tests/people_gallery/fixtures_workspace.py docs/superpowers/plans/2026-04-17-hikbox-pictures-v3-1-phase2-identity-system.md
git commit -m "feat: enforce phase2 decision order and low-confidence payload contract (Task 5)"
```

### Task 6: Incremental run 收口 + review 显式动作 + 即时 scoped regeneration + ANN fail-closed

**Depends on:** Task 4, Task 5

**Scope Budget:**
- Max files: 20
- Estimated files touched: 14
- Max added lines: 1000
- Estimated added lines: 1000

**Files:**
- Create: `src/hikbox_pictures/services/incremental_cluster_run_service.py`
- Create: `src/hikbox_pictures/services/review_action_service.py`
- Create: `src/hikbox_pictures/services/ann_publish_state_service.py`
- Modify: `src/hikbox_pictures/services/review_regeneration_service.py`
- Modify: `src/hikbox_pictures/services/review_workflow_service.py`
- Modify: `src/hikbox_pictures/services/action_service.py`
- Modify: `src/hikbox_pictures/api/routes_reviews.py`
- Modify: `src/hikbox_pictures/repositories/review_repo.py`
- Modify: `src/hikbox_pictures/repositories/person_repo.py`
- Modify: `src/hikbox_pictures/services/prototype_service.py`
- Modify: `src/hikbox_pictures/ann/index_store.py`
- Create: `tests/people_gallery/test_incremental_run_resolution_states.py`
- Create: `tests/people_gallery/test_review_actions_phase2_contract.py`
- Create: `tests/people_gallery/test_review_actions_idempotency.py`
- Create: `tests/people_gallery/test_possible_merge_gate_and_resolution.py`
- Create: `tests/people_gallery/test_ann_publish_recovery.py`
- Modify: `tests/people_gallery/test_api_actions.py`

- [ ] **Step 1: 写失败测试，锁定 incremental 三态、动作细节与即时 regeneration**
  - incremental run 初始态仅允许 `materialized/review_pending/discarded`。
  - `new_person create-person` 成功后：`resolution_state='materialized'`、`publish_state='published'`、`resolution_reason='review_created_person'`。
  - `new_person assign-person` 成功后：`resolution_state='adopted'`、`publish_state='not_applicable'`、`resolution_reason='review_adopted_into_person'`。
  - `low_confidence confirm-person` 成功后：assignment 写 `manual+locked=1`，并写 `confirmed_at`。
  - `low_confidence reject-to-unassigned` 后：不立即生成新的 observation-backed `new_person`。
  - review `ignore` 成功后：`resolution_reason='review_ignored'`。
  - incremental auto materialize 成功后：`resolution_reason='incremental_materialized'`；系统丢弃为 `discarded` 时：`resolution_reason='discarded_by_system'`。
  - 每个成功动作都要断言“同请求链路完成 scoped regeneration 结果可见”。
  - 并发反例：同一 review 被并发提交不同动作时，后到请求 CAS 未命中不得覆盖先到提交结果。

- [ ] **Step 2: 写失败测试，锁定 possible_merge 生成门与 superseded 关闭规则**
  - 仅当双方满足：active + trusted 数达标 + prototype 存在 + 距离门 + margin 门时生成 open `possible_merge`。
  - margin 口径：缺 second neighbor 时写 `second_neighbor_missing=true` 且 margin 视为 `+inf`。
  - 执行 merge 动作后，所有引用 source person 的 open reviews 必须写 `superseded`。
  - 在线 review 生成冲突复用策略（`new_person` 与 `possible_merge`）：
    - 触发唯一冲突时复用既有 open review，不抛重试错误。
    - 复用时刷新 `payload_json/priority/runtime_profile_id/evidence_fingerprint`。

- [ ] **Step 3: 写失败测试，锁定 ANN fail-closed 与 preparing 恢复双分支**
  - `ann_publish_state='failed'` 时：review 动作写路径必须拒绝。
  - `ann_publish_state='preparing'` 恢复分支：
    - 分支 A（manifest 在 `epoch/model_key/embedding_dimension/index_sha256` 一致）补事务 B 成功回 `idle`。
    - 分支 B（manifest 不一致）转 `failed` 并拒绝写路径。

- [ ] **Step 4: 运行失败测试**
  - Run: `source .venv/bin/activate && PYTHONPATH=src python3 -m pytest tests/people_gallery/test_incremental_run_resolution_states.py tests/people_gallery/test_review_actions_phase2_contract.py tests/people_gallery/test_possible_merge_gate_and_resolution.py tests/people_gallery/test_ann_publish_recovery.py tests/people_gallery/test_api_actions.py -v`
  - Expected: FAIL，至少命中一条动作副作用、possible_merge 生成门或 ANN 恢复分支断言。

- [ ] **Step 5: 实现 incremental run 与 review 显式动作链路**
  - incremental 判定复用 phase1 gate，不新增增量专用 gate。
  - review 动作写路径统一使用 `BEGIN IMMEDIATE + CAS`；CAS 未命中仅返回幂等成功或并发冲突。
  - `review_item.resolution_action` 仅允许动作白名单映射。
  - 动作与 `resolution_reason` 必须一一映射并落库，禁止留空或写错枚举。
  - `new_person` / `possible_merge` / `low_confidence_assignment` 在线生成都采用“唯一冲突复用 + 四字段刷新”策略。
  - low-confidence 动作落实：
    - `confirm-person` 强制 `target_person_id`。
    - `reject-to-unassigned` 不立即建新 `new_person`。
  - `possible_merge` 动作落实：`merge-into-primary/merge-into-secondary/keep-separate/ignore`。

- [ ] **Step 6: 实现动作后即时 scoped regeneration（Task 6 内完成）**
  - `create-person/assign-person/confirm-person/merge-into-*/reject-to-unassigned` 成功后立即调用 scoped regeneration。
  - scoped 范围至少覆盖受影响 observation/person/cluster。
  - 不允许延迟到 `reviews regenerate` CLI。

- [ ] **Step 7: 实现 ANN 双阶段发布 + fail-closed + 恢复逻辑**
  - 写路径前检查 `ann_publish_state`；`failed` 直接拒绝。
  - 完整落地 `prepare -> switch -> epoch commit`。
  - 实现 `preparing` 双分支恢复逻辑与审计字段回写；一致性比较字段固定为 `epoch/model_key/embedding_dimension/index_sha256`。

- [ ] **Step 8: 运行通过测试并验证 DB 真值**
  - Run: `source .venv/bin/activate && PYTHONPATH=src python3 -m pytest tests/people_gallery/test_incremental_run_resolution_states.py tests/people_gallery/test_review_actions_phase2_contract.py tests/people_gallery/test_review_actions_idempotency.py tests/people_gallery/test_possible_merge_gate_and_resolution.py tests/people_gallery/test_ann_publish_recovery.py tests/people_gallery/test_api_actions.py -v`
  - Expected: PASS，且断言：
    - `resolved/ignored` review 全部有 `resolution_action`
    - `reject-to-unassigned` 后立即 `new_person` open 数不增长
    - `ann_publish_state='failed'` 时写路径返回错误且 DB 真相不变
    - merge 后 source 关联 open reviews 均为 `superseded`
    - 动作 -> `resolution_reason` 映射断言全部通过（覆盖 `incremental_materialized/review_created_person/review_adopted_into_person/review_ignored/discarded_by_system`）
    - `new_person/possible_merge` 唯一冲突场景下复用既有 open review 且四字段刷新
    - 并发动作 CAS 未命中场景下后到请求不覆盖先到结果（仅幂等成功或并发冲突）

**Task completion action (not a checkbox step): Commit task changes and plan progress**

```bash
git add src/hikbox_pictures/services/incremental_cluster_run_service.py src/hikbox_pictures/services/review_action_service.py src/hikbox_pictures/services/ann_publish_state_service.py src/hikbox_pictures/services/review_regeneration_service.py src/hikbox_pictures/services/review_workflow_service.py src/hikbox_pictures/services/action_service.py src/hikbox_pictures/api/routes_reviews.py src/hikbox_pictures/repositories/review_repo.py src/hikbox_pictures/repositories/person_repo.py src/hikbox_pictures/services/prototype_service.py src/hikbox_pictures/ann/index_store.py tests/people_gallery/test_incremental_run_resolution_states.py tests/people_gallery/test_review_actions_phase2_contract.py tests/people_gallery/test_review_actions_idempotency.py tests/people_gallery/test_possible_merge_gate_and_resolution.py tests/people_gallery/test_ann_publish_recovery.py tests/people_gallery/test_api_actions.py docs/superpowers/plans/2026-04-17-hikbox-pictures-v3-1-phase2-identity-system.md
git commit -m "feat: implement review action engine with immediate regeneration and ann fail-closed (Task 6)"
```

### Task 7: People 四动作收口 + merge trusted 审计 + 即时 scoped regeneration

**Depends on:** Task 6

**Scope Budget:**
- Max files: 20
- Estimated files touched: 12
- Max added lines: 1000
- Estimated added lines: 980

**Files:**
- Modify: `src/hikbox_pictures/api/routes_people.py`
- Modify: `src/hikbox_pictures/services/person_truth_service.py`
- Modify: `src/hikbox_pictures/services/action_service.py`
- Modify: `src/hikbox_pictures/repositories/person_repo.py`
- Modify: `src/hikbox_pictures/repositories/export_repo.py`
- Modify: `src/hikbox_pictures/services/prototype_service.py`
- Modify: `src/hikbox_pictures/services/review_regeneration_service.py`
- Modify: `src/hikbox_pictures/services/web_query_service.py`
- Create: `tests/people_gallery/test_people_phase2_actions_contract.py`
- Create: `tests/people_gallery/test_confirm_assignments_bootstrap_gate.py`
- Create: `tests/people_gallery/test_exclude_assignments_requeue.py`
- Create: `tests/people_gallery/test_merge_export_template_rewrite.py`
- Modify: `tests/people_gallery/test_person_truth_actions.py`

- [ ] **Step 1: 写失败测试，锁定 people 正式动作与旧动作下线**
  - 仅保留 `rename/merge/confirm-assignments/exclude-assignments`。
  - `split` 和 `lock-assignment` 必须返回不可用。
  - `rename` 语义：匿名 -> 正式名时 `confirmed=1`；正式名再改名不改变 `confirmed`。
  - `confirm-assignments` 严格门在缺 second person 时，诊断字段必须写 `second_person_missing=true` 且 margin 按 `+inf` 处理。
  - `confirm-assignments` 逐条失败原因最小集合必须完整返回（7项）：
    - `quality_too_low`
    - `distance_too_far`
    - `margin_too_small`
    - `exact_duplicate_blocked`
    - `burst_duplicate_blocked`
    - `bootstrap_sample_count_insufficient`
    - `bootstrap_photo_count_insufficient`
  - `ann_publish_state='failed'` 时 people 写路径（merge/confirm-assignments/exclude-assignments）必须 fail-closed 拒绝。

- [ ] **Step 2: 写失败测试，锁定 trusted 来源语义与 merge 审计字段**
  - `person_trusted_sample.trust_source` 仅允许 `bootstrap_seed/incremental_seed/review_seed/manual_confirm`。
  - merge 迁移后 target 新建 trusted 记录必须保留原 `trust_source`，并写 `merge_from_person_id/source_review_id`。
  - source trusted 记录必须失活，不允许直接改 `person_id` 原地迁移。

- [ ] **Step 3: 写失败测试，锁定 confirm/exclude 后即时 scoped regeneration**
  - `confirm-assignments`、`exclude-assignments`、`merge` 成功后必须立即触发 scoped regeneration。
  - 校验同请求链路内可观察到 review 队列变化（新增/ superseded/消失）。
  - 并发反例：同一 assignment/同一 person 被并发提交时，CAS 未命中请求不得覆盖先到提交状态。

- [ ] **Step 4: 运行失败测试**
  - Run: `source .venv/bin/activate && PYTHONPATH=src python3 -m pytest tests/people_gallery/test_people_phase2_actions_contract.py tests/people_gallery/test_confirm_assignments_bootstrap_gate.py tests/people_gallery/test_exclude_assignments_requeue.py tests/people_gallery/test_merge_export_template_rewrite.py tests/people_gallery/test_person_truth_actions.py -v`
  - Expected: FAIL，至少命中 trusted 审计字段或即时 regeneration 断言。

- [ ] **Step 5: 实现 people 四动作与 gate 细节**
  - people 写路径统一采用 `BEGIN IMMEDIATE + CAS`；CAS 未命中仅返回幂等成功或并发冲突。
  - `confirm-assignments`：严格门/启动门，批量启动子集按 `quality_score DESC, observation_id ASC`。
  - `exclude-assignments`：assignment/trusted 失活，prototype/ANN 重建，observation 回未归属候选池。
  - `merge`：assignment 改 `merge` 来源、trusted 重建迁移、origin 增 `merge_adopt`、模板绑定重写。

- [ ] **Step 6: 实现动作后即时 scoped regeneration（Task 7 内完成）**
  - `merge/confirm-assignments/exclude-assignments` 成功后立即执行 scoped regeneration。
  - 返回结果中附带 regeneration 摘要（受影响 review 数）。

- [ ] **Step 7: 运行通过测试并验证 DB 真值**
  - Run: `source .venv/bin/activate && PYTHONPATH=src python3 -m pytest tests/people_gallery/test_people_phase2_actions_contract.py tests/people_gallery/test_confirm_assignments_bootstrap_gate.py tests/people_gallery/test_exclude_assignments_requeue.py tests/people_gallery/test_merge_export_template_rewrite.py tests/people_gallery/test_person_truth_actions.py -v`
  - Expected: PASS，且断言：
    - source person `status='merged'`
    - target trusted 新记录保留原 `trust_source` 且 `merge_from_person_id/source_review_id` 非空
    - `export_template_person` 无记录指向 merged source
    - exclude 后 observation 可重新进入 incremental candidate
    - `confirm-assignments` 批量响应覆盖并仅使用这 7 项失败原因编码：`quality_too_low/distance_too_far/margin_too_small/exact_duplicate_blocked/burst_duplicate_blocked/bootstrap_sample_count_insufficient/bootstrap_photo_count_insufficient`
    - `ann_publish_state='failed'` 时 people 写路径拒绝且 DB 真相不变
    - CAS 未命中场景下后到请求不覆盖先到提交结果

**Task completion action (not a checkbox step): Commit task changes and plan progress**

```bash
git add src/hikbox_pictures/api/routes_people.py src/hikbox_pictures/services/person_truth_service.py src/hikbox_pictures/services/action_service.py src/hikbox_pictures/repositories/person_repo.py src/hikbox_pictures/repositories/export_repo.py src/hikbox_pictures/services/prototype_service.py src/hikbox_pictures/services/review_regeneration_service.py src/hikbox_pictures/services/web_query_service.py tests/people_gallery/test_people_phase2_actions_contract.py tests/people_gallery/test_confirm_assignments_bootstrap_gate.py tests/people_gallery/test_exclude_assignments_requeue.py tests/people_gallery/test_merge_export_template_rewrite.py tests/people_gallery/test_person_truth_actions.py docs/superpowers/plans/2026-04-17-hikbox-pictures-v3-1-phase2-identity-system.md
git commit -m "feat: converge people actions with trusted merge audit and immediate regeneration (Task 7)"
```

### Task 8: `reviews regenerate` 运维入口与再入控制

**Depends on:** Task 6

**Scope Budget:**
- Max files: 20
- Estimated files touched: 8
- Max added lines: 1000
- Estimated added lines: 760

**Files:**
- Modify: `src/hikbox_pictures/services/review_regeneration_service.py`
- Modify: `src/hikbox_pictures/repositories/review_repo.py`
- Modify: `src/hikbox_pictures/services/review_workflow_service.py`
- Modify: `src/hikbox_pictures/cli.py`
- Create: `tests/people_gallery/test_review_regeneration_service.py`
- Create: `tests/people_gallery/test_reviews_regenerate_cli.py`
- Create: `tests/people_gallery/test_review_fingerprint_reentry_control.py`
- Modify: `tests/people_gallery/test_cli_control_plane.py`

- [ ] **Step 1: 写失败测试，锁定 CLI 作用域与再入规则**
  - `reviews regenerate` 支持全库与 scoped（`--person-id/--observation-id`）。
  - 不改动 cluster 真相和 trusted sample 真相。
  - 同对象同指纹不重复 open；对象已有 open 时冲突复用并刷新 payload。

- [ ] **Step 2: 运行失败测试**
  - Run: `source .venv/bin/activate && PYTHONPATH=src python3 -m pytest tests/people_gallery/test_review_regeneration_service.py tests/people_gallery/test_reviews_regenerate_cli.py tests/people_gallery/test_review_fingerprint_reentry_control.py -v`
  - Expected: FAIL，至少包含 CLI 缺失或指纹再入规则不满足。

- [ ] **Step 3: 实现 CLI 与 scoped/full regeneration**
  - CLI：`python -m hikbox_pictures.cli reviews regenerate --workspace ... [--person-id ...] [--observation-id ...]`。
  - regeneration 覆盖 `new_person/low_confidence_assignment/possible_merge`。
  - 旧 review 失效统一写 `superseded`。

- [ ] **Step 4: 运行通过测试并验证 DB 真值**
  - Run: `source .venv/bin/activate && PYTHONPATH=src python3 -m pytest tests/people_gallery/test_review_regeneration_service.py tests/people_gallery/test_reviews_regenerate_cli.py tests/people_gallery/test_review_fingerprint_reentry_control.py tests/people_gallery/test_cli_control_plane.py -v`
  - Expected: PASS，且断言：
    - regenerate 前后 `identity_cluster*` 与 `person_trusted_sample` 行数不变
    - 同指纹重跑 open 数不增长

**Task completion action (not a checkbox step): Commit task changes and plan progress**

```bash
git add src/hikbox_pictures/services/review_regeneration_service.py src/hikbox_pictures/repositories/review_repo.py src/hikbox_pictures/services/review_workflow_service.py src/hikbox_pictures/cli.py tests/people_gallery/test_review_regeneration_service.py tests/people_gallery/test_reviews_regenerate_cli.py tests/people_gallery/test_review_fingerprint_reentry_control.py tests/people_gallery/test_cli_control_plane.py docs/superpowers/plans/2026-04-17-hikbox-pictures-v3-1-phase2-identity-system.md
git commit -m "feat: add reviews regenerate cli with fingerprint reentry control (Task 8)"
```

### Task 9: `rebuild-artifacts` 专项收口（只重建 prototype/ANN）

**Depends on:** Task 7

**Scope Budget:**
- Max files: 20
- Estimated files touched: 7
- Max added lines: 1000
- Estimated added lines: 620

**Files:**
- Modify: `src/hikbox_pictures/services/prototype_service.py`
- Modify: `src/hikbox_pictures/services/action_service.py`
- Modify: `src/hikbox_pictures/services/runtime.py`
- Modify: `src/hikbox_pictures/ann/index_store.py`
- Create: `tests/people_gallery/test_rebuild_artifacts_contract.py`
- Modify: `tests/people_gallery/test_cli_control_plane.py`
- Modify: `tests/people_gallery/test_prototype_from_trusted_samples.py`

- [ ] **Step 1: 写失败测试，锁定 rebuild-artifacts 只影响派生产物**
  - 执行前后以下表计数必须不变：
    - `person_face_assignment`
    - `person_trusted_sample`
    - `review_item`
    - `identity_cluster`
    - `identity_cluster_resolution`
  - 有 active person 但无 active trusted sample 时：prototype 必须失活且从 live ANN 移除。

- [ ] **Step 2: 运行失败测试**
  - Run: `source .venv/bin/activate && PYTHONPATH=src python3 -m pytest tests/people_gallery/test_rebuild_artifacts_contract.py -v`
  - Expected: FAIL，至少命中“误修改 assignment/review/cluster”或 prototype/ANN 状态不一致。

- [ ] **Step 3: 实现 rebuild-artifacts 专项逻辑**
  - 只扫描 active person，依据 active trusted sample 重建 prototype，再聚合重建 ANN。
  - 不允许触发 review 生成、cluster 变更、assignment/trusted 写入。

- [ ] **Step 4: 运行通过测试并验证 DB 真值**
  - Run: `source .venv/bin/activate && PYTHONPATH=src python3 -m pytest tests/people_gallery/test_rebuild_artifacts_contract.py tests/people_gallery/test_prototype_from_trusted_samples.py tests/people_gallery/test_cli_control_plane.py -v`
  - Expected: PASS，且断言：
    - rebuild 前后 assignment/trusted/review/cluster 行数完全一致
    - prototype/ANN 状态与 active trusted sample 一致

**Task completion action (not a checkbox step): Commit task changes and plan progress**

```bash
git add src/hikbox_pictures/services/prototype_service.py src/hikbox_pictures/services/action_service.py src/hikbox_pictures/services/runtime.py src/hikbox_pictures/ann/index_store.py tests/people_gallery/test_rebuild_artifacts_contract.py tests/people_gallery/test_cli_control_plane.py tests/people_gallery/test_prototype_from_trusted_samples.py docs/superpowers/plans/2026-04-17-hikbox-pictures-v3-1-phase2-identity-system.md
git commit -m "feat: constrain rebuild-artifacts to prototype and ann only (Task 9)"
```

### Task 10: WebUI/API/Export 收口与旧入口退场

**Depends on:** Task 7, Task 8, Task 9

**Scope Budget:**
- Max files: 20
- Estimated files touched: 18
- Max added lines: 1000
- Estimated added lines: 990

**Files:**
- Modify: `src/hikbox_pictures/api/routes_web.py`
- Modify: `src/hikbox_pictures/api/routes_reviews.py`
- Modify: `src/hikbox_pictures/api/routes_people.py`
- Modify: `src/hikbox_pictures/api/routes_export.py`
- Modify: `src/hikbox_pictures/services/web_query_service.py`
- Modify: `src/hikbox_pictures/web/templates/review_queue.html`
- Modify: `src/hikbox_pictures/web/templates/people.html`
- Modify: `src/hikbox_pictures/web/templates/person_detail.html`
- Modify: `src/hikbox_pictures/web/templates/export_templates.html`
- Modify: `src/hikbox_pictures/web/static/app.js`
- Modify: `src/hikbox_pictures/web/static/style.css`
- Modify: `tests/people_gallery/test_api_contract.py`
- Modify: `tests/people_gallery/test_review_actions_contract.py`
- Modify: `tests/people_gallery/test_webui_content.py`
- Modify: `tests/people_gallery/test_webui_actions_e2e.py`
- Modify: `tests/people_gallery/test_export_matching_and_ledger.py`
- Modify: `tools/review_queue_playwright_check.py`
- Modify: `tools/review_queue_playwright_capture.cjs`

- [ ] **Step 1: 写失败测试，锁定页面/API 最终语义**
  - review 页只保留 `new_person/low_confidence_assignment/possible_merge`。
  - 人物详情页只保留四类动作按钮。
  - 导出页区分“无 assignment / 有 assignment 无 prototype / merged / ignored”。
  - `/identity-tuning` 只读诊断，不承载正式动作。

- [ ] **Step 2: 运行失败测试（含 Playwright）**
  - Run: `source .venv/bin/activate && PYTHONPATH=src python3 -m pytest tests/people_gallery/test_api_contract.py tests/people_gallery/test_review_actions_contract.py tests/people_gallery/test_webui_content.py tests/people_gallery/test_webui_actions_e2e.py tests/people_gallery/test_export_matching_and_ledger.py -v`
  - Run: `source .venv/bin/activate && python3 tools/review_queue_playwright_check.py --workspace .tmp/phase2-webui/workspace --output-dir .tmp/phase2-webui/output --runner-dir .tmp/phase2-webui/runner --install-browser`
  - Expected: 先 FAIL（旧按钮或旧队列仍存在）。

- [ ] **Step 3: 实现 UI/API 收口**
  - 删除 `possible_split`、`split`、`lock-assignment`、`resolve`、`dismiss` 的页面与路由暴露。
  - 人物列表/详情补齐 `status`、trusted 数、prototype/ANN 状态、pending review 数。

- [ ] **Step 4: 运行通过测试并验证真值**
  - Run: `source .venv/bin/activate && PYTHONPATH=src python3 -m pytest tests/people_gallery/test_api_contract.py tests/people_gallery/test_review_actions_contract.py tests/people_gallery/test_webui_content.py tests/people_gallery/test_webui_actions_e2e.py tests/people_gallery/test_export_matching_and_ledger.py tests/people_gallery/test_web_navigation.py -v`
  - Run: `source .venv/bin/activate && python3 tools/review_queue_playwright_check.py --workspace .tmp/phase2-webui/workspace --output-dir .tmp/phase2-webui/output --runner-dir .tmp/phase2-webui/runner`
  - Expected: PASS，且断言：
    - API 路由枚举无旧端点
    - 页面无 `possible_split` 队列
    - merged person 不再作为模板绑定目标

**Task completion action (not a checkbox step): Commit task changes and plan progress**

```bash
git add src/hikbox_pictures/api/routes_web.py src/hikbox_pictures/api/routes_reviews.py src/hikbox_pictures/api/routes_people.py src/hikbox_pictures/api/routes_export.py src/hikbox_pictures/services/web_query_service.py src/hikbox_pictures/web/templates/review_queue.html src/hikbox_pictures/web/templates/people.html src/hikbox_pictures/web/templates/person_detail.html src/hikbox_pictures/web/templates/export_templates.html src/hikbox_pictures/web/static/app.js src/hikbox_pictures/web/static/style.css tests/people_gallery/test_api_contract.py tests/people_gallery/test_review_actions_contract.py tests/people_gallery/test_webui_content.py tests/people_gallery/test_webui_actions_e2e.py tests/people_gallery/test_export_matching_and_ledger.py tools/review_queue_playwright_check.py tools/review_queue_playwright_capture.cjs docs/superpowers/plans/2026-04-17-hikbox-pictures-v3-1-phase2-identity-system.md
git commit -m "feat: converge phase2 webui api and export surfaces (Task 10)"
```

### Task 11: README 收口、A01-A25 验收矩阵执行与最终回归

**Depends on:** Task 10

**Scope Budget:**
- Max files: 20
- Estimated files touched: 6
- Max added lines: 1000
- Estimated added lines: 640

**Files:**
- Modify: `README.md`
- Modify: `docs/db_schema/README.md`
- Create: `tests/people_gallery/test_phase2_acceptance_matrix.py`
- Modify: `tests/people_gallery/test_e2e_full_system.py`
- Modify: `tests/people_gallery/test_web_navigation.py`
- Modify: `scripts/run_tests.sh`

- [ ] **Step 1: 写失败测试，固化 A01-A25 验收矩阵**
  - `test_phase2_acceptance_matrix.py` 按 A01-A25 建立可执行断言，逐条映射 API/CLI/DB 真值。
  - 增加旧语义检索断言：运行路径不允许出现 `possible_split`、`/actions/resolve`、`/actions/dismiss`、`/actions/split`、`lock-assignment`。

- [ ] **Step 2: 运行失败测试**
  - Run: `source .venv/bin/activate && PYTHONPATH=src python3 -m pytest tests/people_gallery/test_phase2_acceptance_matrix.py -v`
  - Expected: FAIL，直到所有验收项落实。

- [ ] **Step 3: 更新 README 与运维说明**
  - README 必须清晰区分：
    - phase2 日常路径（scan -> review -> people -> export）
    - phase1 诊断入口（`/identity-tuning`）
  - 明确 `rebuild-artifacts` 仅重建 prototype/ANN。
  - 明确 `reviews regenerate` 为运维入口，不替代在线即时 regeneration。
  - 明确 `activate-run` 在 phase2 delta 存在时返回 `phase2_delta_present`。

- [ ] **Step 4: 全量回归与旧语义清零检查**
  - Run: `source .venv/bin/activate && ./scripts/run_tests.sh`
  - Run: `source .venv/bin/activate && PYTHONPATH=src python3 -m pytest tests/people_gallery/test_phase2_acceptance_matrix.py tests/people_gallery/test_e2e_full_system.py -v`
  - Run: `rg "possible_split|/actions/resolve|/actions/dismiss|/actions/split|lock-assignment" src tests README.md`
  - Expected: 回归 PASS；`rg` 仅允许命中历史迁移说明，不允许命中正式运行路径与当前测试期望。

**Task completion action (not a checkbox step): Commit task changes and plan progress**

```bash
git add README.md docs/db_schema/README.md tests/people_gallery/test_phase2_acceptance_matrix.py tests/people_gallery/test_e2e_full_system.py tests/people_gallery/test_web_navigation.py scripts/run_tests.sh docs/superpowers/plans/2026-04-17-hikbox-pictures-v3-1-phase2-identity-system.md
git commit -m "docs: finalize phase2 readme and executable acceptance matrix (Task 11)"
```

## 计划自检结论

- 已把 `activate-run` phase2-delta 护栏从 runtime profile 生命周期中剥离，单独落到 run 激活链路（Task 4）。
- 已把“动作后立即 scoped regeneration”写入 Task 6/7 执行链路和断言，不再依赖 Task 8 CLI。
- Task 1 已补齐 DB 级契约细节：review 分型 CHECK、pair CHECK、open 唯一索引、publish/resolution 耦合、非法插入失败断言。
- 已补齐 runtime profile 字段全集与跨字段约束（Task 2）。
- 已补齐 trusted source 语义与 merge 审计字段（Task 1 + Task 7）。
- 已补齐 margin 统一口径中的 `second_person_missing` 与 `second_neighbor_missing`（Task 5/6/7）。
- 已补齐 low-confidence payload/动作细节（Task 5/6）。
- 已补齐 possible_merge 生成门与 merge 后 superseded 关闭规则（Task 6）。
- 已补齐 ANN fail-closed 与 preparing 恢复双分支（Task 6）。
- 已补齐 Task 5/6/7 的 `BEGIN IMMEDIATE + CAS` 与并发反例测试要求（CAS 未命中只允许幂等成功或并发冲突）。
- 已把 ANN fail-closed 扩展到 assignment（Task 5）与 people（Task 7）写路径断言。
- 已补齐 `identity_cluster_run.trigger_scan_session_id` 写入与不可回填断言（Task 3）。
- 已补齐 `resolution_reason` 枚举、写入规则与动作映射断言（Task 1 + Task 6）。
- 已新增 `rebuild-artifacts` 专项任务（Task 9）。
- 已将 Wave E 调整为 `Task 7 -> Task 8` 串行，消除共享写文件冲突。
