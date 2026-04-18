# HikBox Pictures v3.1 快速验证导出工具 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax. Executors run dependency-free tasks in parallel when safe (default max concurrency: `5`). All subagents use the controller's current model; implementers use `medium` reasoning and reviewers use `xhigh` reasoning. Execution tracking, checkbox ownership, `Task completion action` timing, and task completion commit behavior follow `superpowers:subagent-driven-development`. Task worktree assignment, merge-back, and cleanup follow `superpowers:using-git-worktrees`.

**Goal:** 在仓库内新增一个离线、单页的 v3.1 prototype export 工具，直接消费 phase1 的 `identity_cluster_run` / `identity_cluster` / `identity_cluster_member` / `identity_cluster_resolution` / `identity_observation_snapshot` 真相，验证当前 final cluster 作为 seed identity 的可用性，以及其他 observation 的 `auto_assign / review / reject` 分布和证据是否站得住。

**Architecture:** 实现全部放在新的实验包 `src/hikbox_experiments/identity_v3_1/` 中，分成 `query_service.py`、`assignment_service.py`、`export_service.py` 三层，再由独立脚本 `scripts/export_identity_v3_1_report.py` 触发。查询与 assign 逻辑只消费现有 phase1 真相；导出阶段允许调用 `PreviewArtifactService.ensure_crop()`、`ensure_context()`、`ensure_photo_preview()` 补齐 crop/context/preview artifact 及其必要元数据写入，这类写入仅限证据图资产，不属于 identity/person/runtime 真相回写。HTML 直接在服务中输出为离线单页，不修改 `hikbox_pictures.cli`、FastAPI、WebUI 路由、schema 或正式 runtime 真相。

**Tech Stack:** Python 3.12、SQLite、NumPy、Pillow、`argparse`、`Path`、pytest、`PreviewArtifactService`

---

## 约束与防偏离实现规则

- 只新增实验包、独立脚本和测试；不要修改 `src/hikbox_pictures/cli.py`、API 路由、Jinja 模板、migration、`docs/db_schema/README.md`。
- 输出目录默认固定为 `repo/.tmp/v3_1-identity-prototype/<timestamp>/`；如果传入 `--output-root`，仍然只写该目录下的新时间戳子目录。
- 本需求默认 workspace 固定为 `repo/.tmp/.hikbox`；脚本未显式传入 `--workspace` 时必须使用该路径。
- HTML 不允许引入 Jinja 模板或其他 package-data；所有结构和样式直接在 `export_service.py` 里生成。
- 距离度量必须与 [src/hikbox_pictures/ann/index_store.py](/Users/linker/src/Piasy/HikBoxPictures/src/hikbox_pictures/ann/index_store.py) 的 `AnnIndexStore.search()` 一致，只使用 normalized embedding 的 L2 距离；禁止改成 cosine。
- 任何临时 seed、assign 判定、override 结果都不能回写 identity/person/runtime 真相；唯一允许的 workspace 写入是 `PreviewArtifactService.ensure_*()` 触发的 crop/context/preview artifact 补齐及其必要元数据/observability 记录，除此之外的新增落盘只允许是导出 bundle 的 `index.html`、`manifest.json`、`assets/`。
- 若仓库根目录不存在 `.venv`，执行任务前先运行 `./scripts/install.sh`；所有验证命令默认先 `source .venv/bin/activate`。

## 固定实现口径

### 参数、枚举和值域

- `--base-run-id` 显式提供时，必须覆盖默认 `is_review_target = 1` 选择路径，但仅允许指向 `run_status='succeeded'` 的 run。
- `assign_source` 只允许 `all`、`review_pending`、`attachment`，默认 `all`。
- 默认参数固定为：
  - `top_k = 5`
  - `auto_max_distance = 0.25`
  - `review_max_distance = 0.35`
  - `min_margin = 0.08`
- `auto_max_distance > review_max_distance`、`top_k <= 0`、`min_margin < 0` 都必须直接失败。
- `AssignParameters.validate()` 必须在两个入口被显式调用，并由测试证明生效：
  - `IdentityV31QueryService.load_report_context()` 入口
  - `scripts/export_identity_v3_1_report.py` 在调用 export service 之前
- `identity_id` 固定生成 `seed-cluster-<cluster_id>`，确保 manifest diff 稳定。

### seed identity 生成

- 默认 seed = 所选 run 下 `cluster_stage='final'`、`cluster_state='active'`、`resolution_state='materialized'` 的 cluster。
- `--promote-cluster-ids` 只能提升同一个 run 下 `review_pending` final cluster；指定不存在 cluster、非 final cluster 或非 `review_pending` cluster 时直接失败。
- `--disable-seed-cluster-ids` 只能禁用默认 seed 或本次 promote 成功的 seed；指定未知 cluster 时直接失败。
- prototype 计算优先顺序固定为：
  1. `is_selected_trusted_seed = 1` 且 embedding 有效的成员
  2. 若为空，则退回 `decision_status != 'rejected'` 且 embedding 有效的 retained 成员
  3. 对选中向量求均值，再做一次归一化
- 某个 seed cluster 没有任何有效 prototype 成员时，不中断整轮导出；该 cluster 记录为无效 seed，写入 manifest `errors`，并从 assign 召回集合中排除。
- 如果所有启用 seed 都无效，则导出脚本必须返回非零退出码。

### assign 判定口径

- `same_photo_conflict` 固定定义为：候选 observation 的 `photo_id` 与最佳 seed cluster 中任一 `decision_status != 'rejected'` 成员的 `photo_id` 相同。
- 最近邻召回使用暴力 L2：

```python
delta = prototype_matrix - query.reshape(1, -1)
distances = np.linalg.norm(delta, axis=1)
order = np.lexsort((seed_cluster_ids, distances))
```

- `distance_margin = second_best_distance - best_distance`；如果没有第二候选，写 `float("inf")`。
- `reason_code` 固定按以下优先级产生，避免同一 observation 在不同导出中漂移：
  - `no_seed_candidates`
  - `distance_above_review_threshold`
  - `same_photo_conflict`
  - `margin_below_threshold`
  - `distance_above_auto_threshold`
  - `auto_threshold_pass`
- 决策只允许：
  - `auto_assign`
  - `review`
  - `reject`

### candidate 计数不变量

- 默认 candidate 来源 = `review_pending retained` + snapshot `attachment`。
- 去重按 `observation_id`；同一 observation 同时命中两类来源时，`source_kind` 固定记为 `review_pending_retained`。
- 进入 assign 判定前必须排除：
  - 已属于启用 seed 的 observation
  - 缺少 normalized embedding 的 observation
  - embedding 维度与有效 seed prototype 维度不一致的 observation
- `assignment_summary.candidate_count` 只统计真正跑过判定的 observation，并保持：

```text
candidate_count = auto_assign_count + review_count + reject_count
```

- `missing_embedding_count` 和 `dimension_mismatch_count` 统计被跳过的 observation，不计入 `candidate_count`。
- 仅当“来源筛选 + seed 排除”后的候选集合非空，且所有 observation 都因缺 embedding 被跳过时，导出必须硬失败。

### manifest 与 HTML 结构

- `manifest.json` 顶层至少固定包含：
  - `workspace`
  - `db_path`
  - `generated_at`
  - `base_run`
  - `snapshot`
  - `parameters`
  - `seed_identities`
  - `pending_clusters`
  - `assignment_summary`
  - `warnings`
  - `errors`
- `errors` 每项至少包含：
  - `code`
  - `cluster_id`
  - `message`
- `base_run` 至少包含：
  - `id`
  - `run_status`
  - `observation_snapshot_id`
  - `cluster_profile_id`
  - `is_review_target`
- `parameters` 至少包含：
  - `base_run_id`
  - `assign_source`
  - `top_k`
  - `auto_max_distance`
  - `review_max_distance`
  - `min_margin`
  - `promote_cluster_ids`
  - `disable_seed_cluster_ids`
- `snapshot` 至少包含：
  - `id`
  - `observation_profile_id`
  - `embedding_model_key`
- `seed_identities` 每项至少包含：
  - `identity_id`
  - `source_cluster_id`
  - `resolution_state`
  - `seed_member_count`
  - `fallback_used`
  - `prototype_dimension`
  - `representative_observation_id`
  - `member_observation_ids`
  - `valid`
  - `error_code`
  - `error_message`
- 所有尝试启用的 seed cluster 都必须落入 `seed_identities`；invalid seed 也必须保留同形状记录，要求 `valid = false`、`prototype_dimension = null`、并在 `errors` 中有对应 `cluster_id` 的错误对象。
- `pending_clusters` 每项至少包含：
  - `cluster_id`
  - `retained_member_count`
  - `distinct_photo_count`
  - `representative_count`
  - `retained_count`
  - `excluded_count`
  - `promoted_to_seed`
- 为了让 HTML 和脚本 diff 共用同一份结构，再额外增加顶层 `assignments` 数组；每条 assignment 至少包含：
  - `observation_id`
  - `photo_id`
  - `source_kind`
  - `source_cluster_id`
  - `best_identity_id`
  - `best_cluster_id`
  - `best_distance`
  - `second_best_distance`
  - `distance_margin`
  - `same_photo_conflict`
  - `decision`
  - `reason_code`
  - `top_candidates`
  - `assets`
- `assignments[*].top_candidates` 必须是按排序后截断的对象数组，每项至少包含：
  - `rank`
  - `identity_id`
  - `cluster_id`
  - `distance`
  且顺序固定按 `rank ASC`，对应的距离排序口径固定为 `distance ASC, cluster_id ASC`。
- `index.html` 固定为单页，必须至少有以下 `<details id="...">` 区块：
  - `summary`
  - `seed-identities`
  - `overrides`
  - `review-pending-clusters`
  - `bucket-auto-assign`
  - `bucket-review`
  - `bucket-reject`
- `summary` 区块必须展示：
  - workspace 路径
  - base run id
  - snapshot id
  - generated_at
  - 参数摘要
  - seed identity 数量
  - `auto_assign / review / reject` 计数
  - warnings / errors 摘要
- `seed identities` 卡片必须展示：
  - source cluster id
  - resolution state
  - prototype 成员数
  - fallback 是否触发
  - representative crop/context
  - seed member observation id 列表
- `review_pending clusters` 卡片必须展示：
  - cluster id
  - retained member count
  - distinct photo count
  - representative / retained / excluded 计数
  - promoted_to_seed
- `overrides` 区块必须展示：
  - promoted cluster ids
  - disabled seed cluster ids
  - invalid prototype cluster ids
  - 当某项为空时明确展示 `none`，不能省略该行
- `assignment` 卡片必须展示：
  - crop
  - context 或 preview
  - observation id / photo id
  - source kind
  - 原 cluster id
  - best candidate cluster
  - top-k candidate 及距离
  - margin
  - same-photo-conflict
  - decision
  - reason_code

## 文件结构设计

### 新增实验包

- Create: `src/hikbox_experiments/__init__.py`
- Create: `src/hikbox_experiments/identity_v3_1/__init__.py`
- Create: `src/hikbox_experiments/identity_v3_1/models.py`
- Create: `src/hikbox_experiments/identity_v3_1/query_service.py`
- Create: `src/hikbox_experiments/identity_v3_1/assignment_service.py`
- Create: `src/hikbox_experiments/identity_v3_1/export_service.py`

### 新增独立脚本

- Create: `scripts/export_identity_v3_1_report.py`

