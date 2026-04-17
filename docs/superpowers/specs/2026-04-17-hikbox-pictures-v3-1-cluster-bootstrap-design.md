# HikBox Pictures v3.1 Cluster Bootstrap 双阶段设计文档

## 目标

v3.1 不再把当前工作理解成“修补 v3 bootstrap”，而是把 bootstrap 重做成一个可反复 rerun、可 review、可调参的独立系统。

本设计的直接目标有四个：

- 先把“是否存在人物 cluster”和“哪些成员应该留在 cluster 中”拆成两层决策。
- 把可复用的 observation 级预处理与可反复重跑的 bootstrap run 参数彻底拆开。
- 把 Phase 1 写成一个完整实验闭环：预处理复用、bootstrap rerun、run 选择、review 页面、neighbor 导出、再次 rerun。
- 把 Phase 2 与日常功能开发明确隔离，避免 Phase 1 为了兼顾完整产品流程而继续绑死在旧语义上。

## 设计前提

本次设计接受以下前提，并以此为边界：

- 不再考虑 v3 兼容性，也不以“尽量不破坏现有代码”为目标。
- 当前代码里已经落了一部分 v3 改造以及更早的实现，允许在 v3.1 中整体替换或废弃。
- v3.1 仍然按两个阶段推进，但阶段边界必须比 v3 更硬。
- Phase 1 的目标不是交付完整人物系统，而是交付“可重复试验并可被人工 review 的 bootstrap 系统”。
- Phase 2 才接上日常身份归属、review queue、人物维护、导出与完整 WebUI。
- 可以尽量复用 v3 已经做过的 observation 级预处理能力，但这些能力必须在契约上从 bootstrap 参数中拆出来。

## 非目标

本设计刻意不覆盖以下内容：

- 不在本文中展开具体 migration SQL。
- 不在本文中展开每个脚本、服务、页面的逐文件实施步骤。
- 不继续兼容 `auto_cluster`、`auto_cluster_member`、`person.origin_cluster_id` 作为 v3.1 运行时真相。
- 不把日常增量扫描、自动归属、review queue、导出流程混进 Phase 1。
- 不保留 `top2 + mutual + margin` 这一类旧 bootstrap 规则作为 v3.1 的主路径。

## 核心判断

v3 的主方向并没有错，错的是 bootstrap 的问题拆分方式。

真实问题不是“某两个 observation 能不能连边”，而是：

1. 这些 observation 里是否存在稳定的人物密度团。
2. 这个团里哪些 observation 属于核心、哪些只是边界、哪些应该被排除。
3. 这个团在清洗完成后，是否已经稳定到可以直接长成人物。

因此 v3.1 的核心变化不是“把阈值调松”，而是把 bootstrap 改成：

- 先在 observation 级完成质量、去重、分池等可复用预处理。
- 再在 bootstrap run 级完成成团、拆分、清洗、吸附和 cluster 级处置。
- 最后才决定是否产出匿名 person、trusted seed、prototype 与 ANN。

## 两阶段设计

## Phase 1：Bootstrap Rerun + Review 闭环

Phase 1 只做 bootstrap 系统本身，并把它做成一个可重复实验闭环。

Phase 1 必须交付以下能力：

- observation 级预处理可独立运行，并产出可复用快照。
- 在同一份 observation 预处理快照上，可以基于不同 cluster 参数反复创建新的 bootstrap run。
- 每次 rerun 都生成独立 `run_id`，历史 run 保留，不覆盖旧结果。
- `/identity-tuning` 与 `scripts/export_observation_neighbors.py` 都能显式选择 `run_id`。
- review 面能展示 run 级摘要、raw -> cleaned -> final lineage、cluster 指标、成员保留/排除证据。
- 对通过硬门的 `materialized` cluster，Phase 1 允许直接产出匿名 person、trusted seed、prototype 与 ANN。
- 对没有通过硬门但 cluster 仍然成立的结果，必须稳定落为 `review_pending`，而不是半成品人物。

Phase 1 明确不做以下事情：

- 不做日常扫描后的增量新人物发现。
- 不做正式 review queue 的产品化处理流。
- 不做人物重命名、merge、split、导出模板维护等日常操作闭环。
- 不要求现有 v3 WebUI 与旧 batch 语义继续可用。

Phase 1 的验收标准是：

- 同一 observation 快照可跑出多轮 run，并能按 `run_id` review。
- 人工能只看页面和导出工具，就理解某轮 bootstrap 为什么保留、排除、物化或暂缓某个 cluster。
- materialized 结果不会以“匿名 person 已写入，但 trusted seed / prototype / ANN 半失败”的状态暴露给用户。

## Phase 2：完整身份功能收口

Phase 2 基于 Phase 1 的稳定 bootstrap 契约，继续完成日常身份系统。

Phase 2 覆盖的内容包括：

- 日常增量扫描后的自动归属与新人物发现。
- review queue 的正式分类、操作动作与回写链路。
- 人物详情页、人物维护、merge / ignore / manual confirm。
- 导出链路与模板系统对接。
- 完整 README、CLI、WebUI 收口。

Phase 2 的关键约束是：

- 不得重新定义 Phase 1 的 cluster 语义。
- 不得再把 observation 预处理与 bootstrap 参数揉回一个 profile。
- Phase 2 的日常功能必须消费 Phase 1 的 run、cluster、resolution 与人物来源真相。

## 两层运行契约

## 第一层：Observation 级预处理契约

这一层负责回答“哪些 observation 有资格参与发现 cluster，以及它们以什么形式参与”。

它必须从 bootstrap run 中独立出来，因为它的产物可以跨多轮 rerun 复用。

### 新对象

v3.1 引入以下 observation 级对象：

- `identity_observation_profile`
  - 表示质量分、分池、去重规则的参数集合。
- `identity_observation_snapshot`
  - 表示某次预处理运行的不可变快照。
- `identity_observation_pool_entry`
  - 表示某个 observation 在该快照中的分池、去重、排除结果。

### 这一层负责的事情

- 计算或复用 `quality_score`。
- 把 observation 分到 `core_discovery`、`attachment`、`excluded`。
- 做证据去重，包括同图去重、burst 去重、完全重复向量折叠。
- 生成代表 observation。
- 记录排除原因、去重原因、质量分快照与必要的诊断字段。

### 可复用产物

以下产物属于可复用 observation 级结果：

- `quality_score` 与其组成特征快照。
- `core_discovery` / `attachment` / `excluded` 分池结果。
- 去重分组结果与代表 observation 选择结果。
- 同图 / burst / exact duplicate 的折叠信息。
- 可选的 k 无关候选索引或中间向量索引。

Phase 1 对缓存有一条硬约束：