### 新增测试与夹具

- Create: `tests/people_gallery/fixtures_identity_v3_1_export.py`
- Create: `tests/people_gallery/test_identity_v3_1_export_fixtures.py`
- Create: `tests/people_gallery/test_identity_v3_1_query_service.py`
- Create: `tests/people_gallery/test_identity_v3_1_assignment_service.py`
- Create: `tests/people_gallery/test_identity_v3_1_export_service.py`
- Create: `tests/people_gallery/test_export_identity_v3_1_report_script.py`

## Parallel Execution Plan

### Wave A

- 可并行任务：无
- 执行任务：`Task 1`
- 阻塞任务：`Task 2`、`Task 3`、`Task 4`、`Task 5`
- 解锁条件：共享类型、参数契约和 phase1 export 夹具可用。

### Wave B

- 可并行任务：`Task 2`、`Task 3`
- 并行原因：`Task 1` 已先把 `models.py` 与 export fixture 的共享字段契约锁死，`Task 2` 只消费 `QueryContext` / `ObservationCandidateRecord` / `ClusterRecord` 的固定字段，`Task 3` 只消费同一批 dataclass 的内存输入；写入文件完全分离，分别只改查询层和 assign 层。
- 阻塞任务：`Task 4`、`Task 5`
- 解锁条件：`Task 2` 和 `Task 3` 都在不改共享 dataclass 字段名/字段含义的前提下通过。

### Wave C

- 可并行任务：无
- 执行任务：`Task 4`
- 阻塞任务：`Task 5`
- 解锁条件：导出服务已能生成完整 bundle，并通过 manifest / HTML / 资产测试。

### Wave D

- 可并行任务：无
- 执行任务：`Task 5`
- 阻塞任务：无
- 解锁条件：脚本入口、非零退出码和整套新测试子集全部通过。

## 任务详情

### Task 1: 实验包骨架与共享夹具

**Depends on:** None

**Scope Budget:**
- Max files: 20
- Estimated files touched: 5
- Max added lines: 1000
- Estimated added lines: 360

**Files:**
- Create: `src/hikbox_experiments/__init__.py`
- Create: `src/hikbox_experiments/identity_v3_1/__init__.py`
- Create: `src/hikbox_experiments/identity_v3_1/models.py`
- Create: `tests/people_gallery/fixtures_identity_v3_1_export.py`
- Create: `tests/people_gallery/test_identity_v3_1_export_fixtures.py`

- [x] **Step 1: 先写共享夹具契约测试，锁定后续任务可依赖的 phase1 数据拓扑**

在 `tests/people_gallery/test_identity_v3_1_export_fixtures.py` 先写失败测试，至少断言：

```python
def test_build_identity_v3_1_export_workspace_seeds_expected_topology(tmp_path: Path) -> None:
    ws = build_identity_v3_1_export_workspace(tmp_path / "identity-v3-1-export-fixture")
    try:
        assert ws.base_run_id > 0
        assert ws.latest_non_target_run_id > 0
        assert ws.failed_run_id > 0
        assert ws.snapshot_id > 0
        assert ws.cluster_ids["seed_primary"] > 0
        assert ws.cluster_ids["seed_fallback"] > 0
        assert ws.cluster_ids["seed_invalid"] > 0
        assert ws.cluster_ids["pending_promotable"] > 0
        assert ws.observation_ids["attachment_auto"] > 0
        assert ws.observation_ids["pending_attachment_overlap"] > 0
        assert ws.observation_ids["embedding_probe"] > 0
        assert ws.observation_ids["attachment_missing_embedding"] > 0
        assert ws.observation_ids["attachment_dim_mismatch"] > 0
        assert ws.cluster_ids["other_run_materialized"] > 0
        assert ws.observation_ids["other_snapshot_attachment"] > 0
        assert ws.observation_ids["warmup_active"] > 0
        assert ws.embedding_probe_expected_dim > 0
        assert ws.embedding_probe_expected_model_key
        assert ws.selected_snapshot_embedding_model_key == ws.embedding_probe_expected_model_key
        assert ws.latest_visible_profile_embedding_model_key
        assert ws.selected_snapshot_embedding_model_key != ws.latest_visible_profile_embedding_model_key
        assert ws.expected_cluster_ids_by_run_id[ws.base_run_id] == {
            ws.cluster_ids["seed_primary"],
            ws.cluster_ids["seed_fallback"],
            ws.cluster_ids["seed_invalid"],
            ws.cluster_ids["pending_promotable"],
        }
        assert ws.expected_cluster_ids_by_run_id[ws.latest_non_target_run_id]
        assert ws.expected_cluster_ids_by_run_id[ws.latest_non_target_run_id] != ws.expected_cluster_ids_by_run_id[
            ws.base_run_id
        ]
        assert (ws.base_run_id, "review_pending") in ws.expected_candidate_ids_by_run_id_and_source
        assert (ws.base_run_id, "attachment") in ws.expected_candidate_ids_by_run_id_and_source
        assert ws.expected_candidate_ids_by_run_id_and_source[(ws.latest_non_target_run_id, "all")] != ws.expected_candidate_ids_by_run_id_and_source[
            (ws.base_run_id, "all")
        ]
        assert ws.observation_ids["other_snapshot_attachment"] not in ws.expected_candidate_ids_by_run_id_and_source[
            (ws.base_run_id, "all")
        ]
        assert ws.observation_ids["warmup_active"] not in ws.expected_candidate_ids_by_run_id_and_source[
            (ws.base_run_id, "all")
        ]
        assert ws.photo_ids["attachment_same_photo_conflict"] == ws.photo_ids["seed_primary_a"]
        assert ws.photo_ids["attachment_auto"] != ws.photo_ids["seed_primary_a"]
    finally:
        ws.close()
```

- [x] **Step 2: 运行夹具契约测试，确认当前仓库还没有这套 export fixture**

Run:

```bash
source .venv/bin/activate
PYTHONPATH=src python -m pytest tests/people_gallery/test_identity_v3_1_export_fixtures.py -q
```

Expected: FAIL，报 `ModuleNotFoundError` 或 `ImportError`，因为 `fixtures_identity_v3_1_export.py` 和实验包尚未创建。

- [x] **Step 3: 新建共享 dataclass/参数契约，并实现可复用的 phase1 export fixture**

实现要求：

- `src/hikbox_experiments/identity_v3_1/models.py` 至少定义以下 dataclass：
  - `AssignParameters`
  - `BaseRunContext`
  - `SnapshotContext`
  - `TopCandidateRecord`
  - `ClusterMemberRecord`
  - `ClusterRecord`
  - `ObservationCandidateRecord`
  - `SeedIdentityRecord`
  - `SeedBuildResult`
  - `AssignmentRecord`
  - `AssignmentSummary`
  - `AssignmentEvaluation`
  - `QueryContext`
- 为了保证 Wave B 真并行，上述 dataclass 字段契约必须在 Task 1 一次写死，后续任务不得再临时扩字段改语义；至少固定为：
  - `BaseRunContext`：
    - `id`
    - `run_status`
    - `observation_snapshot_id`
    - `cluster_profile_id`
    - `is_review_target`
  - `SnapshotContext`：
    - `id`
    - `observation_profile_id`
    - `embedding_model_key`
  - `ClusterMemberRecord`：
    - `cluster_id`
    - `observation_id`
    - `photo_id`
    - `source_pool_kind`
    - `member_role`
    - `decision_status`
    - `is_selected_trusted_seed`
    - `is_representative`
    - `quality_score_snapshot`
    - `primary_path`
    - `embedding_vector`
    - `embedding_dim`
  - `TopCandidateRecord`：
    - `rank`
    - `identity_id`
    - `cluster_id`
    - `distance`
  - `ClusterRecord`：
    - `cluster_id`
    - `cluster_stage`
    - `cluster_state`
    - `resolution_state`
    - `representative_observation_id`
    - `retained_member_count`
    - `distinct_photo_count`
    - `representative_count`
    - `retained_count`
    - `excluded_count`
    - `members`
  - `ObservationCandidateRecord`：
    - `observation_id`
    - `photo_id`
    - `source_kind`
    - `source_cluster_id`
    - `primary_path`
    - `embedding_vector`
    - `embedding_dim`
    - `embedding_model_key`
  - `SeedIdentityRecord`：
    - `identity_id`
    - `source_cluster_id`
    - `resolution_state`
    - `seed_member_count`
    - `fallback_used`
    - `prototype_dimension`
    - `representative_observation_id`
    - `member_observation_ids`
    - `valid`
    - `error_code`
    - `error_message`
    - `prototype_vector`
  - `SeedBuildResult`：
    - `valid_seeds_by_cluster`
    - `invalid_seeds`
    - `errors`
    - `prototype_dimension`
  - `AssignmentRecord`：
    - `observation_id`
    - `photo_id`
    - `source_kind`
    - `source_cluster_id`
    - `best_identity_id`
    - `best_cluster_id`
    - `best_distance`
    - `second_best_distance`
    - `distance_margin`
    - `same_photo_conflict`
    - `decision`
    - `reason_code`
    - `top_candidates`
    - `assets`
    - `missing_assets`
  - `AssignmentSummary`：
    - `candidate_count`
    - `auto_assign_count`
    - `review_count`
    - `reject_count`
    - `same_photo_conflict_count`
    - `missing_embedding_count`
    - `dimension_mismatch_count`
  - `AssignmentEvaluation`：
    - `assignments`
    - `by_observation_id`
    - `excluded_seed_observation_ids`
    - `summary`
  - `QueryContext`：
    - `base_run`
    - `snapshot`
    - `clusters`
    - `clusters_by_id`
    - `candidate_observations`
    - `non_rejected_member_observation_ids_by_cluster`
    - `source_candidate_observation_ids`
    - `warnings`
- `SeedBuildResult.invalid_seeds` 的元素类型必须和 `seed_identities` 一致，统一使用 `SeedIdentityRecord(valid=False, ...)`；`errors` 中再追加同 cluster 的错误对象，避免后续层面对 invalid seed 出现两套不同输出形状。
- `AssignParameters.validate()` 要直接完成参数硬校验：

```python
ASSIGN_SOURCE_CHOICES = ("all", "review_pending", "attachment")

@dataclass(frozen=True)
class AssignParameters:
    top_k: int = 5
    auto_max_distance: float = 0.25
    review_max_distance: float = 0.35
    min_margin: float = 0.08
    assign_source: str = "all"

    def validate(self) -> "AssignParameters":
        if self.assign_source not in ASSIGN_SOURCE_CHOICES:
            raise ValueError(f"不支持的 assign_source: {self.assign_source}")
        if self.top_k <= 0:
            raise ValueError("top_k 必须大于 0")
        if self.auto_max_distance > self.review_max_distance:
            raise ValueError("auto_max_distance 不能大于 review_max_distance")
        if self.min_margin < 0:
            raise ValueError("min_margin 不能小于 0")
        return self
```

- `tests/people_gallery/fixtures_identity_v3_1_export.py` 必须复用现有 `build_identity_seed_workspace()`，不要重写 migration 或 workspace 初始化。
- 新夹具必须显式构造以下 deterministic 数据：
  - 一个 `succeeded + is_review_target = 1` 的 base run，其 `identity_observation_snapshot -> identity_observation_profile.embedding_model_key` 固定为 `selected_snapshot_embedding_model_key`
  - 一个更新但非 review target 的 `succeeded` run，其 snapshot/profile 使用不同的 `latest_visible_profile_embedding_model_key`，并通过插入顺序或更高 id 保证它在 workspace 内是“另一条可见且更晚的 profile”，用来验证 QueryService 不会偷读 latest/global profile
  - 上述两个 succeeded run 的允许 `cluster` 集或允许 `candidate` 集必须至少有一组真实差异，保证 export 层能通过真实 bundle 对比锁定 `base_run_id` 已生效
  - 一个 `failed` run，用来验证失败路径
  - 一个 `materialized` primary seed cluster：3 个 retained 成员、2 个 `is_selected_trusted_seed = 1`
  - 一个 `materialized` fallback seed cluster：2 个 retained 成员、0 个 `is_selected_trusted_seed = 1`
  - 一个 `materialized` invalid seed cluster：会进入默认 seed 集，但其成员无法产出 prototype，用来覆盖“invalid seed 软失败”与“仅剩 invalid seed 时硬失败”
  - 一个 `review_pending` cluster：2 个 retained 成员，既能默认充当 assign candidate，也能通过 `--promote-cluster-ids` 变成 seed
  - 一个 `pending_attachment_overlap` observation：同一 observation 同时是 `review_pending` cluster 的 retained 成员、又在 snapshot 中是 `pool_kind='attachment'`，用来锁定双来源去重
  - 一个 `other_run_materialized` cluster：挂在非选中 run 上，状态本身合法，但必须因为 `run_id` 不匹配而被 QueryService 排除
  - 一个 `other_snapshot_attachment` observation：挂在非选中 run 对应的 snapshot 上，来源本身合法，但必须因为 `snapshot_id` 不匹配而被 QueryService 排除
  - 一个 `warmup_active` observation：保留在 workspace 里处于 active，但不在 selected snapshot，也不属于 selected run 的 `review_pending` cluster，用来防止实现者偷扫 workspace 级 active observation
  - 一个 `discarded` final cluster，用来证明查询层会过滤掉它
  - 一个 `embedding_probe` observation：在测试夹具专用 DB 中为同一 `face_observation_id` 人工插入三条 `face_embedding` 行，用来锁定 QueryService 只能接受正确那条：
    - `feature_type='face'` + 正确 `model_key` + `normalized=1`
    - 错误 `model_key`
    - `normalized=0`
  - 6 个 attachment observation：
    - `attachment_auto`
    - `attachment_review_margin`
    - `attachment_same_photo_conflict`
    - `attachment_reject`
    - `attachment_missing_embedding`
    - `attachment_dim_mismatch`
- 夹具里的 embedding helper 必须把传入向量正规化后再写入 `face_embedding.normalized = 1`，避免“flag 写成 normalized，但向量本身不是 normalized”的测试伪像。
- `embedding_probe_expected_model_key` 必须等于 `selected_snapshot_embedding_model_key`，且显式不同于 `latest_visible_profile_embedding_model_key`；这样 Task 2 可以直接断言 QueryService 绑定的是 selected snapshot/profile 的 model key。
- 由于正式 schema 的 `face_embedding` 带有 `UNIQUE(face_observation_id, feature_type)`，`embedding_probe` 只允许在测试夹具 DB 内通过“重建 `face_embedding` 表副本去掉唯一约束，再回填同 observation 的三条 row”来构造；该重建只存在于测试夹具，不得影响产品 migration 或运行时 schema。
- 夹具 dataclass 至少公开：
  - `root`
  - `conn`
  - `base_run_id`
  - `latest_non_target_run_id`
  - `failed_run_id`
  - `snapshot_id`
  - `cluster_ids`
  - `observation_ids`
  - `photo_ids`
  - `embedding_probe_expected_dim`
  - `embedding_probe_expected_model_key`
  - `selected_snapshot_embedding_model_key`
  - `latest_visible_profile_embedding_model_key`
  - `expected_cluster_ids_by_run_id`
  - `expected_candidate_ids_by_run_id_and_source`
  - `close()`

- [x] **Step 4: 回跑共享夹具测试，确认后续任务可以复用同一套 deterministic 数据**

Run:

```bash
source .venv/bin/activate
PYTHONPATH=src python -m pytest tests/people_gallery/test_identity_v3_1_export_fixtures.py -q
```

Expected: PASS。

### Task 2: phase1 只读查询层

**Depends on:** Task 1

**Scope Budget:**
- Max files: 20
- Estimated files touched: 2
- Max added lines: 1000
- Estimated added lines: 320

**Files:**
- Create: `src/hikbox_experiments/identity_v3_1/query_service.py`
- Create: `tests/people_gallery/test_identity_v3_1_query_service.py`

- [x] **Step 1: 先写查询层测试，锁定 run 选择、cluster 过滤和 candidate 来源聚合规则**

在 `tests/people_gallery/test_identity_v3_1_query_service.py` 先写失败测试，至少覆盖：

```python
def test_query_service_defaults_to_review_target_run_not_latest_run(tmp_path: Path) -> None:
    ws = build_identity_v3_1_export_workspace(tmp_path / "query-default-run")
    try:
        payload = IdentityV31QueryService(ws.root).load_report_context(
            base_run_id=None,
            assign_parameters=AssignParameters(),
        )
        assert payload.base_run.id == ws.base_run_id
    finally:
        ws.close()

def test_query_service_fails_without_review_target_when_base_run_id_omitted(tmp_path: Path) -> None:
    ws = build_identity_v3_1_export_workspace(tmp_path / "query-no-review-target")
    try:
        ws.conn.execute("UPDATE identity_cluster_run SET is_review_target = 0")
        ws.conn.commit()
        with pytest.raises(ValueError, match="默认 review target run 不存在"):
            IdentityV31QueryService(ws.root).load_report_context(
                base_run_id=None,
                assign_parameters=AssignParameters(),
            )
    finally:
        ws.close()

def test_query_service_fails_when_workspace_config_is_broken(tmp_path: Path) -> None:
    ws = build_identity_v3_1_export_workspace(tmp_path / "query-broken-workspace")
    try:
        config_path = ws.root / ".hikbox" / "config.json"
        config_path.write_text('{"version": 1, "external_root": ""}\n', encoding="utf-8")
        with pytest.raises(ValueError, match="workspace 配置缺少 external_root"):
            IdentityV31QueryService(ws.root).load_report_context(
                base_run_id=None,
                assign_parameters=AssignParameters(),
            )
    finally:
        ws.close()

def test_query_service_rejects_missing_or_non_succeeded_run(tmp_path: Path) -> None:
    ws = build_identity_v3_1_export_workspace(tmp_path / "query-run-validation")
    try:
        service = IdentityV31QueryService(ws.root)
        with pytest.raises(ValueError, match="cluster run 不存在"):
            service.load_report_context(base_run_id=999999, assign_parameters=AssignParameters())
        with pytest.raises(ValueError, match="run_status 必须为 succeeded"):
            service.load_report_context(base_run_id=ws.failed_run_id, assign_parameters=AssignParameters())
    finally:
        ws.close()

def test_query_service_honors_explicit_base_run_id_and_calls_validate(tmp_path: Path, monkeypatch) -> None:
    ws = build_identity_v3_1_export_workspace(tmp_path / "query-explicit-run")
    calls = {"validate": 0}

    def _validate(self):
        calls["validate"] += 1
        return self

    monkeypatch.setattr(AssignParameters, "validate", _validate)
    try:
        payload = IdentityV31QueryService(ws.root).load_report_context(
            base_run_id=ws.latest_non_target_run_id,
            assign_parameters=AssignParameters(assign_source="attachment", top_k=7),
        )
        assert payload.base_run.id == ws.latest_non_target_run_id
        assert calls["validate"] == 1
    finally:
        ws.close()

def test_query_service_dedupes_overlap_candidate_and_prefers_review_pending_source(tmp_path: Path) -> None:
    ws = build_identity_v3_1_export_workspace(tmp_path / "query-overlap-dedupe")
    try:
        payload = IdentityV31QueryService(ws.root).load_report_context(
            base_run_id=None,
            assign_parameters=AssignParameters(assign_source="all"),
        )
        overlap_items = [
            item for item in payload.candidate_observations
            if item.observation_id == ws.observation_ids["pending_attachment_overlap"]
        ]
        assert len(overlap_items) == 1
        assert overlap_items[0].source_kind == "review_pending_retained"
    finally:
        ws.close()

def test_query_service_returns_exact_cluster_and_candidate_scope_for_selected_run(tmp_path: Path) -> None:
    ws = build_identity_v3_1_export_workspace(tmp_path / "query-scope-lock")
    try:
        payload = IdentityV31QueryService(ws.root).load_report_context(
            base_run_id=None,
            assign_parameters=AssignParameters(assign_source="all"),
        )
        assert {cluster.cluster_id for cluster in payload.clusters} == ws.expected_cluster_ids_by_run_id[ws.base_run_id]
        assert {item.observation_id for item in payload.candidate_observations} == ws.expected_candidate_ids_by_run_id_and_source[
            (ws.base_run_id, "all")
        ]
        assert ws.cluster_ids["other_run_materialized"] not in {cluster.cluster_id for cluster in payload.clusters}
        assert ws.observation_ids["other_snapshot_attachment"] not in {
            item.observation_id for item in payload.candidate_observations
        }
        assert ws.observation_ids["warmup_active"] not in {
            item.observation_id for item in payload.candidate_observations
        }
    finally:
        ws.close()

def test_query_service_uses_selected_snapshot_profile_embedding_model_key(tmp_path: Path) -> None:
    ws = build_identity_v3_1_export_workspace(tmp_path / "query-snapshot-profile-binding")
    try:
        default_payload = IdentityV31QueryService(ws.root).load_report_context(
            base_run_id=None,
            assign_parameters=AssignParameters(),
        )
        latest_payload = IdentityV31QueryService(ws.root).load_report_context(
            base_run_id=ws.latest_non_target_run_id,
            assign_parameters=AssignParameters(),
        )
        assert default_payload.snapshot.embedding_model_key == ws.selected_snapshot_embedding_model_key
        assert default_payload.snapshot.embedding_model_key != ws.latest_visible_profile_embedding_model_key
        assert latest_payload.snapshot.embedding_model_key == ws.latest_visible_profile_embedding_model_key
        assert latest_payload.snapshot.embedding_model_key != ws.selected_snapshot_embedding_model_key
    finally:
        ws.close()

def test_query_service_selects_only_matching_normalized_embedding_row(tmp_path: Path) -> None:
    ws = build_identity_v3_1_export_workspace(tmp_path / "query-embedding-filter")
    try:
        payload = IdentityV31QueryService(ws.root).load_report_context(
            base_run_id=None,
            assign_parameters=AssignParameters(),
        )
        probe = next(
            item for item in payload.candidate_observations
            if item.observation_id == ws.observation_ids["embedding_probe"]
        )
        assert probe.embedding_vector is not None
        assert probe.embedding_dim == ws.embedding_probe_expected_dim
        assert probe.embedding_model_key == ws.embedding_probe_expected_model_key
    finally:
        ws.close()
```

还要断言：