- observation 快照不得缓存绑定 `discovery_knn_k`、`attachment_candidate_knn_k` 这类 run 参数的固定邻居列表。
- 如果为了加速保留候选索引，则该索引必须显式记录：
  - `candidate_policy_hash`
  - `max_knn_supported`
- 只要目标 run 的邻域需求超过 `max_knn_supported`，或候选生成规则哈希变化，就必须判定 observation 快照失效并重建。

### 复用条件

只有在以下条件全部满足时，新的 bootstrap run 才可以复用旧 observation 快照：

- `embedding_model_key`、`embedding_schema_version`、距离度量一致。
- observation 数据集没有变化，或至少参与身份判断的 observation 集合哈希一致。
- `identity_observation_profile` 未变化。
- observation 级算法版本未变化。
- 若快照带候选索引，则 `candidate_policy_hash` 与 `max_knn_supported` 仍覆盖当前 run 需求。

只要上述任一条件变化，就必须重新生成 `identity_observation_snapshot`，而不是复用旧快照。

## 第二层：Bootstrap Run 契约

这一层负责回答“在某份 observation 快照上，当前 cluster 参数会长出什么 cluster，以及这些 cluster 最终怎么处置”。

### 新对象

v3.1 引入以下 bootstrap 级对象：

- `identity_cluster_profile`
  - 表示密度发现、拆分、清洗、吸附、materialize gate 的参数集合。
- `identity_cluster_run`
  - 表示在某个 observation 快照上执行的一轮不可变 bootstrap run。

### run 基本规则

- 每次 rerun 都创建新的 `identity_cluster_run`，绝不覆盖旧 run。
- 历史 run 保留，用于 review 和横向对比。
- `identity_cluster_run` 必须显式引用：
  - `identity_observation_snapshot_id`
  - `identity_cluster_profile_id`
  - `algorithm_version`
  - `supersedes_run_id`（可空）
- run 状态最少包括：
  - `created`
  - `running`
  - `succeeded`
  - `failed`
  - `cancelled`
- 只有 `succeeded` 的 run 才有资格成为当前 review 对象。

### 当前 review 对象契约

为了闭合“多次 rerun + review”的流程，必须有一个明确的“当前 review 对象”概念。

约束如下：

- 全库同一时刻最多只有一个 `identity_cluster_run.is_review_target = 1`。
- 新 run 只要 `run_status = succeeded` 且整轮 `run summary`、`cluster`、`member`、`resolution`、失败审计信息已完整落库，就有资格切换 `is_review_target`。
- `is_review_target` 的切换不要求该 run 的 `materialized` cluster 全部通过 prepare，也不要求该 run 已成为 `is_materialization_owner`。
- 第一轮成功 run 如果库中尚无 review target，则系统必须在同一事务内把它标记为 `is_review_target = 1`，并写入 `review_selected_at = finished_at`。
- 后续 run 是否在成功后自动成为新的 review target，可以由 rerun CLI 决定；但 `select-run` 必须允许把任一 `succeeded` run 显式切换为 review target。
- `/identity-tuning` 默认只展示 `is_review_target = 1` 的 run。
- 如果缺少 review target，应视为数据完整性问题，而不是产品层隐式回退。
- `/identity-tuning`、neighbor 导出脚本、调试脚本都必须支持显式传入 `run_id`。
- 历史 run 默认不隐藏，也不被覆盖，只能显式清理。

### review target 与 materialization owner 分离

仅有 `is_review_target` 还不够，因为 Phase 1 允许直接长出匿名 person，而多次 rerun 又要求保留历史 run。

如果不把“当前 review 对象”和“当前 live 物化结果的所有者”拆开，就会出现：

- 历史 run 仍可 review，但旧 run 产出的匿名 person 继续活着；
- 新 run 再次 materialize 时，库里出现多套匿名 person；
- prototype / ANN 到底代表哪一轮 run 变得不明确。

因此必须再引入一个独立概念：

- `identity_cluster_run.is_materialization_owner`

约束如下：

- 全库同一时刻最多只有一个 `is_materialization_owner = 1` 的 run。
- `is_review_target = 1` 不自动等于 `is_materialization_owner = 1`。
- 新 run 成功后，默认只进入可 review 状态；是否发布为 live 结果，要走显式 activation。
- activation 只能针对 `succeeded` 且安全门全部通过的 run。
- 当某个 run 成为新的 materialization owner 时，上一轮 owner 产出的 bootstrap 匿名 person、prototype、ANN 必须在同一原子发布流程中退场。
- 历史 run 的 cluster、member、resolution、lineage 记录继续保留，不因 live owner 切换而删除。

### run 选择与激活入口

因为 `/identity-tuning` 必须保持只读，所以“切换默认 review 对象”和“发布 live 物化结果”必须由脚本或 CLI 完成。

Phase 1 至少要有两类显式入口：

- `select-run`
  - 只切换 `is_review_target`
  - 可针对任一 `succeeded` run 执行
  - 不触碰 live person / prototype / ANN
- `activate-run`
  - 先校验目标 run 是 `succeeded`
  - 再校验所有 `materialized` cluster 的 cluster 级 prepared bundle 与该 run 的 ANN prepared bundle 都已生成且可校验
  - 最后原子切换 `is_materialization_owner`

`activate-run` 的原子切换至少要覆盖：

- 旧 owner 的 bootstrap-origin 匿名 `person` 退场或失活
- 旧 owner 对应 `person_cluster_origin` 的 active 标记切换
- prototype live 指针切换
- ANN live artifact 指针切换
- 新 owner 的 `activated_at` 写入

这样，Phase 1 才能同时满足：

- 历史 run 可回看
- 页面默认对象可切换
- live 人物结果始终只代表一轮明确 run

### run 级 prepared artifact 契约

`materialized` 的 prepare 产物必须显式拆成两层，不能把所有内容都塞进 cluster 层：

- cluster 级 prepared bundle
  - 作用域是单个 `final` cluster。
  - 至少包含：
    - `trusted_seed_bundle`
    - `prototype_bundle`
    - `person_publish_plan`
- run 级 prepared bundle
  - 作用域是整个 `identity_cluster_run`。
  - 至少包含：
    - `ann_bundle`

其中有一条硬约束：

- ANN 不是 cluster 局部产物。
- ANN 必须由“该 run 中所有 cluster 级 `prototype_bundle`”聚合生成一个 run-scoped prepared artifact。
- `identity_cluster_resolution.ann_status` 表达的是“该 cluster 的 prototype 是否已被纳入所属 run 的 ANN prepared bundle / live ANN”，而不是“该 cluster 自己拥有一份独立 ANN 文件”。

为了保证 `activate-run` 可校验、可审计，prepare 产物必须具备以下 staging 契约：