- `base_run_id=ws.latest_non_target_run_id` 时，明确覆盖默认 review target，且返回该 run
- `base_run_id=None` 且没有任何 `is_review_target = 1` + `run_status='succeeded'` run 时，`load_report_context()` 直接抛出 `ValueError("默认 review target run 不存在")`
- workspace `.hikbox/config.json` 损坏或缺 `external_root` 时，`load_report_context()` 直接透传 `load_workspace_paths()` 的 `FileNotFoundError` / `ValueError`
- 查询出的 `cluster_id` 集必须精确等于 `selected base_run` 下 `cluster_stage='final'`、`cluster_state='active'`、`resolution_state in ('materialized', 'review_pending')` 的 cluster 集；不得混入非选中 run 的合法 cluster
- `assign_source='all'` 时同时存在 `review_pending_retained` 与 `attachment` 候选
- `assign_source='review_pending'` 与 `assign_source='attachment'` 时能分别收敛到单一来源
- `candidate observation_id` 集必须精确等于 `selected base_run.observation_snapshot_id` 下允许来源集合：
  - `all` = `review_pending retained` 与 selected snapshot `attachment` 的并集去重
  - `review_pending` = selected run 的 `review_pending retained`
  - `attachment` = selected snapshot 的 `attachment`
  不允许从 workspace 级 `face_observation.active = 1` 扫描，也不允许混入 `warmup_active` 或非选中 snapshot 的 observation
- `payload.snapshot.embedding_model_key` 必须等于 `selected base_run -> observation_snapshot -> observation_profile.embedding_model_key`；测试会用 `latest_visible_profile_embedding_model_key` 证明它不能偷读 workspace 里更晚/更可见的 profile key
- 对 `pending_attachment_overlap`，QueryService 只保留一条 candidate，且 `source_kind == 'review_pending_retained'`
- `load_report_context()` 会显式调用 `AssignParameters.validate()`；测试中用 `monkeypatch` 替换该方法并断言调用发生
- 传入 `AssignParameters(assign_source='bogus')`、`AssignParameters(top_k=0)`、`AssignParameters(auto_max_distance=0.5, review_max_distance=0.4)`、`AssignParameters(min_margin=-0.1)` 时，`load_report_context()` 直接抛出对应 `ValueError`
- 缺 embedding 和维度不一致 observation 会保留在 `QueryContext` 中，并带上 `embedding_dim` / `embedding_vector` 的缺失信息，交给 assign 层做软失败统计
- 对 `embedding_probe`，测试必须证明 QueryService 只接受 `feature_type='face'`、正确 `model_key`、`normalized=1` 的那一条 row；错误 `model_key` 和 `normalized=0` 的 row 必须被忽略
- QueryContext 必须显式暴露每个 cluster 下 `decision_status != 'rejected'` 的 member observation id，供 Task 3 做“已属于启用 seed 的 observation 排除”；这一层不要提前应用该排除，因为 promote/disable 还未决

- [x] **Step 2: 运行查询层测试，确认服务尚未实现**

Run:

```bash
source .venv/bin/activate
PYTHONPATH=src python -m pytest tests/people_gallery/test_identity_v3_1_query_service.py -q
```

Expected: FAIL，报 `ModuleNotFoundError`、`ImportError` 或 `AttributeError`。

- [x] **Step 3: 用直接 SQL 实现 `IdentityV31QueryService`，不要为了这个实验工具改 repository 层**

实现要求：

- 新服务入口固定为：
  - `IdentityV31QueryService.__init__(workspace: Path)`
  - `IdentityV31QueryService.load_report_context(*, base_run_id: int | None, assign_parameters: AssignParameters) -> QueryContext`

- `load_report_context()` 内部分四段查询：
  1. 用 `load_workspace_paths()` 校验 workspace，并解析 base run
  2. 从 `identity_observation_snapshot -> identity_observation_profile` 读取 `embedding_model_key`
  3. 读取 final cluster / resolution / member，并构造 `clusters_by_id`
  4. 读取默认 assign candidates：`review_pending retained` + `attachment`
- `load_report_context()` 一开始就必须执行 `assign_parameters = assign_parameters.validate()`；不要把参数校验留给下游层隐式兜底。
- 当 `base_run_id is None` 时，必须显式查询 `is_review_target = 1 AND run_status = 'succeeded'` 的 run；如果没有结果，直接抛出 `ValueError("默认 review target run 不存在")`，不要偷偷回退到“最新 run”。
- `load_workspace_paths()` 抛出的 `FileNotFoundError` / `ValueError` 必须原样向外暴露，用于锁定 workspace 配置损坏的失败路径。
- cluster 查询必须显式 `WHERE identity_cluster_run_id = selected_base_run.id`；candidate 查询必须显式锚定 `selected_base_run.observation_snapshot_id` 与 selected run 的 `review_pending` 成员，禁止写成“扫全库 active observation 再 Python 过滤”。
- `embedding_model_key` 的来源必须显式绑定到 `selected_base_run.observation_snapshot_id -> identity_observation_snapshot.observation_profile_id -> identity_observation_profile.embedding_model_key`；禁止读取 workspace 级 active profile、最新 profile 或其他全局 profile。
- embedding 查询必须 `LEFT JOIN face_embedding`，并强制：
  - `feature_type = 'face'`
  - `model_key = observation_profile.embedding_model_key`
  - `normalized = 1`
- 由于夹具会为 `embedding_probe` observation 造出多条 `face_embedding` row，过滤条件必须直接写在 SQL `JOIN` / `WHERE` 中；禁止“先把所有 embedding 拉出来，再在 Python 里随便取第一条”。
- candidate 去重时，来源优先级固定为：

```python
source_priority = {
    "review_pending_retained": 0,
    "attachment": 1,
}
```

- 去重必须发生在“review_pending retained 候选集”和“attachment 候选集”做并集之后，按 `observation_id` 保留一条；若发生重叠，只允许留下来源优先级更高的那条。

- cluster 排序固定为：
  - `materialized` 在前
  - 同状态下按 `retained_member_count DESC`
  - 最后按 `cluster_id ASC`
- member 必须把这些字段完整带出来，供 seed 与 HTML 共用：
  - `cluster_id`
  - `observation_id`
  - `photo_id`
  - `source_pool_kind`
  - `member_role`
  - `decision_status`
  - `is_selected_trusted_seed`
  - `is_representative`
  - `quality_score_snapshot`
  - `primary_path`
  - `embedding_vector`
  - `embedding_dim`
- QueryContext 还必须显式暴露：
  - `non_rejected_member_observation_ids_by_cluster`
  - `source_candidate_observation_ids`
  让 Task 3 可以在 seed 集合确定后，基于启用 seed 的非 rejected 成员做最终 candidate 排除。

- [x] **Step 4: 回跑查询层测试，确认 run 选择、过滤和来源聚合稳定**

Run:

```bash
source .venv/bin/activate
PYTHONPATH=src python -m pytest tests/people_gallery/test_identity_v3_1_query_service.py -q
```

Expected: PASS。

### Task 3: seed prototype 与 assign 判定层

**Depends on:** Task 1

**Scope Budget:**
- Max files: 20
- Estimated files touched: 2
- Max added lines: 1000
- Estimated added lines: 360

**Files:**
- Create: `src/hikbox_experiments/identity_v3_1/assignment_service.py`
- Create: `tests/people_gallery/test_identity_v3_1_assignment_service.py`

- [x] **Step 1: 先写 assign 层测试，锁定 trusted-seed 优先级、L2 margin 和同图冲突判定**

在 `tests/people_gallery/test_identity_v3_1_assignment_service.py` 先写失败测试。测试不要依赖 DB 查询层，而是直接使用 `models.py` 的 dataclass 构造最小输入，这样本任务可与查询层并行：

```python
def test_build_seed_identities_prefers_trusted_seed_and_falls_back_to_retained_members() -> None:
    service = IdentityV31AssignmentService()
    seed_result = service.build_seed_identities(
        clusters=[make_primary_cluster(), make_fallback_cluster()],
        promote_cluster_ids=set(),
        disable_seed_cluster_ids=set(),
    )
    assert seed_result.valid_seeds_by_cluster[101].fallback_used is False
    assert seed_result.valid_seeds_by_cluster[202].fallback_used is True
    assert np.allclose(
        seed_result.valid_seeds_by_cluster[101].prototype_vector,
        normalize(np.mean([trusted_seed_vector_a(), trusted_seed_vector_b()], axis=0)),
        atol=1e-6,
    )
    assert np.allclose(
        seed_result.valid_seeds_by_cluster[202].prototype_vector,
        normalize(np.mean([fallback_member_vector_a(), fallback_member_vector_b()], axis=0)),
        atol=1e-6,
    )

def test_assign_candidates_produces_auto_review_reject_and_reason_codes() -> None:
    service = IdentityV31AssignmentService()
    clusters = [make_primary_cluster(), make_fallback_cluster()]
    seed_result = service.build_seed_identities(
        clusters=clusters,
        promote_cluster_ids=set(),
        disable_seed_cluster_ids=set(),
    )
    evaluation = service.evaluate_assignments(
        query_context=make_query_context(clusters=clusters),
        seed_result=seed_result,
        assign_parameters=AssignParameters(top_k=3, auto_max_distance=0.25, review_max_distance=0.35, min_margin=0.08),
    )
    assert evaluation.summary.auto_assign_count == 1
    assert evaluation.summary.review_count == 2
    assert evaluation.summary.reject_count == 1
    assert evaluation.by_observation_id[3001].best_distance < 0.25
    assert evaluation.by_observation_id[3002].distance_margin == pytest.approx(0.03, abs=1e-6)
    assert evaluation.by_observation_id[3002].reason_code == "same_photo_conflict"
    assert evaluation.by_observation_id[3003].reason_code == "distance_above_review_threshold"
    assert [item.rank for item in evaluation.by_observation_id[3002].top_candidates] == [1, 2]
    assert [item.cluster_id for item in evaluation.by_observation_id[3002].top_candidates] == [101, 202]
    assert [item.distance for item in evaluation.by_observation_id[3002].top_candidates] == pytest.approx(
        [0.19, 0.22],
        abs=1e-6,
    )

def test_assign_candidates_sets_second_best_distance_to_inf_when_only_one_seed_candidate() -> None:
    service = IdentityV31AssignmentService()
    clusters = [make_primary_cluster()]
    seed_result = service.build_seed_identities(
        clusters=clusters,
        promote_cluster_ids=set(),
        disable_seed_cluster_ids=set(),
    )
    evaluation = service.evaluate_assignments(
        query_context=make_query_context(clusters=clusters, candidate_mode="single-seed"),
        seed_result=seed_result,
        assign_parameters=AssignParameters(top_k=5),
    )
    assignment = evaluation.by_observation_id[3901]
    assert assignment.second_best_distance == float("inf")
    assert len(assignment.top_candidates) == 1
    assert assignment.top_candidates[0].rank == 1
    assert assignment.top_candidates[0].cluster_id == 101

def test_build_seed_identities_rejects_illegal_overrides_and_records_invalid_seed() -> None:
    service = IdentityV31AssignmentService()
    with pytest.raises(ValueError, match="promote cluster 不存在"):
        service.build_seed_identities(clusters=make_clusters(), promote_cluster_ids={999999}, disable_seed_cluster_ids=set())
    with pytest.raises(ValueError, match="只能 promote review_pending cluster"):
        service.build_seed_identities(clusters=make_clusters(), promote_cluster_ids={101}, disable_seed_cluster_ids=set())
    with pytest.raises(ValueError, match="disable 目标不是启用 seed cluster"):
        service.build_seed_identities(clusters=make_clusters(), promote_cluster_ids=set(), disable_seed_cluster_ids={303})

    seed_result = service.build_seed_identities(
        clusters=make_clusters_with_invalid_seed(),
        promote_cluster_ids=set(),
        disable_seed_cluster_ids=set(),
    )
    assert seed_result.invalid_seeds
    assert seed_result.invalid_seeds[0].source_cluster_id == 404
    assert seed_result.invalid_seeds[0].valid is False
    assert seed_result.errors[0]["cluster_id"] == 404
    assert seed_result.errors[0]["code"] == "invalid_seed_prototype"

def test_build_seed_identities_fails_when_all_enabled_seeds_are_invalid() -> None:
    service = IdentityV31AssignmentService()
    with pytest.raises(ValueError, match="没有任何可用 seed identity"):
        service.build_seed_identities(
            clusters=make_clusters_with_invalid_seed(),
            promote_cluster_ids=set(),
            disable_seed_cluster_ids={101, 202},
        )
```