- 所有 prepared artifact 都必须落在 run-scoped staging root 下，而不是直接写 live 路径。
- cluster 级和 run 级 manifest 至少都要记录：
  - `source_run_id`
  - `source_cluster_id`（run 级 ANN manifest 可空）
  - `artifact_path`
  - `artifact_checksum`
  - `model_key`
  - `created_at`
- `activate-run` 只能消费 manifest 完整且 checksum 校验通过的 prepared artifact。
- 若某个 cluster 的 cluster 级 prepared bundle 不完整，该 cluster 必须回退为 `review_pending`。
- 若 run 级 `ann_bundle` 构建失败，则该 run 中所有原本准备物化的 cluster 都必须回退为 `review_pending`，并显式记录 `run_ann_bundle_failed` 一类原因；不得带着残缺 ANN 进入 `materialized/prepared`。
- Phase 1 不引入 activation journal 与自动 crash recovery；若 `activate-run` 过程中进程异常，允许通过人工重新执行 `activate-run` 收敛状态，自动恢复流程留到后续阶段。

### 这一层必须重跑的内容

以下结果属于 run 级产物，每次 rerun 都必须重算：

- raw cluster 发现结果。
- raw -> cleaned -> final 的 lineage。
- `anchor_core` / `core` / `boundary` / `attachment` 成员判定。
- cluster 指标、成员指标、排除原因。
- `materialized` / `review_pending` / `discarded` 处置结果。
- trusted seed 选择结果。
- cluster 级 `trusted_seed_bundle` / `prototype_bundle` / `person_publish_plan` 产出结果，以及 run 级 `ann_bundle` 产出结果。

## 参数分层

## Observation 级参数

以下参数属于 `identity_observation_profile`，Phase 1 调 cluster 参数时默认不应重算它们：

| 参数 | 含义 |
| --- | --- |
| `quality_formula_version` | 质量分公式版本。 |
| `quality_area_weight` / `quality_sharpness_weight` / `quality_pose_weight` | 质量分特征权重。 |
| `core_quality_threshold` | 进入 `core_discovery` 的最低质量。 |
| `attachment_quality_threshold` | 进入 `attachment` 的最低质量。 |
| `exact_duplicate_distance_threshold` | 完全重复向量折叠阈值。 |
| `same_photo_keep_best` | 同图重复 observation 的保留规则。 |
| `burst_window_seconds` | burst 判定时间窗口。 |
| `burst_duplicate_distance_threshold` | burst 内折叠阈值。 |
| `pool_exclusion_rules_version` | 前置排除规则版本。 |

这组参数变化时，必须重建 observation 快照；不能只重跑 bootstrap run。

## Cluster Run 级参数

以下参数属于 `identity_cluster_profile`，这是 Phase 1 允许反复调参和 rerun 的主体：

| 参数 | 含义 |
| --- | --- |
| `discovery_knn_k` | 主聚类使用的邻域规模。 |
| `density_min_samples` | 本地密度半径使用的最小样本数。 |
| `raw_cluster_min_size` | raw cluster 的最小成员数。 |
| `raw_cluster_min_distinct_photo_count` | raw cluster 的最小不同照片数。 |
| `intra_photo_conflict_policy_version` | cluster 内同图冲突判定规则版本。 |
| `anchor_core_min_support_ratio` | 进入 `anchor_core` 的最低支持率。 |
| `anchor_core_radius_quantile` | `anchor_core` 半径取值所用分位点。 |
| `core_min_support_ratio` | 进入 `core` 的最低支持率。 |
| `boundary_min_support_ratio` | 进入 `boundary` 的最低支持率。 |
| `boundary_radius_multiplier` | `boundary` 最大半径相对 `anchor_core_radius` 的倍数。 |
| `split_min_component_size` | cluster 内二次拆分时子团最小规模。 |
| `split_min_medoid_gap` | cluster 内二次拆分时子团之间的最小 medoid 间距。 |
| `existence_min_retained_count` | cluster 被视为“仍然存在”的最小 retained 成员数。 |
| `existence_min_anchor_core_count` | cluster existence gate 的最小 `anchor_core` 数。 |
| `existence_min_distinct_photo_count` | cluster existence gate 的最小不同照片数。 |
| `existence_min_support_ratio_p50` | cluster existence gate 的最小 `support_ratio_p50`。 |
| `existence_max_intra_photo_conflict_ratio` | cluster existence gate 允许的最大同图冲突占比。 |
| `attachment_max_distance` | attachment 到目标 final cluster 的最大距离。 |
| `attachment_candidate_knn_k` | attachment 支持率计算使用的邻域规模。 |
| `attachment_min_support_ratio` | attachment 的最低支持率。 |
| `attachment_min_separation_gap` | attachment 相对第二候选 cluster 的最小分离差。 |
| `materialize_min_anchor_core_count` | 允许直接物化的最小 `anchor_core` 数。 |
| `materialize_min_distinct_photo_count` | 允许直接物化的最小不同照片数。 |
| `materialize_max_compactness_p90` | 允许直接物化的 cluster 紧致度上限。 |
| `materialize_min_separation_gap` | 允许直接物化的 cluster 分离度下限。 |
| `materialize_max_boundary_ratio` | 允许直接物化的边界成员占比上限。 |
| `trusted_seed_min_quality` | trusted seed 最低质量。 |
| `trusted_seed_min_count` / `trusted_seed_max_count` | trusted seed 数量上下限。 |
| `trusted_seed_allow_boundary` | 是否允许边界成员进入 trusted seed 候选。 |

这组参数变化时，只需要新建 `identity_cluster_run`，不应重算 observation 快照。

## Phase 1 核心参数-证据矩阵

Phase 1 允许调的关键参数，必须同时满足四件事：

- 有唯一使用步骤。
- 有唯一计算口径。
- 有唯一持久化字段。
- 有唯一页面或导出呈现位置。

最小对照如下：

| 参数 | 使用步骤 | 计算口径 | 落库字段 | 页面 / 导出字段 |
| --- | --- | --- | --- | --- |
| `discovery_knn_k` | raw cluster 发现、成员 `support_ratio` 计算 | mutual kNN 与 `effective_neighbor_count` 统一按该值截断 | `identity_cluster_run.summary_json`、`identity_cluster_member.support_ratio` | run summary、cluster member 明细、neighbor manifest |
| `density_min_samples` | `density_radius_i` 计算 | 第 `density_min_samples` 个近邻距离 | `identity_cluster_member.density_radius` | cluster member 明细、neighbor manifest |
| `intra_photo_conflict_policy_version` | support 过滤、split、attachment、existence gate | 同图 observation 是否可共存及是否计入支持率的统一规则 | `identity_cluster_run.summary_json`、`identity_cluster_member.decision_reason_code` | run profile 详情、cluster member 明细 |
| `split_min_component_size` / `split_min_medoid_gap` | cluster 内二次拆分 | 仅在 split graph 连通分量上判定 | `identity_cluster_lineage.reason_code`、`identity_cluster.summary_json` | lineage 视图、cluster 详情 |
| `existence_*` | cleaned -> final existence gate | retained 数、`anchor_core` 数、不同照片数、`support_ratio_p50`、同图冲突率 | `identity_cluster.cluster_state`、`identity_cluster.discard_reason_code`、`identity_cluster_resolution.resolution_reason` | run summary、discarded cluster 列表 |
| `attachment_candidate_knn_k` / `attachment_*` | attachment 判定 | `attachment_support_ratio`、`separation_gap`、距离阈值 | `identity_cluster_member.attachment_support_ratio`、`identity_cluster_member.decision_status` | cluster member 明细、neighbor manifest |
| `materialize_*` | final cluster 处置 | `anchor_core_count`、`distinct_photo_count`、`compactness_p90`、`separation_gap`、`boundary_ratio` | `identity_cluster_resolution.resolution_state`、`identity_cluster_resolution.resolution_reason` | materialized / review_pending 列表 |
| `trusted_seed_*` | trusted seed 选择与 publish bundle 构建 | 候选排序、去重、截断与最小数量判断 | `identity_cluster_resolution.trusted_seed_candidate_count`、`identity_cluster_resolution.trusted_seed_count` | cluster 详情、seed 审计信息 |

## 算法契约

v3.1 可以继续采用 HDBSCAN 风格的思路，但不能只停留在“像 HDBSCAN”这一层。

至少在契约上，算法必须落到可实现、可落库、可展示、可对比的计算口径。

## 1. 输入池

### `core_discovery_pool`

用于发现 raw cluster。

特征：

- 质量达标。
- 去重后保留下来的代表 observation。
- 有效 embedding 完整。

### `attachment_pool`

用于后吸附，不参与 raw cluster 发现。

特征：

- embedding 有效。
- 质量不足以定义 cluster，但仍有归属价值。

### `excluded_pool`

直接不参与本轮 bootstrap。

常见原因：

- embedding 缺失或损坏。
- 质量过低。
- 被同图 / burst / exact duplicate 去重折叠。
- 命中前置排除规则。

## 2. Raw Cluster 发现

在 `core_discovery_pool` 上构建 kNN 图。

对任一 observation `i`，定义：

- `density_radius_i`
  - `i` 到其第 `density_min_samples` 个近邻的距离。
- `local_density_score_i`
  - `1 / max(density_radius_i, epsilon)`。

raw cluster 发现规则固定为：

- 只在 mutual kNN 邻边上考虑连通。
- 当 `distance(i, j) <= max(density_radius_i, density_radius_j)` 时，`i` 与 `j` 才允许进入同一密度连通结构。
- 满足 `raw_cluster_min_size` 与 `raw_cluster_min_distinct_photo_count` 的连通分量，落为 `raw` stage cluster。
- 不满足上述门槛的 observation，落为 `noise` 或 `discarded before raw`，并在 member / pool 层留下原因。

这一定义保证：

- raw cluster 不再依赖旧的 `top2 + margin` 局部拒边。
- “边界模糊”与“完全无 cluster”可以区分。

## 3. 成员指标

每个进入 raw cluster 的 observation，至少要计算以下指标并落库：

- `distance_to_medoid`
  - 到当前 cluster medoid 的距离。
- `support_ratio`
  - 其前 `discovery_knn_k` 个有效邻居中，属于当前 cluster 的占比。
- `nearest_competing_cluster_distance`
  - 到最近竞争 cluster medoid 的距离。
- `separation_gap`
  - `nearest_competing_cluster_distance - distance_to_medoid`。
- `source_pool_kind`
  - `core_discovery` / `attachment` / `excluded`。

其中：

- medoid 定义为“cluster 内到其他成员距离和最小的 observation”。
- `support_ratio` 定义为：
  - `cluster_neighbor_count / max(1, effective_neighbor_count)`
  - 其中 `effective_neighbor_count = min(discovery_knn_k, candidate_neighbor_count_after_filters)`
- `candidate_neighbor_count_after_filters` 必须排除：
  - 自身 observation
  - 已被 observation 快照折叠为 shadow 的重复 observation
  - 由 `intra_photo_conflict_policy_version` 判定为不可同时成立的同图冲突 observation
- `support_ratio` 只用于 raw / cleaned 成员保留，不再与 attachment 共用一套 `k`。

## 4. `anchor_core`、`core`、`boundary`

raw cluster 成立后，先根据成员指标划出三层 retained discovery 成员：

- `anchor_core`
  - `support_ratio >= anchor_core_min_support_ratio`
  - `distance_to_medoid <= anchor_core_radius`
- `core`
  - `support_ratio >= core_min_support_ratio`
  - `distance_to_medoid <= boundary_radius`
  - 但不属于 `anchor_core`
- `boundary`
  - `support_ratio >= boundary_min_support_ratio`
  - `distance_to_medoid <= boundary_radius`
  - 但不属于 `anchor_core` 或 `core`

其中：

- `anchor_core_radius`
  - 当前 raw cluster 中 `distance_to_medoid` 的 `anchor_core_radius_quantile` 分位点。
- `boundary_radius`
  - `anchor_core_radius * boundary_radius_multiplier`。

未满足 `boundary` 条件的 raw 成员，进入 `excluded`，必须写明 `decision_reason_code`。

当 `decision_status = excluded` 时，`decision_reason_code` 就是页面和导出里展示的 `exclusion_reason`。

`decision_reason_code` 的最小枚举包括：

- `low_support_ratio`
- `outside_boundary_radius`
- `photo_conflict_after_cleaning`
- `split_into_other_child`
- `duplicate_shadow`

## 5. Cluster 内二次拆分

为了避免桥接 observation 把两个不同人误并成一个大团，raw cluster 后必须允许一次明确的二次拆分。

拆分契约如下：

- 仅用 `anchor_core + core` 成员构建清洗图。
- split graph 的边定义复用 raw discovery 阶段的 mutual kNN 候选边，但要同时满足：
  - 两端成员都属于 `anchor_core + core`
  - `distance(i, j) <= boundary_radius`
  - 不命中 `intra_photo_conflict_policy_version` 的禁止规则
- 在该图上取连通分量。
- 连通分量满足 `split_min_component_size`，且分量 medoid 之间距离大于 `split_min_medoid_gap` 时，拆成多个 `cleaned` child cluster。
- 原 `raw` cluster 自身状态记为 `split`，通过 `identity_cluster_lineage` 指向各 child。
- 如果拆分条件不成立，则保留单个 `cleaned` cluster。

这一步的目标是：

- raw cluster 先保住密度团。
- cleaned cluster 再处理“一个团里是否其实有两个人”。