还要覆盖：

- 测试 helper 的返回形状必须直接对齐 Task 1 已锁死的 dataclass 字段契约，避免 Task 2/3 并行时各自发明局部字段：
  - `make_primary_cluster()`、`make_fallback_cluster()`、`make_clusters_with_invalid_seed()` 返回的 `ClusterRecord` 必须完整填充 `cluster_stage`、`cluster_state`、`resolution_state`、`representative_observation_id`、计数字段和 `members`
  - `make_query_context()` 必须完整填充 `base_run`、`snapshot`、`clusters`、`clusters_by_id`、`candidate_observations`、`non_rejected_member_observation_ids_by_cluster`、`source_candidate_observation_ids`、`warnings`
  - `make_seed_result()` 必须完整填充 `valid_seeds_by_cluster`、`invalid_seeds`、`errors`、`prototype_dimension`
- 测试 helper 的向量必须专门选成“representative 向量、首成员向量、未归一化均值、归一化均值”彼此不同，确保实现者不能用 representative、首个成员或未归一化均值混过 prototype 公式断言
- 至少有一条测试路径必须使用真实 `build_seed_identities()` 产出的 `seed_result` 再喂给 `evaluate_assignments()`，禁止只用手工伪造 `SeedBuildResult` 避开 prototype 计算

- `same_photo_conflict` 会检查最佳 seed cluster 中全部 `decision_status != 'rejected'` 成员，而不只看 retained；测试要专门构造一个 `deferred` 成员与候选 observation 同图，断言仍命中冲突
- `top_candidates` 的测试必须锁死：
  - 长度 = `min(top_k, valid_seed_count)`
  - 顺序 = `distance ASC, cluster_id ASC`
  - 距离值 = 真实 L2 距离，不允许写占位或复用 `best_distance` / `second_best_distance`
- 单候选场景下，`second_best_distance == float("inf")`，且 `top_candidates` 只保留一条 rank=1 记录
- `--promote-cluster-ids` 会增加 seed 数量
- `--disable-seed-cluster-ids` 会减少 seed 数量
- promote 未知 cluster id 直接失败
- promote 非 `review_pending` cluster 直接失败
- disable 未启用 seed cluster 直接失败
- 当 `review_pending` cluster 被 promote 为 seed 时，它自己的 `decision_status != 'rejected'` 成员会从最终 assignments 中被排除，并且不计入 `candidate_count`
- invalid seed cluster 会被记录到 `invalid_seeds` / `errors`
- invalid seed cluster 会被记录到 `invalid_seeds` 与 `errors` 两处，且 `invalid_seeds` 的元素必须仍是 `SeedIdentityRecord(valid=False, ...)`
- 禁用所有 seed 时抛出 `ValueError("没有任何可用 seed identity")`
- 当所有启用 seed 都是 invalid seed 时，也抛出 `ValueError("没有任何可用 seed identity")`
- 当候选集合非空但全部 observation 都缺 embedding 时抛出 `ValueError("所有候选 observation 都缺少可用 embedding")`
- `missing_embedding_count` / `dimension_mismatch_count` 会增加，但这些 observation 不计入 `candidate_count`

- [x] **Step 2: 运行 assign 层测试，确认当前仓库还没有该服务**

Run:

```bash
source .venv/bin/activate
PYTHONPATH=src python -m pytest tests/people_gallery/test_identity_v3_1_assignment_service.py -q
```

Expected: FAIL，报 `ModuleNotFoundError` 或 `AttributeError`。

- [x] **Step 3: 实现 `IdentityV31AssignmentService`，让 seed 与 assign 完全基于内存计算**

实现要求：

- 公开两个明确入口：
  - `IdentityV31AssignmentService.build_seed_identities(*, clusters: list[ClusterRecord], promote_cluster_ids: set[int], disable_seed_cluster_ids: set[int]) -> SeedBuildResult`
  - `IdentityV31AssignmentService.evaluate_assignments(*, query_context: QueryContext, seed_result: SeedBuildResult, assign_parameters: AssignParameters) -> AssignmentEvaluation`

- `build_seed_identities()` 必须：
  - 先校验 override：
    - promote 目标不存在则失败
    - promote 目标不是 `review_pending` final cluster 则失败
    - disable 目标不在默认 seed 或 promote 后 seed 集合内则失败
  - 优先挑 `is_selected_trusted_seed = 1`
  - 否则回退到 `decision_status != 'rejected'`
  - 对均值向量再做一次归一化
  - 计算时禁止退化成 representative、首个成员或未归一化均值；测试会用刻意区分的向量验证这一点
  - 把无有效 embedding 或无法产出 prototype 的 cluster 记录到 `invalid_seeds`，元素仍为 `SeedIdentityRecord(valid=False, error_code="invalid_seed_prototype", error_message=...)`
  - 同时在 `errors` 里追加 `{"code": "invalid_seed_prototype", "cluster_id": <id>, "message": <message>}`，供导出层和 manifest 直接复用
  - 如果 override 应用后的启用 seed 集合非空，但去掉 `invalid_seeds` 后一个有效 seed 都不剩，抛出 `ValueError("没有任何可用 seed identity")`
- `evaluate_assignments()` 必须：
  - 先执行 `assign_parameters = assign_parameters.validate()`
  - 基于 `query_context.non_rejected_member_observation_ids_by_cluster` 计算 `enabled_seed_observation_ids`
  - 在任何 embedding/dimension 判定之前，先排除所有已属于启用 seed 的 observation
  - 先基于 `seed_result.prototype_dimension` 过滤 candidate
  - 用 L2 暴力召回 top-k
  - 计算 `best_distance`、`second_best_distance`、`distance_margin`
  - 生成 `top_candidates` 时必须输出排序后的完整对象数组，元素形状固定为 `TopCandidateRecord(rank, identity_id, cluster_id, distance)`；长度固定为 `min(assign_parameters.top_k, valid_seed_count)`，顺序固定为 `distance ASC, cluster_id ASC`
  - 当有效候选数小于 2 时，`second_best_distance = float("inf")`
  - 用最佳 seed cluster 的 `decision_status != 'rejected'` 成员 photo 集合判定 `same_photo_conflict`
  - 按固定优先级生成 `reason_code`
  - 当候选集合非空且全部 observation 都因缺 embedding 被跳过时，抛出 `ValueError("所有候选 observation 都缺少可用 embedding")`
- 排序必须稳定：

```python
order = np.lexsort((seed_cluster_ids_array, distance_array))
```

- `AssignmentSummary` 必须至少包含：
  - `candidate_count`
  - `auto_assign_count`
  - `review_count`
  - `reject_count`
  - `same_photo_conflict_count`
  - `missing_embedding_count`
  - `dimension_mismatch_count`
- `AssignmentEvaluation` 必须显式暴露：
  - `excluded_seed_observation_ids`
  - `by_observation_id`
  让 Task 4 能断言这些 observation 不会进入 `assignments` 和 `candidate_count`
- `SeedBuildResult` 必须显式暴露：
  - `invalid_seeds`
  - `errors`
  - `valid_seeds_by_cluster`
  - `prototype_dimension`
  让 Task 4 能把 valid/invalid seed 按同一契约传到页面摘要、overrides 和 manifest。

- [x] **Step 4: 回跑 assign 层测试，确认 prototype、margin、same-photo 决策都已固定**

Run:

```bash
source .venv/bin/activate
PYTHONPATH=src python -m pytest tests/people_gallery/test_identity_v3_1_assignment_service.py -q
```

Expected: PASS。

### Task 4: 离线 bundle 导出层

**Depends on:** Task 2, Task 3

**Scope Budget:**
- Max files: 20
- Estimated files touched: 2
- Max added lines: 1000
- Estimated added lines: 620

**Files:**
- Create: `src/hikbox_experiments/identity_v3_1/export_service.py`
- Create: `tests/people_gallery/test_identity_v3_1_export_service.py`

- [ ] **Step 1: 先写导出服务测试，锁定 bundle 结构、manifest 字段和 HTML 区块**

在 `tests/people_gallery/test_identity_v3_1_export_service.py` 先写失败测试，至少覆盖：