## 6. Cluster Existence Gate

`review_pending` 与 `discarded` 的边界不能靠语义描述，必须靠同一组 existence gate 判断。

existence gate 的判定时机固定为：

- raw cluster 完成成员清洗与必要的二次拆分之后；
- attachment 之前；
- 从 `cleaned` 进入 `final` 之前。

一个 `cleaned` cluster 被视为“仍然存在”，必须同时满足：

- `retained_member_count >= existence_min_retained_count`
- `anchor_core_count >= existence_min_anchor_core_count`
- `distinct_photo_count >= existence_min_distinct_photo_count`
- `support_ratio_p50 >= existence_min_support_ratio_p50`
- `intra_photo_conflict_ratio <= existence_max_intra_photo_conflict_ratio`

其中：

- `intra_photo_conflict_ratio`
  - retained 成员中，按 `intra_photo_conflict_policy_version` 被判定为冲突的 observation 对数，占全部 retained observation 对数的比例。

如果 existence gate 失败：

- 必须创建一个 `final(discarded)` 节点用于审计，而不是让 cluster 在 `cleaned` 阶段直接消失。
- `identity_cluster_resolution.resolution_state = discarded`
- `identity_cluster.discard_reason_code` 与 `identity_cluster_resolution.resolution_reason` 必须一致。

`discard_reason_code` / `resolution_reason` 的最小枚举包括：

- `retained_too_small`
- `anchor_core_insufficient`
- `distinct_photo_insufficient`
- `support_ratio_too_low`
- `intra_photo_conflict_too_high`

只有通过 existence gate 的 cluster，才有资格进入 `final(active)` 并继续 attachment 与 cluster 级处置。

## 7. Final Cluster 定型

`cleaned` cluster 在排除不稳定成员后，先基于 retained discovery 成员生成 `final` cluster，再在其上追加 attachment 结果。

为了避免 attachment 反向影响成团判断，`final` cluster 必须同时区分两类指标：

- `final gate metrics`
  - 仅基于 `anchor_core + core + boundary` 这组 retained discovery 成员计算。
  - 用于 existence gate、materialize gate、run summary 和默认页面主指标。
- `post-attachment metrics`
  - 只表达 attachment 追加后的成员数量或展示信息。
  - 不得回写 `final gate metrics`，也不得改变 cluster 的 existence / materialize 判定。

除 `attachment_count` 外，本节列出的 `final` cluster 核心指标一律按 `final gate metrics` 口径计算并在 attachment 前冻结。

`final` cluster 必须至少落以下指标：

- `retained_member_count`
- `anchor_core_count`
- `core_count`
- `boundary_count`
- `attachment_count`
- `excluded_count`
- `distinct_photo_count`
- `compactness_p50`
- `compactness_p90`
- `support_ratio_p10`
- `support_ratio_p50`
- `intra_photo_conflict_ratio`
- `nearest_cluster_distance`
- `separation_gap`
- `boundary_ratio`

计算口径固定为：

- `retained_member_count`
  - 固定等于 `anchor_core_count + core_count + boundary_count`。
  - 不包含 attachment。
- `distinct_photo_count`
  - `final gate metrics` 对应 retained discovery 成员覆盖的不同照片数。
- `compactness_p50` / `compactness_p90`
  - `final gate metrics` 对应 retained discovery 成员 `distance_to_medoid` 的 p50 / p90。
- `support_ratio_p10` / `support_ratio_p50`
  - `final gate metrics` 对应 retained discovery 成员 `support_ratio` 的 p10 / p50。
- `nearest_cluster_distance`
  - 当前 run 中最近其他 `final` cluster medoid 的距离。
  - medoid 与竞争 cluster 都按 retained discovery 成员定义。
- `separation_gap`
  - `nearest_cluster_distance - compactness_p90`。
- `boundary_ratio`
  - `boundary_count / retained_member_count`。
- `intra_photo_conflict_ratio`
  - 按 `intra_photo_conflict_policy_version` 统计的 retained discovery 成员冲突对占比。
- `attachment_count`
  - post-attachment metric。
  - 只统计 attachment 条件通过后追加到该 `final` cluster 的成员数量。

## 8. Attachment 条件

`attachment_pool` 里的 observation 只能吸附到已经形成的 `final` cluster，不能反向决定 cluster 是否存在。

对 attachment observation，必须额外计算：

- `attachment_support_ratio`
  - 在 `attachment_candidate_knn_k` 邻域内，支持目标 `final` cluster 的邻居占比。

attachment 成立必须同时满足：

- 到目标 `final` cluster medoid 的距离 `<= attachment_max_distance`
- `attachment_support_ratio >= attachment_min_support_ratio`
- `separation_gap >= attachment_min_separation_gap`
- 不与现有 retained 成员产生由 `intra_photo_conflict_policy_version` 判定的不可接受同图冲突

attachment 成员的角色固定为 `attachment`。

attachment 的约束如下：

- 允许写入人物归属。
- 不得直接进入 `anchor_core`。
- 不得在 Phase 1 中直接参与 trusted seed 选择，除非后续明确扩展。
- 不得回写或重算以下 `final gate metrics`：
  - `retained_member_count`
  - `anchor_core_count`
  - `core_count`
  - `boundary_count`
  - `distinct_photo_count`
  - `compactness_p50`
  - `compactness_p90`
  - `support_ratio_p10`
  - `support_ratio_p50`
  - `intra_photo_conflict_ratio`
  - `nearest_cluster_distance`
  - `separation_gap`
  - `boundary_ratio`
- 页面或导出如需展示“最终总成员数”，应使用 `retained_member_count + attachment_count` 这一类展示口径；existence gate 与 materialize gate 仍只使用 attachment 前冻结的 `final gate metrics`。

## 9. Cluster 级处置

`final` cluster 的处置只有三类：

### `materialized`

`materialized` 在 v3.1 中的语义固定为：

- 该 cluster 已通过 existence gate；
- 该 cluster 已通过 materialize gate；
- 该 cluster 已准备好完整 publish bundle；
- 但只有在其所属 run 成为 `is_materialization_owner = 1` 后，publish bundle 才会变成 live person / prototype / ANN。

同时满足以下条件时，cluster 才能被记为 `materialized`。以下 materialize gate 一律使用 attachment 前冻结的 `final gate metrics`：

- `anchor_core_count >= materialize_min_anchor_core_count`
- `distinct_photo_count >= materialize_min_distinct_photo_count`
- `compactness_p90 <= materialize_max_compactness_p90`
- `separation_gap >= materialize_min_separation_gap`
- `boundary_ratio <= materialize_max_boundary_ratio`
- trusted seed 选择成功
- cluster 级 `prototype_bundle` 构建成功
- 所在 run 的 `ann_bundle` 构建成功，且该 cluster 的 prototype 已被纳入其中

### `review_pending`

满足“cluster 仍然存在”，但不满足直接物化门时，进入 `review_pending`。

典型原因包括：

- cluster 主体可信，但 `anchor_core` 太少。
- cluster 紧致度或分离度尚未过门。
- trusted seed 去重后不足。
- cluster 级 prepared bundle 或 run 级 `ann_bundle` 构建失败，需要回退。

### `discarded`

只有在以下情况下才允许 `discarded`：

- cleaned / final 阶段已经不存在稳定 `anchor_core`。
- 经过拆分后所有 child cluster 都不满足 cluster 存在条件。
- observation 只剩噪声或互相冲突，无法形成 final cluster。

`discarded` 表示“cluster 本身不存在”，不再表示“旧连边规则没有通过”。

## Materialized 安全门

如果 Phase 1 允许直接长出匿名 person，就必须把 materialized 做成硬门，而不是“先写人，后面慢慢补”。

`materialized` 的最小执行顺序固定为 `cluster-prepare -> run-prepare -> publish` 三阶段。

### cluster-prepare 阶段

cluster-prepare 在 run 执行时完成，目标是为单个 cluster 生成完整但尚未发布的 cluster 级 prepared bundle。

顺序固定如下：

1. 基于 `anchor_core` 优先、`core` 补齐的规则选择 trusted seed 候选。
2. 对 trusted seed 候选再次做同图 / burst / exact duplicate 去重。
3. 去重后如果 `trusted_seed_count < trusted_seed_min_count`，则本 cluster 直接回退为 `review_pending`。
4. 在 run-scoped staging 区生成该 cluster 的 `trusted_seed_bundle`、`prototype_bundle`、`person_publish_plan`。
5. 为上述产物写入 cluster 级 manifest，并完成 checksum 校验。
6. 只有当 cluster 级三类 bundle 全部成功且可校验时，该 cluster 才进入 run-prepare 候选；此时还不能直接记为 `materialized/prepared`。

cluster-prepare 明确禁止：

- 创建 live `person`
- 创建 live `person_face_assignment`
- 创建 live `person_trusted_sample`
- 切换 live prototype
- 切换 live ANN

### run-prepare 阶段

run-prepare 发生在同一轮 run 内、所有 cluster-prepare 候选收集完成之后，目标是生成整个 run 的 ANN prepared bundle。

顺序固定如下：

1. 收集当前 run 中全部 cluster-prepare 成功的 `prototype_bundle`。
2. 基于这些 prototype bundle 构建唯一的 run-scoped `ann_bundle`。
3. 为 `ann_bundle` 写入 run 级 manifest，并完成 checksum 校验。
4. 只有当 run 级 `ann_bundle` 成功且可校验时，相关 cluster 才允许把 `resolution_state` 记为 `materialized`，并把 `publish_state` 记为 `prepared`。
5. 如果 run 级 `ann_bundle` 构建失败，则所有受影响 cluster 必须统一回退为 `review_pending`，并记录 `run_ann_bundle_failed` 一类显式原因。

### publish 阶段

publish 阶段只在 `activate-run` 中发生，且只针对目标 owner run 的 `materialized` clusters。

publish 阶段必须在一个短事务或同一原子发布流程内完成以下动作：

1. 创建或激活该 run 的 live 匿名 `person`。
2. 写入 live `person_face_assignment`、`person_trusted_sample`、`person_cluster_origin`。
3. 切换 prototype live 指针。
4. 把 live ANN artifact 指针切换到该 run 的 prepared `ann_bundle`。
5. 把对应 `identity_cluster_resolution.publish_state` 从 `prepared` 切为 `published`。
6. 回收上一轮 owner run 的 live bootstrap-origin 匿名 person 与相关 live 指针。

如果 publish 任一步失败：

- 当前 cluster 及当前 run 不得成为新的 live materialization owner。
- 新写入的 live 人物层记录必须整批回滚或保持不可见。
- `identity_cluster_resolution` 保持 `resolution_state = materialized`，并把 `publish_state` 记为 `publish_failed`；
- 只有当失败根因属于 cluster 质量或 bundle 完整性本身时，才允许把该 cluster 降回 `review_pending`；
- 无论哪种分支，都不得出现 `published` 假阳性。
- `resolution_reason` / `publish_failure_reason` 必须记录对应失败原因，例如 `prepared_bundle_checksum_mismatch`、`person_publish_plan_invalid`、`live_ann_switch_failed`、`publish_transaction_failed`。

Phase 1 的 review 页面只允许看到两种 materialize 结果：

- 已生成完整 publish bundle 的 `materialized`
- 已明确表现为 `review_pending` 或 `publish_failed` 的失败结果

不允许出现“live 人物已创建但 publish bundle 半失败”的中间态。

## Trusted Seed 选择契约

trusted seed 选择必须具备确定性，不能在实现时临时决定排序规则。

最小契约如下：

1. trusted seed 候选池按成员角色分层：
   - 第一层：`anchor_core`
   - 第二层：`core`
   - 第三层：`boundary`，仅当 `trusted_seed_allow_boundary = 1` 时允许进入
2. 每层内部统一按以下顺序排序：
   - `quality_score_snapshot` 降序
   - `support_ratio` 降序
   - `distance_to_medoid` 升序
   - `observation_id` 升序
3. 排序后依次执行同图 / burst / exact duplicate 去重。
4. 去重后按顺序截断到 `trusted_seed_max_count`。
5. 如果最终 `trusted_seed_count < trusted_seed_min_count`，该 cluster 不得进入 `materialized`。

为了支持 review 和复盘，以下 seed 审计信息必须可落库、可展示：

- `trusted_seed_candidate_count`
- `trusted_seed_count`
- `trusted_seed_reject_distribution`
- 每个被选 seed 的 `seed_rank`

## 数据模型

## 运行时真相替换关系

v3.1 里以下旧对象不再作为运行时真相：

- `auto_cluster`
- `auto_cluster_member`
- `person.origin_cluster_id`
- 混合了质量分、bootstrap、自动归属、trusted gate 的 `identity_threshold_profile`

替换原则如下：

- observation 预处理真相使用 `identity_observation_profile` + `identity_observation_snapshot` + `identity_observation_pool_entry`
- bootstrap 真相使用 `identity_cluster_profile` + `identity_cluster_run` + `identity_cluster*`
- 人物来源真相使用 `person_cluster_origin`
- `person`、`person_face_assignment`、`person_trusted_sample`、`person_prototype` 保留为人物层真相，但不再反向承担 cluster 历史表达
- 人物层若仍需记录参数来源，必须引用 `source_run_id`、`source_observation_profile_id`、`source_cluster_profile_id` 这一类新真相，不再依赖 `threshold_profile_id`