```python
def test_export_service_writes_index_manifest_and_assets(tmp_path: Path) -> None:
    ws = build_identity_v3_1_export_workspace(tmp_path / "export-service")
    try:
        result = IdentityV31ReportExportService(ws.root).export(output_root=tmp_path / "bundle")
        output_dir = Path(result["output_dir"])
        assert (output_dir / "index.html").is_file()
        assert (output_dir / "manifest.json").is_file()
        assert (output_dir / "assets").is_dir()
        assert output_dir.parent == (tmp_path / "bundle").resolve()
        assert output_dir != (tmp_path / "bundle").resolve()
    finally:
        ws.close()

def test_export_service_copies_real_preview_artifacts_and_renders_details_sections(tmp_path: Path) -> None:
    ws = build_identity_v3_1_export_workspace(tmp_path / "export-assets")
    tracker = TrackingPreviewArtifactService(
        db_path=ws.root / ".hikbox" / "library.db",
        workspace=ws.root,
    )
    try:
        result = IdentityV31ReportExportService(
            ws.root,
            preview_artifact_service=tracker,
        ).export(output_root=tmp_path / "bundle")
        output_dir = Path(result["output_dir"])
        html_text = (output_dir / "index.html").read_text(encoding="utf-8")
        assert '<details id="summary"' in html_text
        assert '<details id="seed-identities"' in html_text
        assert '<details id="overrides"' in html_text
        assert '<details id="review-pending-clusters"' in html_text
        assert '<details id="bucket-auto-assign"' in html_text
        assert '<details id="bucket-review"' in html_text
        assert '<details id="bucket-reject"' in html_text

        obs_id = ws.observation_ids["attachment_auto"]
        photo_id = ws.photo_ids["attachment_auto"]
        bundle_dir = output_dir / f"assets/observations/obs-{obs_id}"
        assert (bundle_dir / "crop.jpg").read_bytes() == tracker.crop_paths[obs_id].read_bytes()
        assert (bundle_dir / "context.jpg").read_bytes() == tracker.context_paths[obs_id].read_bytes()
        assert (bundle_dir / "preview.jpg").read_bytes() == tracker.preview_paths_by_photo_id[photo_id].read_bytes()
    finally:
        ws.close()

def test_export_service_serializes_top_candidates_and_summary_counters(tmp_path: Path) -> None:
    ws = build_identity_v3_1_export_workspace(tmp_path / "export-top-candidates")
    try:
        result = IdentityV31ReportExportService(ws.root).export(output_root=tmp_path / "bundle")
        output_dir = Path(result["output_dir"])
        manifest = json.loads((output_dir / "manifest.json").read_text(encoding="utf-8"))
        assignment = next(
            item for item in manifest["assignments"]
            if item["observation_id"] == ws.observation_ids["attachment_review_margin"]
        )
        assert len(assignment["top_candidates"]) >= 2
        assert assignment["top_candidates"][0]["rank"] == 1
        assert assignment["top_candidates"][0]["cluster_id"] != assignment["top_candidates"][1]["cluster_id"]
        assert assignment["top_candidates"][0]["distance"] <= assignment["top_candidates"][1]["distance"]
        assert manifest["assignment_summary"]["same_photo_conflict_count"] >= 1
        assert manifest["assignment_summary"]["missing_embedding_count"] >= 1
        assert manifest["assignment_summary"]["dimension_mismatch_count"] >= 1

        html_text = (output_dir / "index.html").read_text(encoding="utf-8")
        assert 'data-top-candidate-rank="1"' in html_text
        assert 'data-top-candidate-rank="2"' in html_text
        assert f'data-cluster-id="{assignment["top_candidates"][0]["cluster_id"]}"' in html_text
        assert f'data-distance="{assignment["top_candidates"][0]["distance"]:.6f}"' in html_text
    finally:
        ws.close()

def test_export_service_real_output_changes_with_base_run_and_overrides(tmp_path: Path) -> None:
    ws = build_identity_v3_1_export_workspace(tmp_path / "export-parameter-effects")
    try:
        service = IdentityV31ReportExportService(ws.root)
        default_dir = Path(service.export(output_root=tmp_path / "bundle-default")["output_dir"])
        latest_dir = Path(
            service.export(
                base_run_id=ws.latest_non_target_run_id,
                output_root=tmp_path / "bundle-latest",
            )["output_dir"]
        )
        override_dir = Path(
            service.export(
                promote_cluster_ids={ws.cluster_ids["pending_promotable"]},
                disable_seed_cluster_ids={ws.cluster_ids["seed_fallback"]},
                output_root=tmp_path / "bundle-override",
            )["output_dir"]
        )

        default_manifest = json.loads((default_dir / "manifest.json").read_text(encoding="utf-8"))
        latest_manifest = json.loads((latest_dir / "manifest.json").read_text(encoding="utf-8"))
        override_manifest = json.loads((override_dir / "manifest.json").read_text(encoding="utf-8"))

        default_cluster_ids = {
            *(item["source_cluster_id"] for item in default_manifest["seed_identities"]),
            *(item["cluster_id"] for item in default_manifest["pending_clusters"]),
        }
        latest_cluster_ids = {
            *(item["source_cluster_id"] for item in latest_manifest["seed_identities"]),
            *(item["cluster_id"] for item in latest_manifest["pending_clusters"]),
        }

        assert default_manifest["base_run"]["id"] == ws.base_run_id
        assert latest_manifest["base_run"]["id"] == ws.latest_non_target_run_id
        assert default_cluster_ids == ws.expected_cluster_ids_by_run_id[ws.base_run_id]
        assert latest_cluster_ids == ws.expected_cluster_ids_by_run_id[ws.latest_non_target_run_id]
        assert latest_cluster_ids != default_cluster_ids

        assert override_manifest["parameters"]["promote_cluster_ids"] == [ws.cluster_ids["pending_promotable"]]
        assert override_manifest["parameters"]["disable_seed_cluster_ids"] == [ws.cluster_ids["seed_fallback"]]
        assert ws.cluster_ids["pending_promotable"] in {
            item["source_cluster_id"] for item in override_manifest["seed_identities"]
        }
        assert ws.cluster_ids["seed_fallback"] not in {
            item["source_cluster_id"] for item in override_manifest["seed_identities"]
        }
        assert any(
            item["cluster_id"] == ws.cluster_ids["pending_promotable"] and item["promoted_to_seed"] is True
            for item in override_manifest["pending_clusters"]
        )
        assert override_manifest["assignment_summary"]["candidate_count"] != default_manifest["assignment_summary"]["candidate_count"] or {
            item["best_cluster_id"] for item in override_manifest["assignments"]
        } != {
            item["best_cluster_id"] for item in default_manifest["assignments"]
        }
    finally:
        ws.close()

def test_export_service_renders_full_html_sections_not_shells(tmp_path: Path) -> None:
    ws = build_identity_v3_1_export_workspace(tmp_path / "export-html-completeness")
    try:
        result = IdentityV31ReportExportService(ws.root).export(output_root=tmp_path / "bundle")
        output_dir = Path(result["output_dir"])
        manifest = json.loads((output_dir / "manifest.json").read_text(encoding="utf-8"))
        html_text = (output_dir / "index.html").read_text(encoding="utf-8")
        primary_seed = next(
            item for item in manifest["seed_identities"]
            if item["source_cluster_id"] == ws.cluster_ids["seed_primary"]
        )
        review_assignment = next(
            item for item in manifest["assignments"]
            if item["observation_id"] == ws.observation_ids["attachment_review_margin"]
        )
        summary_text = normalize_visible_text(extract_details_visible_text(html_text, "summary"))
        seed_text = normalize_visible_text(extract_details_visible_text(html_text, "seed-identities"))
        overrides_text = normalize_visible_text(extract_details_visible_text(html_text, "overrides"))
        pending_text = normalize_visible_text(extract_details_visible_text(html_text, "review-pending-clusters"))
        auto_text = normalize_visible_text(extract_details_visible_text(html_text, "bucket-auto-assign"))
        review_text = normalize_visible_text(extract_details_visible_text(html_text, "bucket-review"))
        reject_text = normalize_visible_text(extract_details_visible_text(html_text, "bucket-reject"))

        assert f'data-summary-workspace="{ws.root.resolve()}"' in html_text
        assert f'data-summary-base-run-id="{ws.base_run_id}"' in html_text
        assert f'data-summary-snapshot-id="{ws.snapshot_id}"' in html_text
        assert 'data-summary-generated-at="' in html_text
        assert 'data-summary-assign-source="all"' in html_text
        assert f'data-summary-auto-assign-count="{manifest["assignment_summary"]["auto_assign_count"]}"' in html_text
        assert f'data-summary-review-count="{manifest["assignment_summary"]["review_count"]}"' in html_text
        assert f'data-summary-reject-count="{manifest["assignment_summary"]["reject_count"]}"' in html_text
        assert 'data-summary-warning-count="' in html_text
        assert 'data-summary-error-count="' in html_text

        assert f'data-seed-cluster-id="{ws.cluster_ids["seed_primary"]}"' in html_text
        assert f'data-seed-cluster-id="{ws.cluster_ids["seed_fallback"]}"' in html_text
        assert 'data-seed-resolution-state="materialized"' in html_text
        assert 'data-seed-member-count="' in html_text
        assert 'data-seed-fallback-used="false"' in html_text
        assert 'data-seed-fallback-used="true"' in html_text
        assert 'data-seed-representative-crop-src="' in html_text
        assert 'data-seed-representative-context-src="' in html_text
        assert 'data-seed-member-observation-id="' in html_text

        assert 'data-overrides-promoted-cluster-ids="none"' in html_text
        assert 'data-overrides-disabled-seed-cluster-ids="none"' in html_text
        assert f'data-overrides-invalid-prototype-cluster-ids="{ws.cluster_ids["seed_invalid"]}"' in html_text

        assert f'data-pending-cluster-id="{ws.cluster_ids["pending_promotable"]}"' in html_text
        assert 'data-pending-retained-member-count="' in html_text
        assert 'data-pending-distinct-photo-count="' in html_text
        assert 'data-pending-representative-count="' in html_text
        assert 'data-pending-retained-count="' in html_text
        assert 'data-pending-excluded-count="' in html_text
        assert 'data-pending-promoted-to-seed="false"' in html_text

        assert f'data-bucket-id="auto_assign" data-bucket-count="{manifest["assignment_summary"]["auto_assign_count"]}"' in html_text
        assert f'data-bucket-id="review" data-bucket-count="{manifest["assignment_summary"]["review_count"]}"' in html_text
        assert f'data-bucket-id="reject" data-bucket-count="{manifest["assignment_summary"]["reject_count"]}"' in html_text
        assert 'data-assignment-observation-id="' in html_text
        assert 'data-assignment-photo-id="' in html_text
        assert 'data-assignment-source-kind="' in html_text
        assert 'data-assignment-best-cluster-id="' in html_text
        assert 'data-assignment-distance-margin="' in html_text
        assert 'data-assignment-reason-code="' in html_text

        assert str(ws.root.resolve()) in summary_text
        assert str(ws.base_run_id) in summary_text
        assert str(ws.snapshot_id) in summary_text
        assert "generated at" in summary_text or "导出时间" in summary_text
        assert "assign source" in summary_text or "参数摘要" in summary_text
        assert str(manifest["assignment_summary"]["auto_assign_count"]) in summary_text
        assert str(manifest["assignment_summary"]["review_count"]) in summary_text
        assert str(manifest["assignment_summary"]["reject_count"]) in summary_text
        assert "warnings" in summary_text or "警告" in summary_text
        assert "errors" in summary_text or "错误" in summary_text

        assert str(ws.cluster_ids["seed_primary"]) in seed_text
        assert str(ws.cluster_ids["seed_fallback"]) in seed_text
        assert "materialized" in seed_text
        assert "fallback" in seed_text
        assert str(primary_seed["member_observation_ids"][0]) in seed_text

        assert "promoted" in overrides_text or "提升" in overrides_text
        assert "disabled" in overrides_text or "禁用" in overrides_text
        assert str(ws.cluster_ids["seed_invalid"]) in overrides_text
        assert "none" in overrides_text

        assert str(ws.cluster_ids["pending_promotable"]) in pending_text
        assert "distinct" in pending_text or "照片数" in pending_text
        assert "retained" in pending_text
        assert "excluded" in pending_text
        assert "false" in pending_text or "否" in pending_text

        assert str(ws.observation_ids["attachment_auto"]) in auto_text
        assert str(ws.photo_ids["attachment_auto"]) in auto_text
        assert "auto_assign" in auto_text
        assert str(ws.observation_ids["attachment_review_margin"]) in review_text
        assert str(ws.photo_ids["attachment_review_margin"]) in review_text
        assert "reason_code" in review_text or "原因" in review_text
        assert f'{review_assignment["top_candidates"][0]["distance"]:.6f}' in review_text
        assert str(ws.observation_ids["attachment_reject"]) in reject_text
        assert str(ws.photo_ids["attachment_reject"]) in reject_text
        assert "reject" in reject_text
    finally:
        ws.close()
```

还要断言：

- `manifest.json` 顶层字段完整
- `assignment_summary.candidate_count == auto_assign_count + review_count + reject_count`
- `assignment_summary.same_photo_conflict_count`、`missing_embedding_count`、`dimension_mismatch_count` 已稳定写入 manifest，并可被测试直接断言
- `manifest["parameters"]` 至少包含 `base_run_id`、`assign_source`、`top_k`、`auto_max_distance`、`review_max_distance`、`min_margin`、`promote_cluster_ids`、`disable_seed_cluster_ids`
- 真实 export 用例必须证明：
  - 默认导出与 `base_run_id=ws.latest_non_target_run_id` 的导出在 `manifest["base_run"]["id"]` 和最终 cluster 集上存在可观察差异
  - 非空 `promote_cluster_ids` / `disable_seed_cluster_ids` 会真实改变 `manifest["parameters"]`、`seed_identities`、`pending_clusters.promoted_to_seed`，以及 `assignments` 或 `candidate_count`