换句话说，v3.1 不做“双写兼容”。

旧表可以迁移、归档或删除，但新代码不得继续依赖它们作为主语义。

## 新表语义

### `identity_observation_profile`

用途：

- 表达 observation 级质量分、分池、去重规则。

要求：

- 关键参数应显式成列，不应只藏在 JSON 中。
- profile 变化会导致 observation 快照失效。

### `identity_observation_snapshot`

用途：

- 表达某次 observation 级预处理的不可变快照。

必须至少记录：

- `observation_profile_id`
- observation 集合哈希或等价快照标识
- embedding 绑定
- 算法版本
- run summary

### `identity_observation_pool_entry`

用途：

- 表达 observation 在某个快照中的分池、去重、排除结果。

必须至少记录：

- `snapshot_id`
- `observation_id`
- `pool_kind`
- `quality_score_snapshot`
- `dedup_group_key`
- `representative_observation_id`
- `excluded_reason`
- `diagnostic_json`

### `identity_cluster_profile`

用途：

- 表达 bootstrap run 级参数。

要求：

- Phase 1 调参涉及的关键参数必须显式成列，便于页面和脚本直接对比。
- 允许保留 `diagnostic_json` 承载扩展字段，但不能把主参数藏进去。

### `identity_cluster_run`

用途：

- 表达某轮 bootstrap run 的不可变结果入口。

必须至少记录：

- `observation_snapshot_id`
- `cluster_profile_id`
- `algorithm_version`
- `run_status`
- `is_review_target`
- `review_selected_at`
- `is_materialization_owner`
- `supersedes_run_id`
- `started_at`
- `finished_at`
- `activated_at`
- `prepared_artifact_root`
- `prepared_ann_manifest_json`
- `summary_json`
- `failure_json`

### `identity_cluster`

用途：

- 表达 cluster 生命周期中的一个节点，而不是单纯“最后一个 cluster”。

必须至少记录：

- `run_id`
- `cluster_stage`
- `cluster_state`
- `member_count`
- `retained_member_count`
- `anchor_core_count`
- `core_count`
- `boundary_count`
- `attachment_count`
- `excluded_count`
- `distinct_photo_count`
- `compactness_p50`
- `compactness_p90`
- `support_ratio_p10`
- `support_ratio_p50`
- `intra_photo_conflict_ratio`
- `nearest_cluster_distance`
- `separation_gap`
- `boundary_ratio`
- `discard_reason_code`
- `representative_observation_id`
- `summary_json`

### `identity_cluster_lineage`

用途：

- 表达 raw -> cleaned -> final、split、merge 等演化关系。

必须至少记录：

- `parent_cluster_id`
- `child_cluster_id`
- `relation_kind`
- `reason_code`

### `identity_cluster_member`

用途：

- 表达成员在某个 cluster 节点下的角色、指标与去留结论。

必须至少记录：

- `cluster_id`
- `observation_id`
- `source_pool_kind`
- `quality_score_snapshot`
- `member_role`
- `decision_status`
- `distance_to_medoid`
- `density_radius`
- `support_ratio`
- `attachment_support_ratio`
- `nearest_competing_cluster_distance`
- `separation_gap`
- `decision_reason_code`
- `is_trusted_seed_candidate`
- `is_selected_trusted_seed`
- `seed_rank`
- `is_representative`

### `identity_cluster_resolution`

用途：

- 表达 final cluster 的产品化处置，而不是覆盖 cluster 本身。

必须至少记录：

- `cluster_id`
- `resolution_state`
- `resolution_reason`
- `publish_state`
- `publish_failure_reason`
- `person_id`
- `source_run_id`
- `trusted_seed_count`
- `trusted_seed_candidate_count`
- `trusted_seed_reject_distribution_json`
- `prepared_bundle_manifest_json`
- `prototype_status`
- `ann_status`
- `resolved_at`
- `published_at`

其中：

- `person_id`
  - 在 `publish_state = prepared` 或 `publish_failed` 时允许为空；
  - 只有 `publish_state = published` 时才必须非空。
- `prepared_bundle_manifest_json`
  - 汇总该 cluster 的 `trusted_seed_bundle`、`prototype_bundle`、`person_publish_plan` 的 staging manifest。
- `ann_status`
  - 表达该 cluster 的 prototype 是否已被纳入所属 run 的 ANN prepared bundle / live ANN。
  - 不表示该 cluster 自己拥有独立 ANN 文件。

### `person_cluster_origin`

用途：

- 表达人物从哪个 cluster 长出，以及后续 merge / review 如何吸收来源。

必须至少记录：

- `person_id`
- `origin_cluster_id`
- `source_run_id`
- `origin_kind`
- `active`

## 现有人物层字段收敛

Phase 1 虽然不完成全部人物产品功能，但只要允许 materialize，就必须把人物层的来源追溯写清楚。

最小约束如下：

- `person.origin_cluster_id` 必须移除，不再承载来源真相。
- `person_face_assignment.threshold_profile_id`
  - 如 schema 暂时保留，只能作为历史兼容字段；
  - 新逻辑不得再依赖它判断 bootstrap 来源。
- `person_trusted_sample.threshold_profile_id`
  - 同样只允许保留为历史审计字段；
  - 新逻辑不得再依赖它表达 Phase 1 materialize 的参数来源。
- Phase 1 新写入的人物层记录，至少要能追溯到：
  - `source_run_id`
  - `source_cluster_id`
  - 必要时再补 `source_observation_profile_id` 与 `source_cluster_profile_id`

换句话说：

- `threshold_profile_id` 可以作为遗留字段暂存；
- 但 v3.1 的运行契约不能再把它当真相，也不能让它成为新页面或新脚本的查询入口。

## 状态机

## `cluster_stage`

`cluster_stage` 表达 cluster 在算法流水线中的阶段：

- `raw`
- `cleaned`
- `final`

## `cluster_state`

`cluster_state` 表达该节点在 lineage 中的生命周期：

- `active`
- `split`
- `merged`
- `discarded`

Phase 1 至少要完整用到：

- `active`
- `split`
- `discarded`

`merged` 预留给后续扩展，不影响本次设计闭环。

## `resolution_state`

`resolution_state` 表达 final cluster 的产品化处置：

- `unresolved`
- `materialized`
- `review_pending`
- `ignored`
- `discarded`

Phase 1 至少要完整用到：

- `materialized`
- `review_pending`
- `discarded`

`ignored` 预留给 Phase 2 的人工处理。

## `publish_state`

`publish_state` 表达 `materialized` cluster 的发布阶段：

- `not_applicable`
- `prepared`
- `published`
- `publish_failed`

约束如下：

- `resolution_state != materialized` 时，`publish_state` 必须为 `not_applicable`。
- `resolution_state = materialized` 时，`publish_state` 只能在 `prepared`、`published`、`publish_failed` 三者中取值。
- `publish_state = prepared` 表示该 cluster 的 cluster 级 prepared bundle 与所属 run 的 ANN prepared bundle 都已通过校验，但尚未发布为 live 结果。
- `publish_state = publish_failed` 只表示 `activate-run` 阶段的发布失败，不再承载 cluster-prepare / run-prepare 阶段的失败。

## `member_role`

- `anchor_core`
- `core`
- `boundary`
- `attachment`

## `decision_status`

- `retained`
- `excluded`
- `deferred`

## `origin_kind`

- `bootstrap_materialize`
- `review_materialize`
- `merge_adopt`

## 状态迁移约束

最小迁移闭环如下：

- `raw(active)` -> `cleaned(active)`
- `raw(active)` -> `raw(split)` + 多个 `cleaned(active)`
- `cleaned(active)` -> `final(active)`
- `cleaned(active)` -> `final(discarded)`
- `final(active)` -> `resolution_state = unresolved`
- `resolution_state = unresolved` -> `resolution_state = materialized`
- `resolution_state = unresolved` -> `resolution_state = review_pending`
- `final(discarded)` -> `resolution_state = discarded`

约束如下：

- 只有 `final` stage cluster 才允许进入 `identity_cluster_resolution`。
- `final(active)` 允许先进入 `unresolved`，再由 prepare/gate 结果转入 `materialized` 或 `review_pending`。
- `resolution_state = unresolved` 只允许转入 `materialized` 或 `review_pending`。
- `final(discarded)` 只允许进入 `resolution_state = discarded`。
- 被 `split` 的父 cluster 不再直接参与 resolution。
- `resolution_state = discarded` 必须能追溯到 cluster existence gate 失败，而不是随意手填。

## Review 工具与正式验收面

Phase 1 的 review 工具不是附属品，而是正式验收接口。

## `/identity-tuning`

`/identity-tuning` 必须从“latest batch 摘要页”升级成“指定 run 的 bootstrap 证据页”。

页面最少要支持：

- 通过 `run_id` 查看任意历史 run。
- 默认打开当前 review target run。
- 展示 observation snapshot、observation profile、cluster profile 的绑定关系。
- 展示 run 级摘要：
  - observation 总数
  - `core_discovery` / `attachment` / `excluded` 数量
  - dedup drop 分布
  - raw / cleaned / final cluster 数量
  - `materialized` / `review_pending` / `discarded` 数量
  - 当前 run 是否为 `is_materialization_owner`
- 展示 cluster 级证据：
  - raw -> cleaned -> final lineage
  - `anchor_core` / `core` / `boundary` / `attachment` / `excluded` 数量
  - existence gate 是否通过，以及失败原因
  - `compactness_p90`
  - `separation_gap`
  - `boundary_ratio`
  - trusted seed 候选数、保留数、拒绝分布
  - `publish_state`
  - prototype / ANN 状态
  - `resolution_reason`
- 展示成员级证据：
  - 代表 observation
  - 被保留成员
  - 被排除成员
  - 排除原因分布
  - trusted seed 候选与最终 seed 排名
  - 每个成员的 `support_ratio`、`distance_to_medoid`、`separation_gap`

页面定位应保持只读，不承担写操作。

## `scripts/export_observation_neighbors.py`

该脚本必须从“导出 observation 最近邻预览”升级成“带 run 语境的证据导出工具”。

最小要求：

- 支持 `--run-id`，默认取当前 review target run。
- 保留 `--observation-ids`，并新增 `--cluster-id` 作为批量导出入口。
- manifest 中必须写入：
  - `run_id`
  - `observation_snapshot_id`
  - `observation_profile_id`
  - `cluster_profile_id`
  - `cluster_id`
  - `cluster_stage`
  - `member_role`
  - `decision_status`
  - `exclusion_reason`
  - `publish_state`
  - `is_selected_trusted_seed`
  - `seed_rank`
  - 相关距离与质量值
- 导出对象必须能覆盖：
  - 代表 observation
  - retained 成员
  - excluded 成员
  - 竞争近邻证据

这样，人工在 review cluster 时，才能把页面指标与真实邻域证据对起来看。

## Phase 1 验收要求

Phase 1 至少要能支持以下验收动作：

1. 在同一 observation 快照上用两组不同 `identity_cluster_profile` 跑出两个 `run_id`。
2. 在 `/identity-tuning` 中查看任一 `run_id`，并通过 `select-run` 切换默认 review 对象。
3. 在同一 cluster 上看到 raw -> cleaned -> final 的 lineage 和成员排除原因。
4. 用 neighbor 导出脚本导出该 cluster 的代表 observation、排除成员与竞争近邻。
5. 对任一 `materialized` cluster，确认其 cluster 级 prepared bundle 与所属 run 的 ANN prepared bundle 都已完整生成，且 `publish_state = prepared` 或 `published`。
6. 通过 `activate-run` 发布某轮 run 的 live 物化结果，并确认旧 owner 的 live 结果已退场。
7. 对 owner run 中任一已发布 `materialized` cluster，确认匿名 person、trusted seed、prototype、ANN 都已成功切换到 live。
8. 对任一 bundle 或 publish 失败的 cluster，确认它只会表现为 `review_pending` 或 `publish_failed` 审计状态，不会留下半成品 person。

## 与现有 v3 的关系

v3.1 不是否定 v3 的方向，而是把 v3 真正拆完整。

可以复用的部分包括：

- quality score 计算能力
- observation 质量回填链路
- 同图 / burst 去重经验
- trusted sample -> prototype -> ANN 的基本下游链路

明确废弃的部分包括：

- 旧 bootstrap 的局部连边主导规则
- `auto_cluster*` 作为主真相
- `person.origin_cluster_id` 作为人物来源表达
- “latest bootstrap batch + latest anonymous people” 这一类以 batch 为中心的调参视角
- 把 observation 预处理参数与 bootstrap 参数混装在一个 profile 中

v3.1 的真正收口方式是：

- 复用 observation 级能力
- 重做 bootstrap 契约
- 把 review 面抬到正式验收入口
- 再由 Phase 2 消费这一套真相完成完整人物系统

## 结论

v3.1 Phase 1 的主语不是“把 bootstrap 阈值再调一轮”，而是：

- 先固定一份可复用 observation 预处理快照；
- 再围绕它反复创建新的 cluster run；
- 每轮 run 都可被明确 review；
- 每个 cluster 都有 raw -> cleaned -> final 的证据链；
- 只有完整通过安全门的结果才允许长成人物。

只有把这一层闭合，Phase 2 的日常归属、review、导出和人物维护才有稳定真相可用。