- `seed_identities` 里同时出现 trusted-seed primary 和 fallback seed
- `seed_identities` 每项都带 `source_cluster_id`、`seed_member_count`、`fallback_used`、`prototype_dimension`、`valid`、`error_code`、`error_message`
- invalid seed cluster 必须同时出现在 `manifest["errors"]` 与 `seed_identities[...]["valid"] == false` 的摘要中，并在 `overrides` 区块可见
- `pending_clusters` 能看到 `review_pending` cluster，并带 `promoted_to_seed`
- `pending_clusters` 每项都带 `retained_member_count`、`distinct_photo_count`、`representative_count`、`retained_count`、`excluded_count`
- HTML 必须出现 `<details id="summary">`、`<details id="seed-identities">`、`<details id="overrides">`、`<details id="review-pending-clusters">`、`<details id="bucket-auto-assign">`、`<details id="bucket-review">`、`<details id="bucket-reject">`
- `summary` 区块必须真实渲染 workspace/base run/snapshot/generated_at/参数摘要/总计数/warnings/errors 摘要，而不是只留标题外壳
- `seed-identities` 区块必须真实渲染 source cluster id、resolution state、prototype 成员数、fallback、representative 图、成员列表
- `overrides` 区块必须真实渲染 promoted cluster ids、disabled seed cluster ids、invalid prototype cluster ids；空值必须显示 `none`
- `review-pending-clusters` 区块必须真实渲染 cluster id、retained/distinct photo/representative-retained-excluded 计数、promoted_to_seed
- 三个 bucket 区块必须真实渲染 bucket 计数和 observation 卡片字段，不能只有 `<details>` 外壳
- HTML 测试必须对各 `<details>` 区块提取 `visible_text` 后断言正文内容，证明用户直接打开 `index.html` 就能读到这些值，而不是只能依赖隐藏属性或回头看 manifest
- `assignment` 卡片包含 observation id、photo id、source kind、best cluster、top-k、margin、reason_code
- `manifest["assignments"][*]["top_candidates"]` 必须是非占位对象数组，至少包含 `rank`、`identity_id`、`cluster_id`、`distance`，并且顺序与 Task 3 的排序口径一致
- HTML assignment card 必须真实展示 top-k candidate 与距离，而不是只显示 best/second；测试通过 `data-top-candidate-rank`、`data-cluster-id`、`data-distance` 断言页面中确实渲染了 top-k 列表
- 测试文件可定义 `extract_details_visible_text()` / `normalize_visible_text()` 这类 helper，但断言目标必须是各 section 的可见正文，不是原始 HTML 属性拼接
- 当某个 promoted pending seed 的成员被 Task 3 排除时，这些 observation 不会出现在 `manifest["assignments"]` 中，且不会计入 `candidate_count`
- 若单个 observation 的 `crop`、`context` 或 `preview` 任一导出失败，页面仍然生成，manifest `warnings` 增加对应 `asset_kind` 记录
- 测试文件内必须定义 `TrackingPreviewArtifactService(PreviewArtifactService)`，构造参数固定为 `db_path` 与 `workspace`；它要真实调用父类 `ensure_crop()`、`ensure_context()`、`ensure_photo_preview(photo_id=..., source_path=Path(primary_path))`，并把返回路径记录到：
  - `crop_paths: dict[int, Path]`（key = `observation_id`）
  - `context_paths: dict[int, Path]`（key = `observation_id`）
  - `preview_paths_by_photo_id: dict[int, Path]`（key = `photo_id`）
  导出后断言 bundle 内对应 `crop/context/preview` 文件字节与这些源 artifact 完全一致，禁止用空白图或硬编码占位图混过测试

- [ ] **Step 2: 运行导出服务测试，确认导出服务尚未存在**

Run:

```bash
source .venv/bin/activate
PYTHONPATH=src python -m pytest tests/people_gallery/test_identity_v3_1_export_service.py -q
```

Expected: FAIL，报 `ModuleNotFoundError` 或 `AttributeError`。

- [ ] **Step 3: 实现 `IdentityV31ReportExportService`，生成单页 HTML、manifest 和自包含 assets**

实现要求：

- 服务入口固定为：
  - `IdentityV31ReportExportService.__init__(workspace: Path, *, query_service: IdentityV31QueryService | None = None, assignment_service: IdentityV31AssignmentService | None = None, preview_artifact_service: PreviewArtifactService | None = None)`
  - `IdentityV31ReportExportService.export(*, base_run_id: int | None = None, promote_cluster_ids: set[int] | None = None, disable_seed_cluster_ids: set[int] | None = None, assign_parameters: AssignParameters | None = None, output_root: Path) -> dict[str, Path]`

- `export()` 要按 `ObservationNeighborExportService` 的目录模式创建：
  - `<output_dir>/index.html`
  - `<output_dir>/manifest.json`
  - `<output_dir>/assets/observations/obs-<id>/crop.jpg`
  - `<output_dir>/assets/observations/obs-<id>/context.jpg`
  - `<output_dir>/assets/observations/obs-<id>/preview.jpg`
- 资产导出逻辑必须：
  - 对所有在 seed / pending cluster / assignment cards 中出现的 observation 去重
  - 先调用：
    - `self.preview_artifact_service.ensure_crop(int(observation_id))`
    - `self.preview_artifact_service.ensure_context(int(observation_id))`
    - `self.preview_artifact_service.ensure_photo_preview(photo_id=int(photo_id), source_path=Path(primary_path))`
  - 再把上述返回路径指向的真实 artifact 用 `copy2` 复制到 bundle 内的 `assets/observations/obs-<id>/`
  - bundle 里的 `crop/context/preview` 文件字节必须与返回的 artifact 源文件完全一致；禁止生成占位图、空白文件或重新编码的假文件
  - `crop` / `context` / `preview` 任一单项失败时，只记 `warnings`，不能中断整轮导出
- `output_dir` 必须固定创建为 `output_root/<timestamp>` 子目录；禁止把 `index.html`、`manifest.json`、`assets/` 直接写到 `output_root` 根目录。
- HTML 只能由 `export_service.py` 直接输出，不要引入模板；结构固定为以下 `<details id="...">`：
  - `summary`
  - `seed-identities`
  - `overrides`
  - `review-pending-clusters`
  - `bucket-auto-assign`
  - `bucket-review`
  - `bucket-reject`
- 每个 `<details>` 区块都必须渲染稳定的主内容 DOM，不能只输出标题再让用户回头看 manifest；至少要求：
  - `summary`：输出 `data-summary-workspace`、`data-summary-base-run-id`、`data-summary-snapshot-id`、`data-summary-generated-at`、`data-summary-assign-source`、`data-summary-auto-assign-count`、`data-summary-review-count`、`data-summary-reject-count`、`data-summary-warning-count`、`data-summary-error-count`
  - `seed-identities`：每张卡输出 `data-seed-cluster-id`、`data-seed-resolution-state`、`data-seed-member-count`、`data-seed-fallback-used`、`data-seed-representative-crop-src`、`data-seed-representative-context-src`，并在成员列表输出 `data-seed-member-observation-id`
  - `overrides`：输出 `data-overrides-promoted-cluster-ids`、`data-overrides-disabled-seed-cluster-ids`、`data-overrides-invalid-prototype-cluster-ids`；空值固定写 `none`
  - `review-pending-clusters`：每张卡输出 `data-pending-cluster-id`、`data-pending-retained-member-count`、`data-pending-distinct-photo-count`、`data-pending-representative-count`、`data-pending-retained-count`、`data-pending-excluded-count`、`data-pending-promoted-to-seed`
  - bucket 区块：输出 `data-bucket-id`、`data-bucket-count`，每张 observation 卡输出 `data-assignment-observation-id`、`data-assignment-photo-id`、`data-assignment-source-kind`、`data-assignment-best-cluster-id`、`data-assignment-distance-margin`、`data-assignment-reason-code`
- `manifest["parameters"]` 必须直接写出本次导出的有效参数：
  - `base_run_id`
  - `assign_source`
  - `top_k`
  - `auto_max_distance`
  - `review_max_distance`
  - `min_margin`
  - `promote_cluster_ids`
  - `disable_seed_cluster_ids`
- `seed_identities` 写入 manifest 时必须把 `source_cluster_id`、`seed_member_count`、`fallback_used`、`prototype_dimension` 全部落进去；不要只写 cluster id。
- `seed_identities` 写入 manifest 时必须把有效 seed 和 invalid seed 一起写进去；invalid seed 记录仍沿用 `SeedIdentityRecord` 同形状输出，要求 `valid=false`，并同步写出 `error_code`、`error_message`。
- `pending_clusters` 写入 manifest 时必须把 `retained_member_count`、`distinct_photo_count`、`representative_count`、`retained_count`、`excluded_count`、`promoted_to_seed` 全部落进去。
- `manifest.json` 顶层必须包含 `assignments` 数组，并在每个 observation 上写出：
  - `top_candidates`
  - `assets`
  - `missing_assets`
  - `decision`
  - `reason_code`
- `top_candidates` 序列化结构必须固定为：
  - `rank`
  - `identity_id`
  - `cluster_id`
  - `distance`
  并保持与 assign 层完全一致的顺序和数值；禁止用字符串占位、只写 best/second，或在导出层重新排序
- `overrides` 区块渲染必须直接消费：
  - `manifest["parameters"]["promote_cluster_ids"]`
  - `manifest["parameters"]["disable_seed_cluster_ids"]`
  - `invalid prototype cluster ids`（来自 `seed_result.invalid_seeds`，并与 `errors` 的 `cluster_id` 对齐）
  三项缺一都视为实现不完整。
- invalid seed cluster 还必须在页面顶部摘要和 `seed identities` 摘要中出现错误提示，不能只埋在 JSON 里。
- HTML 卡片输出时必须直接消费同一份 manifest-like 内存结构，不要为页面再拼一套平行数据结构，避免页面和 manifest 统计不一致。
- `export()` 必须真实消费 `base_run_id`、`promote_cluster_ids`、`disable_seed_cluster_ids` 来生成 bundle；测试会直接比较多次真实导出的 `manifest.json`，禁止只在脚本参数传递或 Query/Assignment 单测里证明这些参数生效。
- assignment card 的 top-k 列表必须渲染成稳定 DOM 结构，例如 `<ol class="top-candidates">` 下的 `<li data-top-candidate-rank="1" data-cluster-id="..." data-distance="...">`；测试将按这些属性检查页面确实展示了多个 candidate 与距离。
- 除 `data-*` 属性外，页面正文必须同步渲染可见文本：
  - `summary`：正文中出现 workspace、base run、snapshot、generated_at、参数摘要、计数、warnings/errors 摘要
  - `seed-identities`：正文中出现 source cluster id、resolution state、prototype 成员数、fallback、成员 observation id
  - `overrides`：正文中出现 promoted ids、disabled ids、invalid prototype ids；空值显示 `none`
  - `review-pending-clusters`：正文中出现 cluster id 与 retained/distinct photo/representative-retained-excluded 计数
  - bucket 卡片：正文中出现 observation id、photo id、reason_code、top-k candidate 距离等
- 成功导出后返回：

```python
{
    "output_dir": output_dir,
    "index_path": index_path,
    "manifest_path": manifest_path,
}
```

- [ ] **Step 4: 回跑导出服务测试，确认 bundle 已可离线打开且具备软失败记录**

Run:

```bash
source .venv/bin/activate
PYTHONPATH=src python -m pytest tests/people_gallery/test_identity_v3_1_export_service.py -q
```

Expected: PASS。

### Task 5: 独立脚本入口与端到端脚本测试

**Depends on:** Task 4

**Scope Budget:**
- Max files: 20
- Estimated files touched: 2
- Max added lines: 1000
- Estimated added lines: 260

**Files:**
- Create: `scripts/export_identity_v3_1_report.py`
- Create: `tests/people_gallery/test_export_identity_v3_1_report_script.py`

- [ ] **Step 1: 先写脚本测试，锁定 argparse、默认 workspace/输出目录和非零退出码行为**

测试模式直接复用现有 `tests/people_gallery/test_export_observation_neighbors_script.py` 的 `spec_from_file_location + main(argv)` 方式，不要把脚本硬塞进主 CLI。至少覆盖：

```python
def test_script_passes_parsed_arguments_to_export_service(tmp_path: Path, monkeypatch) -> None:
    calls = {}

    class _StubService:
        def __init__(self, workspace: Path) -> None:
            calls["workspace"] = Path(workspace)

        def export(self, **kwargs: object) -> dict[str, Path]:
            calls["kwargs"] = dict(kwargs)
            output_dir = tmp_path / "bundle"
            output_dir.mkdir(parents=True, exist_ok=True)
            return {
                "output_dir": output_dir,
                "index_path": output_dir / "index.html",
                "manifest_path": output_dir / "manifest.json",
            }

def test_script_rejects_invalid_assign_parameters_before_calling_export_service(tmp_path: Path, monkeypatch) -> None:
    calls = {"validate": 0, "export": 0}

    def _validate(self):
        calls["validate"] += 1
        raise ValueError("top_k 必须大于 0")

    monkeypatch.setattr(_SCRIPT_MODULE.AssignParameters, "validate", _validate)

    class _StubService:
        def __init__(self, workspace: Path) -> None:
            pass

        def export(self, **kwargs: object) -> dict[str, Path]:
            calls["export"] += 1
            raise AssertionError("validate 失败后不应进入 export")

def test_script_happy_path_runs_real_fixture_workspace_and_uses_default_output_root(tmp_path: Path, capsys) -> None:
    ws = build_identity_v3_1_export_workspace(tmp_path / "script-real-workspace")
    try:
        rc = export_main(["--workspace", str(ws.root)])
        assert rc == 0
        stdout_text = capsys.readouterr().out
        payload = extract_json_payload(stdout_text)
        output_dir = Path(payload["output_dir"])
        assert output_dir.parent == (Path(_SCRIPT_PATH).resolve().parents[1] / ".tmp" / "v3_1-identity-prototype")
        assert (output_dir / "index.html").is_file()
        assert (output_dir / "manifest.json").is_file()
    finally:
        ws.close()
```

还要断言：

- `--base-run-id 123` 会解析成 `base_run_id=123` 并传给 export service
- `--assign-source attachment`、`--top-k 7`、`--auto-max-distance 0.22`、`--review-max-distance 0.31`、`--min-margin 0.09` 会被组装进 `AssignParameters` 并传给 export service
- `--promote-cluster-ids 11,22` 会解析成 `{11, 22}`
- `--disable-seed-cluster-ids 33,44` 会解析成 `{33, 44}`
- 默认 `workspace` 为 `repo/.tmp/.hikbox`
- 默认 `output_root` 为 `repo/.tmp/v3_1-identity-prototype`
- 成功路径 stdout 会输出包含 `output_dir` / `index_path` / `manifest_path` 的 JSON 摘要
- `AssignParameters.validate()` 会在脚本里被显式调用；测试中用 `monkeypatch` 替换该方法并断言调用发生
- `--assign-source bogus` 触发 argparse 解析失败；`--top-k 0`、`--auto-max-distance 0.5 --review-max-distance 0.4`、`--min-margin -0.1` 触发 `.validate()` 失败并返回 `1`
- 当导出服务抛出异常时，脚本返回 `1`
- 当禁用所有 seed 时，脚本返回 `1`
- 在真实夹具 workspace 上直接跑 `main(argv)` 会生成离线 bundle，stdout JSON 摘要里的 `output_dir` 指向默认时间戳子目录

- [ ] **Step 2: 运行脚本测试，确认脚本入口尚未实现**

Run:

```bash
source .venv/bin/activate
PYTHONPATH=src python -m pytest tests/people_gallery/test_export_identity_v3_1_report_script.py -q
```

Expected: FAIL，报脚本文件不存在或 `main` 未定义。

- [ ] **Step 3: 新建 `scripts/export_identity_v3_1_report.py`，保持和现有导出脚本一致的交互风格**

实现要求：

- 参数解析风格必须和 `scripts/export_observation_neighbors.py` 一致，使用 `argparse` + `Path`。
- `--assign-source` 必须用 `choices=("all", "review_pending", "attachment")` 锁死解析范围；非法值由 argparse 直接拒绝。
- 对逗号分隔的 cluster id 参数实现独立 parser helper：

```python
def _parse_cluster_ids(raw: str) -> set[int]:
    values: set[int] = set()
    for token in str(raw).split(","):
        stripped = token.strip()
        if stripped:
            values.add(int(stripped))
    if not values:
        raise ValueError("cluster id 列表不能为空")
    return values
```

- 默认输出目录必须是：

```python
Path(__file__).resolve().parents[1] / ".tmp" / "v3_1-identity-prototype"
```

- 默认 workspace 必须是：

```python
Path(__file__).resolve().parents[1] / ".tmp" / ".hikbox"
```

- 脚本在调用 export service 前必须先构造：

```python
assign_parameters = AssignParameters(
    top_k=int(args.top_k),
    auto_max_distance=float(args.auto_max_distance),
    review_max_distance=float(args.review_max_distance),
    min_margin=float(args.min_margin),
    assign_source=str(args.assign_source),
).validate()
```

- 然后把以下字段原样传给 export service：
  - `base_run_id`
  - `promote_cluster_ids`
  - `disable_seed_cluster_ids`
  - `assign_parameters`
  - `output_root`
- 脚本成功时打印 JSON 摘要，最少字段：
  - `output_dir`
  - `index_path`
  - `manifest_path`
- 脚本失败时打印 stderr，并返回 `1`；不要吞掉异常后返回 `0`。
- `Task 5` 的真实 happy path 测试必须直接调用 `main(argv)`，不能只用 stub service 证明参数传递。

- [ ] **Step 4: 回跑脚本测试，再跑整套新增测试子集确认闭环**

Run:

```bash
source .venv/bin/activate
PYTHONPATH=src python -m pytest tests/people_gallery/test_export_identity_v3_1_report_script.py -q
```

Expected: PASS。

Run:

```bash
source .venv/bin/activate
PYTHONPATH=src python -m pytest \
  tests/people_gallery/test_identity_v3_1_export_fixtures.py \
  tests/people_gallery/test_identity_v3_1_query_service.py \
  tests/people_gallery/test_identity_v3_1_assignment_service.py \
  tests/people_gallery/test_identity_v3_1_export_service.py \
  tests/people_gallery/test_export_identity_v3_1_report_script.py -q
```

Expected: PASS，且至少有一条真实导出用例生成 `index.html`、`manifest.json` 和 `assets/`。

## 交付完成判定

- `scripts/export_identity_v3_1_report.py` 能在不启动服务的前提下完成一次离线导出。
- `--base-run-id` 的正向覆盖路径已被测试锁定，且不会被默认 review target 逻辑吞掉。
- 默认入口在缺少 `is_review_target = 1` 的 succeeded run 时会硬失败；workspace 配置损坏时会直接暴露 `load_workspace_paths()` 的错误。
- 查询层严格限定在 selected `base_run` 与其 `observation_snapshot_id` 的数据范围内，不会跨 run、跨 snapshot，也不会把 warmup/active observation 混入 candidates。
- `--assign-source`、`--top-k`、`--auto-max-distance`、`--review-max-distance`、`--min-margin` 的解析、传递和校验失败路径都被测试锁定。
- `AssignParameters.validate()` 已由 QueryService 和脚本入口两侧测试证明会被调用。
- `index.html` 是唯一页面入口，并包含 `seed identities`、`review_pending clusters`、`auto_assign`、`review`、`reject` 五类主要核对区块。
- `index.html` 使用固定 `<details id="summary">`、`<details id="seed-identities">`、`<details id="overrides">`、`<details id="review-pending-clusters">`、`<details id="bucket-auto-assign">`、`<details id="bucket-review">`、`<details id="bucket-reject">` 结构，测试通过具体 id 锁定，而不是只看文案。
- `index.html` 不是 manifest 的空壳镜像；测试已锁定 `summary`、`seed-identities`、`overrides`、`review-pending-clusters`、三个 bucket 区块都必须在页面主 DOM 中渲染关键字段与卡片内容。
- `index.html` 的 `overrides` 区块会展示 promoted cluster ids、disabled seed cluster ids、invalid prototype cluster ids。
- `base_run_id`、非空 `promote_cluster_ids`、非空 `disable_seed_cluster_ids` 已由真实 export 层用例证明会改变 bundle 输出，而不是只在参数传递或子服务单测里生效。
- `manifest.json` 顶层字段和计数不变量满足本计划的固定契约。
- `manifest["parameters"]` 明确写出本次导出的 base run id、assign 参数和 override 列表。
- 双来源重叠 observation 在最终 candidate 集里只保留一条，且 `source_kind == 'review_pending_retained'`。
- QueryService 已通过真实样本证明只接受正确 `model_key + normalized=1` 的 embedding row。
- `--promote-cluster-ids` 与 `--disable-seed-cluster-ids` 会真实改变 seed 集合，而不是只改显示文案。
- promote / disable 非法 override 都会硬失败，不会静默忽略。
- prototype 公式已由真实 `build_seed_identities() -> evaluate_assignments()` 路径锁定为“trusted seed 优先 / retained 回退 / 均值 / 再归一化”，不能被 representative、首成员或未归一化均值替代。
- `top_candidates` 的长度、排序、距离值，以及单候选时 `second_best_distance = float("inf")` 都已被测试锁定，并且 manifest/HTML 会按同一口径输出。
- `index.html` 的正文文本已被分区块测试锁定；用户直接打开页面即可读到 workspace/base run/snapshot、seed/pending cluster、bucket 卡片、reason_code 和 top-k 距离等核心信息。
- 被 promote 为 seed 的 pending cluster 成员不会进入最终 `assignments`，也不会计入 `candidate_count`。
- invalid seed 会同时出现在 `manifest["seed_identities"]` 的 `valid=false` 记录、`manifest["errors"]`、页面摘要和 `overrides` 中；当所有启用 seed 都 invalid 时，流程会硬失败。
- `assignment_summary.same_photo_conflict_count`、`missing_embedding_count`、`dimension_mismatch_count` 已稳定序列化到 manifest。
- bundle 内 `crop/context/preview` 文件来自 `PreviewArtifactService.ensure_*()` 返回的真实 artifact 路径复制，测试通过字节相等锁定，不允许占位文件。
- 当没有任何可用 seed identity 时，脚本返回非零退出码。
- 整个实现不引入 schema 变更、不触碰正式 CLI/API/WebUI，不写回 `person` 或 `prototype_*` 真相。
