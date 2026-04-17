# HikBox Pictures v3.1 Phase 2 身份系统收口设计文档

## 目标

v3.1 phase2 的目标不是再补一批零散功能，而是在 phase1 已经交付的 observation / run / cluster 契约之上，把日常身份系统真正收口成一套稳定产品语义。

本阶段必须同时完成以下四件事：

- 让日常扫描、自动归属、增量新人物发现、review queue、人物维护、导出与正式 WebUI 使用同一套运行时真相。
- 把 phase1 的 bootstrap 真相变成 phase2 的输入前提，而不是在 phase2 再发明一套并行 cluster 语义。
- 明确替换当前代码中仍然保留的 v2 / v3 过渡语义，例如 `possible_split`、`split assignment`、通用 `resolve review`、前端临时 regroup 的 `new_person`。
- 让人物层、trusted pool、prototype、ANN 和导出链路在日常操作下保持一致，不再依赖一次性重建脚本维持表面可用。

## 设计前提

本设计建立在以下前提上：

- 2026-04-17 的 v3.1 phase1 设计已经完整交付。
- phase1 中的 `identity_observation_profile`、`identity_observation_snapshot`、`identity_cluster_profile`、`identity_cluster_run`、`identity_cluster*`、`identity_cluster_resolution`、`person_cluster_origin` 都已经是稳定真相。
- phase1 已经存在且只存在一个 bootstrap `is_materialization_owner = 1` 的 owner run，可提供当前 live bootstrap 基线。
- `/identity-tuning`、`select-run`、`activate-run` 继续保留为 phase1 的诊断与基线切换接口，但不是 phase2 日常产品操作入口。

同时也要正视当前代码现状：

- 当前仓库里的 scan / review / people / export 代码仍然大量停留在 v2 / v3 过渡语义上。
- phase2 不以“尽量复用现有旧逻辑”为目标，而是以“明确替换旧语义”为目标。
- 只要 phase1 契约和当前代码现状冲突，phase2 一律向 phase1 契约收敛，不做双写兼容。

## 非目标

本设计明确不覆盖以下内容：

- 不重新设计 phase1 的 observation 预处理、bootstrap 成团、cluster lineage 或 prepare / publish 安全门。
- 不重新把 observation 参数、bootstrap 参数、日常 assignment 参数揉回一个大 profile。
- 不保留 `possible_split` 队列，也不保留从人物详情页直接 `split` 出新人物的旧语义。
- 不继续使用通用 `resolve` / `dismiss` 承担有目标副作用的 review 写路径。
- 不把 phase2 已上线后的“再次 full bootstrap owner 切换并自动对齐全部人工增量结果”纳入本阶段范围。
- 不补移动端专用交互；本阶段仍以桌面 Safari 近似布局为准。

## 当前代码现状与 Phase 2 收敛方向

| 当前现状 | 问题 | Phase 2 收敛方式 |
| --- | --- | --- |
| `asset_stage_runner` 仍按 `auto_assign / review / new_person_candidate` 三分类运行，核心只看 ANN 距离阈值。 | 既没有消费 phase1 owner run，也没有独立 runtime policy，更没有增量 cluster run。 | 替换为“owner run + runtime profile + incremental cluster run”三段式日常流水线。 |
| `new_person` review 仍是 observation-backed，WebUI 再临时把多条 review regroup。 | review 单位不稳定，无法追溯到 phase1 的 cluster 真相。 | `new_person` 改为 cluster-backed，前端不再 regroup，直接消费持久化 `identity_cluster_resolution(review_pending)`。 |
| `review_item` 仍围绕通用 `resolve / dismiss / ignore` 组织。 | 有目标副作用的动作没有显式目标参数，状态语义也不完整。 | review 只保留显式目标动作，`ignore` 不再落成 `dismissed`。 |
| `possible_split` 仍存在于 schema、查询和页面。 | 与 v3.1 phase2 的人物维护方式冲突。 | 整体删除 `possible_split` 队列、动作、页面和 API。 |
| `PersonTruthService` 与 `routes_people` 仍保留 `split` 与裸 `lock`。 | 继续把人物纠错当成“即时拆人”，而不是“排除 -> 回到未归属池 -> 再进 cluster/review”。 | 人物详情页只保留 `人工确认归属`、`排除归属`、`merge`、`rename`。 |
| 当前 `identity_threshold_profile` 仍混装 quality、bootstrap、assignment、trusted、merge 阈值。 | 与 phase1 “参数分层”设计直接冲突。 | phase2 新增独立 `identity_runtime_profile`，并把旧表中的日常阈值字段降为历史遗留。 |
| export / WebUI 仍主要围绕 active assignment 查询，缺少 trusted sample / prototype 状态。 | 无法体现谁能进入 ANN，谁只是人工档案。 | 正式页面和导出页都要区分 active assignment、active trusted sample、prototype / ANN 状态。 |

## Phase 2 范围

phase2 覆盖以下五个子系统：

- 日常扫描后的 observation 级决策、自动归属与增量新人物发现。
- 正式 review queue、显式动作 API 与 review 状态回写。
- 人物详情页、人物真相维护、trusted pool 更新与 prototype / ANN 重建。
- 导出模板、导出账本、人物 merge 后的下游一致性。
- 正式 WebUI、CLI、README 和验证矩阵收口。

## 运行时总架构

## 第一层：Phase 1 不可变真相

phase1 已经负责表达：

- observation 级真相：`identity_observation_profile`、`identity_observation_snapshot`、`identity_observation_pool_entry`
- bootstrap / incremental 成团真相：`identity_cluster_profile`、`identity_cluster_run`、`identity_cluster`、`identity_cluster_lineage`、`identity_cluster_member`、`identity_cluster_resolution`
- 人物来源真相：`person_cluster_origin`

phase2 不重定义这些对象，只消费它们。

## 第二层：Phase 2 日常运行策略

phase2 新增独立对象：

- `identity_runtime_profile`

它只表达日常运行策略，不表达 observation 预处理，也不表达 bootstrap 成团参数。

它负责以下决策：

- 日常 auto assignment 的质量门、距离门、margin 门、照片冲突门
- `low_confidence_assignment` 的入队门
- `manual_confirm` trusted gate
- `possible_merge` 生成门

之所以必须新增这一层，而不是回退到旧 `identity_threshold_profile`，原因很简单：

- phase1 已经把 observation 参数和 cluster 参数拆开；
- phase2 又需要一组独立的日常 decision gate；
- 如果继续把三层参数混在一起，phase1 的拆分意义会被直接破坏。

## 第三层：Live 人物层真相

phase2 的 live 人物层真相固定为：

- `person`
- `person_face_assignment`
- `person_trusted_sample`
- `person_prototype`
- `person_cluster_origin`

这几张表的职责必须严格区分：

- `person` 表达当前人物档案本身。
- `person_face_assignment` 表达 observation 当前归属。
- `person_trusted_sample` 表达哪些 observation 有资格定义该人物的 prototype。
- `person_prototype` 表达当前 active trusted pool 推导出的派生产物。
- `person_cluster_origin` 表达这个人物吸收过哪些 cluster 来源，以及来源属于哪一类动作。

## Owner Run 与日常运行的关系

phase2 仍然保留 phase1 的 bootstrap owner run 概念，但语义收窄为：

- owner run 只表达当前 live bootstrap 基线来自哪一轮 full bootstrap。
- 日常 scan / review / person / export 一律建立在这个基线之上继续增量演化。
- incremental run、review materialize、manual confirm 不会去切换 `is_materialization_owner`。

换句话说，phase2 的 live 库不再等价于“单一 run 的完整物化结果”，而是：

- owner run 的 bootstrap baseline
- 加上 phase2 日常产生的增量人物和人工修正

这也是本阶段的一个明确边界：

- phase2 不解决“库里已经存在 phase2 增量与人工结果之后，再次切换 bootstrap owner run 并自动对齐所有 live delta”的问题。
- phase2 上线后，如需重新激活新的 bootstrap owner run，应视为后续单独的 reconciliation 设计，不属于本阶段日常路径。

### `activate-run` 防误触护栏（Phase 2 强约束）

Phase 2 上线后，`activate-run` 不能再被当作“日常试错入口”使用。为避免误切 owner run 覆盖 live 增量结果，必须增加硬护栏：

- 一旦检测到任何 phase2 live delta，`activate-run` 必须拒绝执行并返回显式错误（例如 `phase2_delta_present`）。
- phase2 live delta 的最小判定集合至少包括：
  - 存在 `person_cluster_origin.origin_kind IN ('incremental_materialize', 'review_materialize', 'review_adopt', 'merge_adopt')` 的 active 记录；
  - 存在 `assignment_source IN ('incremental', 'manual', 'merge')` 的 active assignment；
  - 存在 `trust_source IN ('incremental_seed', 'review_seed', 'manual_confirm')` 的 active trusted sample。
- `select-run` 仍可用于诊断视图切换，但不得改动 live person/prototype/ANN。
- 解除该护栏只能通过后续专门的 reconciliation 入口；phase2 范围内不提供 `--force activate-run`。

## 数据契约

## `identity_runtime_profile`

phase2 必须新增 `identity_runtime_profile`，至少包含以下字段：

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

约束如下：

- 任意时刻最多只有一个 `active = 1` 的 runtime profile。
- active runtime profile 的 embedding 绑定必须与当前 owner run 绑定的 embedding 空间一致。
- active runtime profile 必须显式引用 `source_materialization_owner_run_id`，不允许“脱离 owner run 独立存在”。
- `threshold_profile_id` 不再是 phase2 运行时查询入口；需要日常 decision 参数时，只能查 active `identity_runtime_profile`。

### `identity_runtime_profile` 生命周期与管理入口

phase2 必须提供正式管理入口，禁止通过手工 SQL 作为常规运维路径。最小生命周期如下（逻辑态，可由 `active` 与审计时间推导）：

- `draft`：新建后未激活，仅可校验与预览。
- `active`：当前唯一生效 profile。
- `retired`：历史 profile，不再参与运行时查询。

最小操作集合如下：

- `create`: 创建 draft profile。
- `validate`: 校验 embedding 绑定、阈值取值域与跨字段约束。
- `activate`: 原子切换 active（旧 active -> retired，新 profile -> active）。
- `list/get`: 用于审计与回溯。

激活硬约束：

- 若当前存在 `scan_session.status = 'running'`，禁止激活，返回显式错误（例如 `scan_session_running`）。
- 激活必须在单事务中完成，不能出现短暂“零 active”或“双 active”窗口。

## `identity_observation_snapshot`

phase2 在不改动 phase1 不可变契约的前提下，为 snapshot 增加语义字段：

- `snapshot_kind`
  - `bootstrap`
  - `incremental`
- `trigger_scan_session_id`（可空）

约束如下：

- `bootstrap` snapshot 只用于 full bootstrap run。
- `incremental` snapshot 只用于日常扫描收口后新增的未归属候选子集。
- snapshot 仍然不可变，任何 observation 集合变化都要创建新 snapshot，而不是回写旧 snapshot。

## `scan_session` 生命周期（Phase 2 口径）

phase2 的“扫描会话收口”以 `scan_session` 为唯一时间边界。最小状态机沿用现有实现：

- `pending -> running -> completed`
- `pending/running/paused -> interrupted`
- `pending/running/paused/interrupted -> failed`
- `pending/running/paused/interrupted -> abandoned`

约束如下：

- phase2 明确不支持多扫描会话并发；同一时刻最多一个 `running` session。
- 只有 session 进入 `completed/failed` 这类终态后，才允许触发本次会话的 incremental snapshot 与 incremental run 收口。
- 进程重启后若发现陈旧 `running` session，必须先收敛为 `interrupted` 再决定恢复或放弃，不允许直接并发启动新 session。
- `trigger_scan_session_id` 必须指向触发该 snapshot/run 的唯一 session，且不可回填修改。

## `identity_cluster_run`

phase2 在现有 run 契约上增加：

- `run_kind`
  - `bootstrap`
  - `incremental`
- `trigger_scan_session_id`（可空）
- `base_owner_run_id`（可空）

约束如下：

- 只有 `run_kind = bootstrap` 的 run 才允许 `is_review_target = 1` 或 `is_materialization_owner = 1`。
- `run_kind = incremental` 的 run 只能用于日常新人物发现、`new_person` review 和增量 auto materialize。
- incremental run 必须显式记录它是基于哪个 `base_owner_run_id` 的 live 基线生成。
- 上述约束必须有数据库级保护（CHECK/触发器），不能只依赖应用层：
  - `run_kind = incremental` 时，`is_review_target = 0` 且 `is_materialization_owner = 0`。
  - `run_kind = bootstrap` 才允许参与 owner/review_target 单例索引竞争。

## `identity_cluster_resolution`

phase1 的 `resolution_state` 已定义包含 `unresolved / materialized / review_pending / ignored / discarded`。phase2 在日常收口中继续沿用该结构，并补齐一类明确结果：

- `adopted`

其语义固定为：

- cluster 本身没有新建人物；
- 但该 cluster 已经被明确吸收到某个现有人物中；
- `person_id` 必须指向吸收目标人物。

phase2 还要显式区分“run 收口初始态”和“人工处置终态”：

- incremental run 在收口时可写入的初始态只允许：`materialized / review_pending / discarded`。
- 只有从 `review_pending` 出发，才允许在 review / people 动作中转为：`materialized / adopted / ignored`。
- `discarded` 只能由系统在成团判定阶段写入，不允许由人工动作直接改写为 `discarded`。

因此 phase2 的完整 `resolution_state` 集合为：

- `materialized`
- `adopted`
- `review_pending`
- `ignored`
- `discarded`

phase1 到 phase2 的 breaking 迁移口径必须显式如下：

- 历史 `resolution_state = unresolved` 必须在迁移窗口内收敛为 `review_pending` 或 `discarded`（按 phase1 最终 gate 结果回填）。
- 不允许把 `publish_state` 的取值（`not_applicable/prepared/publish_failed/published`）误写入 `resolution_state`。

`publish_state` 在 phase2 必须继续严格沿用 phase1 语义，不允许出现第二套解释口径：

- `resolution_state != materialized` 时，`publish_state` 必须为 `not_applicable`。
- `resolution_state = materialized` 时，`publish_state` 只能是 `prepared / published / publish_failed`。
- phase2 的 `create-person` 与 incremental auto materialize 在成功路径结束时，必须写入 `publish_state = published` 与 `published_at`。
- 任一副作用失败（assignment、trusted sample、prototype、ANN）都不得留下“已 published 但 ANN 不一致”的假成功状态；必须按 ANN 发布协议回滚或写入失败态并可恢复。
- `prepared / publish_failed` 在 phase2 只保留给 phase1 `activate-run` 发布链路，不用于日常 review 动作。

`resolution_reason` 至少要能区分：

- `bootstrap_materialized`
- `incremental_materialized`
- `review_created_person`
- `review_adopted_into_person`
- `review_ignored`
- `discarded_by_system`

## `person_cluster_origin`

phase1 已经把人物来源真相下沉到 `person_cluster_origin`（含 `active` 字段）。phase2 在 phase1 枚举基础上扩展 `origin_kind`，至少包括：

- `bootstrap_materialize`
- `incremental_materialize`
- `review_materialize`
- `review_adopt`
- `merge_adopt`

语义固定为：

- `bootstrap_materialize`：phase1 owner run 自动长出的人物来源。
- `incremental_materialize`：phase2 日常 incremental run 自动长出的人物来源。
- `review_materialize`：用户从 `new_person` review 选择“新建人物”。
- `review_adopt`：用户把 cluster 吸收到现有人物。
- `merge_adopt`：人物 merge 后，目标人物吸收源人物的 cluster 来源。

## `person`

phase2 沿用并显式固定 `person.status` 枚举：

- `active`
- `merged`
- `ignored`

最小状态迁移如下：

- `active -> merged`（仅 merge 动作触发）
- `active -> ignored`（明确忽略人物时触发）
- `ignored -> active`（显式恢复时触发）

补充规则：

- “无 assignment / 无 trusted sample / 无 prototype”的空壳人物，默认保持 `active`，不自动降级为 `ignored`。
- 空壳人物在人物列表中继续可见，但必须标注“当前不参与自动归属”。

## `person_face_assignment`

phase2 要求对人物归属表做以下收敛：

- `assignment_source` 收敛为：
  - `bootstrap`
  - `incremental`
  - `auto`
  - `manual`
  - `merge`
- 删除任何残留 `split` 语义。
- 新增或正式启用以下字段：
  - `runtime_profile_id`
  - `source_run_id`
  - `source_cluster_id`
  - `diagnostic_json`

约束如下：

- direct auto assignment 写 `assignment_source = auto`。
- phase1 owner run 自动 publish 的 assignment 写 `assignment_source = bootstrap`。
- incremental run 自动 materialize 的 assignment 写 `assignment_source = incremental`。
- review 与人物详情页确认后的 assignment 写 `assignment_source = manual` 且 `locked = 1`。
- merge 搬迁后的 assignment 写 `assignment_source = merge`。
- `runtime_profile_id` 只记录日常 runtime 决策；phase1 bootstrap materialize 可为空。
- `source_run_id` / `source_cluster_id` 必须足以追溯该 assignment 是从哪个 cluster / run 长出来的。

## `person_trusted_sample`

phase2 在 trusted sample 上补齐运行时来源：

- `runtime_profile_id`
- `source_run_id`
- `source_cluster_id`

并把 `trust_source` 收敛为：

- `bootstrap_seed`
- `incremental_seed`
- `review_seed`
- `manual_confirm`

语义如下：

- `bootstrap_seed`：phase1 owner run 自动 materialize 时选入。
- `incremental_seed`：incremental run 自动 materialize 时选入。
- `review_seed`：用户从 `new_person` cluster 新建人物时，由 cluster 证据直接入池。
- `manual_confirm`：已有目标人物上的单条或批量人工确认样本，经 trusted gate 后入池。

`trusted_block_burst_duplicate` 的判定语义固定为：

- 同一目标人物内，若两条样本来自同一 `photo_asset_id`，且采集时间差在 `burst_window_seconds` 内，视为同一 burst。
- 同一 burst 仅允许保留一条入池；保留规则为 `quality_score` 更高优先，若并列则 observation id 更小优先。

merge 时 trusted sample 的“迁移”语义固定为可审计的重建式：

- 不直接改写 source 记录 `person_id`；
- source 侧记录失活，target 侧新建对应 trusted sample；
- 新建记录默认保留原 `trust_source`（不新增 `merge_seed` 枚举），并在审计字段写明来源人物与触发动作（例如 `merge_from_person_id` 与 `source_review_id`）。

## `export_template_person`

phase2 虽不重做导出模型，但 merge 已对该表形成正式写路径，故需固定最小契约：

- 主键：`id`
- 业务键：`UNIQUE(template_id, person_id)`、`UNIQUE(template_id, position)`
- 外键：`template_id -> export_template.id`、`person_id -> person.id`

merge 自动重写规则：

- source->target 映射时，若同模板下 target 不存在，则把 source 绑定改写为 target 并重排 position；
- 若 target 已存在，则删除 source 绑定并重排 position；
- 不允许留下指向 `person.status = merged` 的模板绑定。

## `review_item`

phase2 的正式 review 表必须显式承载 cluster-backed 与 person-backed 语义。至少需要以下字段：

- `id`
- `review_type`
- `status`
- `resolution_action`
- `primary_person_id`
- `secondary_person_id`
- `resolved_person_id`
- `face_observation_id`
- `cluster_id`
- `source_run_id`
- `runtime_profile_id`
- `pair_person_low_id`
- `pair_person_high_id`
- `evidence_fingerprint`
- `payload_json`
- `priority`
- `created_at`
- `resolved_at`

其中：

- `review_type` 只允许：
  - `new_person`
  - `possible_merge`
  - `low_confidence_assignment`
- `status` 只允许：
  - `open`
  - `resolved`
  - `ignored`
  - `superseded`
- `pair_person_low_id / pair_person_high_id` 只用于 `possible_merge`，必须满足 `pair_person_low_id < pair_person_high_id`。
- `evidence_fingerprint` 必须可复算，且同一业务对象在证据未变化时保持稳定，用于 regeneration 去重与再入控制。

字段职责映射如下（防止不同实现各自解释）：

- `new_person`：
  - `cluster_id/source_run_id/runtime_profile_id` 必填；
  - `primary_person_id/secondary_person_id/face_observation_id` 默认为空。
- `low_confidence_assignment`：
  - `face_observation_id` 必填；
  - `primary_person_id` 固定表示当时 top1 候选；
  - `secondary_person_id` 固定表示当时 top2 候选（可空）。
- `possible_merge`：
  - `pair_person_low_id/pair_person_high_id` 必填且用于唯一性约束；
  - `primary_person_id/secondary_person_id` 只用于 UI 展示顺序，不参与唯一性判定，不得替代 pair 字段。

状态语义固定为：

- `resolved`：用户执行了明确动作，且副作用已经完成。
- `ignored`：用户明确选择忽略，本轮不再提示。
- `superseded`：系统因上游真相变化而作废旧 review，例如 observation 已归属、人物已 merge、cluster 已不再 pending。

`resolution_action` 必须是显式动作审计字段，不能留给调用方自由填值。最小动作映射固定为：

- `new_person`：
  - `create-person` -> `create_person`
  - `assign-person` -> `assign_person`
  - `ignore` -> `ignore`
- `low_confidence_assignment`：
  - `confirm-person` -> `confirm_person`
  - `reject-to-unassigned` -> `reject_to_unassigned`
  - `ignore` -> `ignore`
- `possible_merge`：
  - `merge-into-primary` -> `merge_into_primary`
  - `merge-into-secondary` -> `merge_into_secondary`
  - `keep-separate` -> `keep_separate`
  - `ignore` -> `ignore`

状态与动作一致性约束如下：

- `status IN ('resolved', 'ignored')` 时，`resolution_action` 必须非空。
- `status = 'superseded'` 时，`resolution_action` 可空。
- 同一 review 不允许出现“状态已关闭但动作缺失”的记录。

唯一性约束如下：

- 同一 `cluster_id` 最多只有一条 `status = open` 的 `new_person` review。
- 同一 `face_observation_id` 最多只有一条 `status = open` 的 `low_confidence_assignment` review。
- 对同一无序人物对 `(min(person_a, person_b), max(person_a, person_b))`，最多只有一条 `status = open` 的 `possible_merge` review。

上述唯一性必须由数据库唯一索引硬约束，不能只靠应用层“先查后插”。最小 DDL 约束为：

```sql
CREATE UNIQUE INDEX uq_review_open_new_person_cluster
ON review_item(cluster_id)
WHERE review_type = 'new_person' AND status = 'open';

CREATE UNIQUE INDEX uq_review_open_low_conf_observation
ON review_item(face_observation_id)
WHERE review_type = 'low_confidence_assignment' AND status = 'open';

CREATE UNIQUE INDEX uq_review_open_possible_merge_pair
ON review_item(pair_person_low_id, pair_person_high_id)
WHERE review_type = 'possible_merge' AND status = 'open';
```

除唯一索引外，还必须补齐类型约束，避免 `NULL` 漏洞与跨类型脏数据。最小 CHECK 契约为：

```sql
CHECK (
  review_type != 'possible_merge'
  OR (
    pair_person_low_id IS NOT NULL
    AND pair_person_high_id IS NOT NULL
    AND pair_person_low_id < pair_person_high_id
  )
);

CHECK (
  review_type = 'possible_merge'
  OR (pair_person_low_id IS NULL AND pair_person_high_id IS NULL)
);

CHECK (review_type != 'new_person' OR cluster_id IS NOT NULL);
CHECK (review_type != 'low_confidence_assignment' OR face_observation_id IS NOT NULL);
```

冲突策略固定为：

- 新建 review 时若触发唯一约束，必须改为“读取并复用既有 open review”，且允许按最新证据刷新其 `payload_json / priority / runtime_profile_id / evidence_fingerprint`。
- 不允许因为并发冲突抛错后让调用方重试并产生重复 open review。

## 并发、幂等与 ANN 发布事务契约

phase2 对 scan assignment、review 动作、people 动作的写路径统一要求如下：

1. 所有身份写操作必须在 `BEGIN IMMEDIATE` 控制的单写序列中执行；允许为 ANN 发布采用事务 A/B 分段提交，但业务真相状态迁移必须保持可审计且可恢复。
2. 所有状态迁移必须使用 CAS 条件更新，至少校验“期望旧状态 + 目标对象 active 条件”；CAS 未命中时只能返回幂等成功或显式并发冲突，不能盲写覆盖。
3. `new_person`/`low_confidence_assignment`/`possible_merge` 动作都必须幂等；重复请求不得重复建人、重复迁移 assignment、重复写 trusted sample。
4. 写路径必须在同一单写锁窗口内完成 DB 真相写入与 ANN 发布，但必须承认“SQLite 事务”和“文件替换”不在同一物理原子域。ANN 发布应使用 `prepare -> switch -> epoch commit` 双阶段协议：
   - 事务 A：写入业务真相与 `identity_artifact_state.ann_publish_state='preparing'`、`ann_pending_epoch`、`ann_pending_manifest_json`；
   - 文件阶段：写临时 ANN 文件并 `os.replace` 切换 live 文件；
   - 事务 B：CAS 校验 `ann_publish_state='preparing'` 后提交 `ann_epoch=ann_pending_epoch`，并把状态切回 `idle`。
   - 任一步失败都必须写 `ann_publish_state='failed'` 与 `ann_last_error`，禁止假阳性推进 epoch。
5. ANN 读路径必须校验 `ann_epoch`；若 epoch 变化，必须重新加载索引，避免扫描与人工动作并发时读取陈旧向量。
6. 扫描 assignment 子阶段与 review/people 动作并发时，后到事务必须基于最新提交态重新决策，不允许复用旧快照结果直接覆盖。

### `identity_artifact_state` 最小契约（ANN）

phase2 需要显式维护 ANN 发布状态，最小字段集合建议为：

- `ann_epoch`
- `ann_publish_state`（`idle / preparing / failed`）
- `ann_pending_epoch`
- `ann_active_manifest_json`
- `ann_pending_manifest_json`
- `ann_last_error`
- `updated_at`

`ann_active_manifest_json / ann_pending_manifest_json` 的最小结构固定为：

- `epoch`
- `model_key`
- `embedding_dimension`
- `person_count`
- `index_file_path`
- `index_sha256`
- `built_at`
- `builder_version`

恢复逻辑中的“一致性校验”至少比较：`epoch`、`model_key`、`embedding_dimension`、`index_sha256`。

进程启动与定时巡检必须执行恢复逻辑：

- 若发现 `ann_publish_state='preparing'`，必须校验 live ANN 与 `ann_pending_manifest_json` 是否一致：
  - 一致：补做事务 B，推进 `ann_epoch` 并回到 `idle`；
  - 不一致：标记 `failed`，拒绝 assignment 写路径，等待显式修复。
- 若发现 `ann_publish_state='failed'`，assignment/review/people 写路径必须显式报错，不得“静默继续写 DB 但跳过 ANN”。

## 旧字段与旧语义收敛

phase2 对现有旧语义的态度是“降级为遗留”，而不是“继续作为真相”。

必须明确：

- `identity_threshold_profile` 中 assignment / trusted / possible_merge 相关字段不再被 phase2 查询。
- `person.origin_cluster_id` 不再承担任何运行时意义，只允许作为过渡迁移遗留列，最终应删除。
- `review_item.review_type = possible_split` 必须整体退场；历史数据迁移为 `superseded` 或直接清理，不能再出现在新页面与新接口中。
- `ignore review = dismissed` 的旧行为必须删除。

## 日常扫描身份流水线

## 入口前置条件

扫描进入 assignment 子阶段前，必须满足以下条件：

- 当前库存在且只存在一个 active `identity_runtime_profile`。
- 当前库存在且只存在一个 bootstrap owner run。
- 当前 live prototype / ANN 已可读。

如果以上条件不满足：

- metadata / faces / embeddings 可以继续执行；
- assignment 子阶段必须失败并写出显式错误；
- 不允许在缺失 runtime policy 的情况下悄悄退回旧阈值常量。

## Observation 级决策顺序

对每个新增 observation，日常流水线固定按以下顺序决策：

1. 复用 owner run 对应的 observation profile 规则，计算该 observation 的质量分、分池信息和必要去重诊断。
2. 若 observation 被判为 `excluded`，直接保留为未归属，不进入 direct auto，也不进入增量 cluster。
3. 若 observation 质量不足 direct auto 门，但达到 review 资格线，则允许进入 `low_confidence_assignment` 评估。
4. 只有达到 direct auto 条件的 observation，才进入 ANN 召回与精排。
5. direct auto 失败但 observation 质量达到 cluster 发现门时，进入 incremental candidate 池。

这里有一条硬约束：

- phase2 不允许 scan 子阶段重新发明一套独立 quality gate。
- 质量资格必须复用 owner run 绑定的 observation profile 和 active runtime profile。

## Direct Auto Assignment

direct auto assignment 的判定必须同时满足以下条件：

- observation `quality_score >= assignment_auto_min_quality`
- top1 候选存在
- `top1_distance <= assignment_auto_max_distance`
- `top2_distance - top1_distance >= assignment_auto_min_margin`
- 照片级冲突检查通过
- 目标人物不是 `merged`、不是 `ignored`，且 observation 没有被该人物显式 exclusion

具体流程固定为：

1. 从 live ANN 召回 `assignment_recall_top_k` 个候选人物。
2. 用目标人物当前 active prototype 做精排，得到统一口径的 top1 / top2。
3. 执行照片级冲突检查。
4. 满足 direct auto 门时写入或更新 `auto` assignment。

照片级冲突检查定义固定为：

- 同一 `photo_asset_id` 下，若已存在 active assignment 且目标人物与当前 top1 不一致，则记为冲突。
- 当 `assignment_require_photo_conflict_free = 1` 时，存在冲突即禁止 direct auto。
- 冲突结果必须落入诊断字段（至少包括冲突 observation/person 列表摘要）。

对已有 assignment 的处理规则如下：

- 若 observation 已有 active `manual` / `merge` / `bootstrap` / `incremental` assignment，扫描阶段不得自动覆盖。
- 若 observation 已有 active `auto` assignment 且未锁定，允许在同一 observation 被重新处理时按新结果替换。
- 所有 auto 写入都必须带上 `runtime_profile_id`、显式 `diagnostic_json` 和当前 live `model_key`。

### Margin 统一口径

phase2 所有 margin 判定都必须统一“second candidate 缺失”语义，避免实现分叉：

- direct auto / low confidence 判定中，若不存在 `top2`，则 `margin` 视为 `+inf`，并在 `diagnostic_json` 中写 `second_candidate_missing = true`。
- `manual_confirm` 严格门中，若不存在 second person，`distance_to_second_person - distance_to_target_centroid` 视为 `+inf`，并写 `second_person_missing = true`。
- `possible_merge` 判定中，若某一侧不存在“第二近人物”，该侧 margin 视为 `+inf`，并写 `second_neighbor_missing = true`。

### 诊断字段标准化

phase2 所有 `diagnostic_json/payload_json` 需遵循统一最小字段集，避免调试口径漂移。至少包含：

- `runtime_profile_id`
- `model_key`
- `evaluated_at`
- `decision_path`（如 `direct_auto` / `low_confidence` / `manual_confirm` / `possible_merge`）
- `decision_result`

其中：

- assignment/trusted sample 的 `diagnostic_json` 可以追加对象级细节，但不得缺少上述公共字段。
- review 的 `payload_json` 必须保留生成时诊断快照，不得在动作执行后覆盖掉用于审计的关键字段。

## `low_confidence_assignment` 生成规则

以下 observation 可以生成 `low_confidence_assignment`：

- top1 存在，但没有通过 direct auto 门；
- 仍然落在 `assignment_review_max_distance` 之内；
- observation 没有直接进入 incremental auto materialize；
- 当前不存在 open 的同 observation `low_confidence_assignment` review。

这类 review 的单位固定是单条 observation，不允许前端 regroup。

`payload_json` 至少要包含：

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

其中 `top2_person_id` / `top2_distance` 在 second candidate 缺失时允许为 `null`，但必须与 `second_candidate_missing = true` 同时出现。

## Incremental Candidate 池

会进入 incremental new-person discovery 的 observation 必须同时满足：

- 当前没有 active assignment
- 当前没有 open `low_confidence_assignment`
- quality 达到 owner observation profile 的 `core_discovery` 门
- 当前没有处于 `review_pending` 的 open cluster 绑定到它

这类 observation 不是立即生成 review，而是先进入 incremental candidate 池。

边界处理规则：

- 若本轮 candidate 池为空，则本轮不创建 incremental snapshot/run，并写一条可审计的 no-op 事件。
- phase2 不额外定义“最小池大小门”；即使池很小也按标准流程成团，由 phase1 existence gate/materialize gate 决定 `discarded/review_pending/materialized`。

## Incremental Observation Snapshot

每次扫描会话收口时，系统必须对当前 incremental candidate 池创建一份新的：

- `identity_observation_snapshot(snapshot_kind = incremental)`

其 observation 集合固定为“本轮扫描结束时仍满足 incremental candidate 条件的 observation 子集”。

这份 snapshot 依然必须落库、可追溯、不可变。

## Incremental Cluster Run

在 incremental snapshot 上，系统创建：

- `identity_cluster_run(run_kind = incremental)`

其参数约束如下：

- observation profile 固定复用当前 owner run 绑定的 observation profile
- cluster profile 固定复用当前 owner run 绑定的 cluster profile
- `base_owner_run_id` 固定指向当前 bootstrap owner run
- incremental run 永远不允许成为 review target，也不允许成为 materialization owner

## Incremental Run 的处置结果

incremental run 在“run 收口时刻”写入的初始结果只允许三种：

- `materialized`
- `review_pending`
- `discarded`

其中：

- `materialized`：系统可直接自动长出新的匿名人物，并在成功路径结束时写 `publish_state = published`。
- `review_pending`：cluster 成立，但不允许自动长人，转正式 `new_person` review。
- `discarded`：cluster 本身不存在或稳定性不足，只保留审计。

`materialized` vs `review_pending` 的判定门固定复用 phase1 同一套 gate，不在 phase2 新发明增量专用 gate：

- incremental run 先按 phase1 规则计算 `final gate metrics`，并执行 existence gate；
- existence gate 未通过 -> `discarded`；
- existence gate 通过后，继续执行 phase1 materialize gate 与 trusted seed gate；
- materialize gate + trusted seed gate 都通过 -> `materialized`；
- 否则 -> `review_pending`。

后续只有 `review_pending` 可以继续被人工动作转为：

- `materialized`
- `adopted`
- `ignored`

automatic `materialized` 仍然必须复用 phase1 的安全原则：

- 只要是系统自动长人，就不得出现“assignment 已写入，但 trusted sample / prototype / ANN 半失败”的状态。
- incremental auto materialize 失败时，cluster 必须回退为 `review_pending`，而不是留下半成品人物。

## Incremental Auto Materialize

incremental auto materialize 成功时，系统必须：

- 创建匿名 `person`
- 写入 `assignment_source = incremental` 的 active assignments
- 从 cluster seed 选择中写入 `incremental_seed` trusted samples
- 重建该人物 prototype
- 把新 prototype 加入 live ANN
- 把 `identity_cluster_resolution` 写成 `resolution_state = materialized`、`publish_state = published`、`published_at = now()`
- 写入 `person_cluster_origin(origin_kind = incremental_materialize)`

这里生成的是 phase2 增量人物，不会改动 bootstrap owner run。

## `new_person` Review

所有 `identity_cluster_resolution.resolution_state = review_pending` 的 cluster，都必须对应一条正式 `new_person` review。

该 review 的单位固定是 cluster，不是 observation。

`payload_json` 至少包含：

- `cluster_id`
- `source_run_id`
- `cluster_stage`
- `member_summary`
- `anchor_core_count`
- `core_count`
- `boundary_count`
- `distinct_photo_count`
- `compactness_p90`
- `separation_gap`
- `boundary_ratio`
- `trusted_seed_candidate_count`
- `trusted_seed_reject_distribution`
- 代表 observation 与可视化预览清单

WebUI 直接展示这条持久化 review，不再做前端 regroup。

这里的“retained 成员”沿用 phase1 定义，固定指：

- `identity_cluster_member.decision_status = retained`
- 且属于该 `cluster_id` 的 active final 成员集合。

## Review 系统

## 总原则

phase2 review 系统只有一个原则：

- 凡是会改变人物、assignment、trusted sample、prototype、ANN 或 cluster 处置结果的动作，都必须是显式目标动作。

因此 phase2 不再保留以下公共写路径：

- 通用 `resolve review`
- 通用 `dismiss review`

保留的只有：

- 显式业务动作
- `ignore`

## `new_person`

`new_person` review 只允许三种动作：

- `create-person`
- `assign-person`
- `ignore`

### `create-person`

用户选择“新建人物”时，系统执行：

1. 新建 active `person`
2. 将该 cluster 的 retained 成员写成 `manual + locked = 1` assignment
3. 用 cluster 内已经计算好的 seed 选择结果直接写入 `review_seed` trusted samples
4. 基于这些 `review_seed` 构建 prototype 与 ANN
5. 把 `identity_cluster_resolution` 写成：
   - `resolution_state = materialized`
   - `publish_state = published`
   - `published_at = now()`
   - `resolution_reason = review_created_person`
   - `person_id = new_person_id`
6. 写入 `person_cluster_origin(origin_kind = review_materialize)`
7. 把 review 自身写成 `resolved`
8. `review_item.resolution_action = create_person`

这里的关键区别是：

- `create-person` 不走“已有人物 centroid 距离 gate”；
- 它的 trusted 基础来自 cluster 证据本身，而不是已有 person 的 centroid。
- 若用户没有显式提供名字，系统允许先创建匿名人物，后续再通过 `rename` 收口。
- 任一步骤失败都不得遗留“published 成功但 ANN 未发布”的假成功状态；必须按 ANN 发布协议进入可恢复失败态或完成回滚。

### `assign-person`

用户选择“归入现有人物”时，请求必须显式带 `target_person_id`。系统执行：

1. retained 成员全部写成 `manual + locked = 1` assignment 到目标人物
2. 对 cluster 中标记为 trusted seed candidate 的 observation，逐条执行 `manual_confirm` gate
3. 通过 gate 的 observation 写入 `manual_confirm` trusted sample
4. 对目标人物重建 prototype 与 ANN
5. 把 `identity_cluster_resolution` 写成：
   - `resolution_state = adopted`
   - `publish_state = not_applicable`
   - `resolution_reason = review_adopted_into_person`
   - `person_id = target_person_id`
6. 写入 `person_cluster_origin(origin_kind = review_adopt)`
7. 把 review 写成 `resolved`
8. `review_item.resolution_action = assign_person`

### `ignore`

用户选择忽略时：

- `review_item.status = ignored`
- `review_item.resolution_action = ignore`
- `identity_cluster_resolution.resolution_state = ignored`
- 不写 assignment
- 不写 trusted sample
- cluster 中 observation 不自动重新生成新的 `new_person` review

再次进入候选集的前提只能是：

- owner run / runtime profile 变化
- incremental snapshot 集合变化
- 或后续专门的 review regeneration 逻辑显式重新放行

## `low_confidence_assignment` 动作

`low_confidence_assignment` 只允许三种动作：

- `confirm-person`
- `reject-to-unassigned`
- `ignore`

### `confirm-person`

请求必须显式带：

- `target_person_id`

系统执行：

1. observation 写成 `manual + locked = 1`
2. 写入 `confirmed_at`
3. 保留 review 当时展示给用户的完整 `diagnostic_json`
4. 对该 observation 执行 `manual_confirm` gate
5. 通过 gate 时写入 `manual_confirm` trusted sample
6. 对目标人物重建 prototype 与 ANN
7. review 写成 `resolved`，`resolution_action = confirm_person`

不允许缺少 `target_person_id` 时后端偷偷默认选 top1。

### `reject-to-unassigned`

系统执行：

- observation 不创建 assignment
- `review_item.status = resolved`
- `review_item.resolution_action = reject_to_unassigned`
- 若 observation 质量达到 incremental candidate 门，则回到未归属候选池，等待下一次 incremental cluster run
- 不允许因为一次 reject 就立即生成新的 observation-backed `new_person`

### `ignore`

系统执行：

- `review_item.status = ignored`
- `review_item.resolution_action = ignore`
- observation 当前状态不改变
- 之后只有在候选结果发生显式变化时，才允许生成新的 review

## `possible_merge`

`possible_merge` 只允许四种动作：

- `merge-into-primary`
- `merge-into-secondary`
- `keep-separate`
- `ignore`

### 生成条件

只有以下人物对允许产生 `possible_merge`：

- 两边都是 active 且非 ignored
- 两边都至少有 `possible_merge_min_trusted_sample_count` 条 active trusted sample
- 两边都有 active prototype
- pairwise prototype 距离满足 `possible_merge_max_distance`
- 与各自第二相近人物的分离差满足 `possible_merge_min_margin`

若任一侧不存在“第二相近人物”，该侧分离差按 `+inf` 处理，并要求在诊断字段中显式记录 `second_neighbor_missing = true`。

### merge 动作

用户明确选择保留方向后，系统执行：

1. source person 标记为 `merged`
2. source 的 active assignments 迁移到 target，写 `assignment_source = merge`
3. source 的 active trusted samples 按“失活 source + 为 target 新建记录”重建迁移（保留原 `trust_source`，并写迁移审计字段）
4. source 的 `person_cluster_origin(active=1)` 失活，并为 target 追加 `merge_adopt` origin
5. source 的 prototype 失活
6. 重建 target prototype 与 live ANN
7. 重写 `export_template_person` 中指向 source 的关联：
   - 若模板中尚无 target，则替换为 target
   - 若模板中已存在 target，则删除 source 关联并保留 target
8. 关闭所有引用 source person 的 open review，写成 `superseded`
9. 当前 `possible_merge` review 写成 `resolved`
10. `review_item.resolution_action` 必须按用户选择写为 `merge_into_primary` 或 `merge_into_secondary`

### `keep-separate`

`keep-separate` 的语义不是忽略，而是显式确认“当前不 merge”。

因此它的结果是：

- `review_item.status = resolved`
- `resolution_action = keep_separate`
- 该人物对的“最新决策指纹”更新为当前 `evidence_fingerprint`
- 当前人物层不发生任何 merge 副作用

### `ignore`

`ignore` 的语义是暂时不处理，因此：

- `review_item.status = ignored`
- `review_item.resolution_action = ignore`
- 当前人物层不变
- 当两个人物的 prototype 重新变化后，系统可以重新生成新的 `possible_merge`

## 人物维护与 Trusted Pool

## 人物详情页只保留四类动作

phase2 人物详情页只保留：

- `rename`
- `merge`
- `confirm-assignments`
- `exclude-assignments`

明确删除：

- `split`
- 裸 `lock-assignment`

原因很直接：

- `lock` 只是实现细节，不是产品动作；
- `split` 会把错误归属纠正重新退回为“即时造人”，与 cluster / review 主路径冲突。

## `rename`

rename 的语义需要收紧为：

- 匿名人物改成正式名字时，`confirmed = 1`
- 已有正式名字的人物再次改名，不改动 `confirmed`

phase2 不引入单独的“确认人物名称”动作。

## `confirm-assignments`

人物详情页必须支持单条和批量 `confirm-assignments`。

统一语义为：

1. 目标 observation 写成 `manual + locked = 1`
2. 对每条 observation 独立执行 `manual_confirm` gate
3. 通过 gate 的 observation 写入 `manual_confirm` trusted sample
4. 受影响人物重建 prototype 与 ANN

`manual_confirm` gate 固定使用 active runtime profile，并分两条路径：

- 严格门（目标人物已有 active prototype）：
  - `quality_score >= trusted_min_quality`
  - exact duplicate 拦截
  - burst duplicate 拦截
  - `distance_to_target_centroid <= trusted_centroid_max_distance`
  - `distance_to_second_person - distance_to_target_centroid >= trusted_min_margin`
- 启动门（目标人物当前无 active prototype）：
  - `quality_score >= trusted_min_quality`
  - exact duplicate 拦截
  - burst duplicate 拦截
  - 本次提交后 active trusted sample 数 `>= manual_confirm_bootstrap_min_samples`（默认建议 `2`）
  - 本次提交后 distinct photo 数 `>= manual_confirm_bootstrap_min_photos`（默认建议 `2`）
  - 通过后立即重建 prototype 与 ANN；一旦 prototype 建起，后续自动回到严格门

批量 `confirm-assignments` 的顺序与切门时机固定为：

1. 先对整批 observation 执行基础过滤（质量、exact、burst），得到候选集。
2. 若目标人物无 active prototype，先用“已有 trusted + 本批候选”判断是否可满足启动门：
   - 不满足：本批全部按“仅确认归属未入池”返回，不做 prototype 构建。
   - 满足：按 `quality_score DESC, observation_id ASC` 选最小启动子集入池并立即构建 prototype。
3. prototype 建起后，本批剩余 observation 全部按严格门继续评估入池。

实现不得采用“依赖请求原始顺序”的非确定性行为。

批量动作不是 all-or-nothing：

- 允许一部分 observation 只确认归属但未入池
- UI 必须返回逐条失败原因
- 原因至少包括：
  - `quality_too_low`
  - `exact_duplicate_blocked`
  - `burst_duplicate_blocked`
  - `distance_too_far`
  - `margin_too_small`
  - `bootstrap_sample_count_insufficient`
  - `bootstrap_photo_count_insufficient`

## `exclude-assignments`

排除动作的语义固定为：

1. 目标 assignment 失活
2. 若对应 observation 存在 active trusted sample，则同步失活
3. 对受影响人物重建 prototype 与 ANN
4. observation 回到未归属状态
5. 若其质量仍满足 incremental candidate 门，则重新进入未归属候选池

phase2 明确禁止：

- 在排除动作后立即生成 observation-backed `new_person`
- 在排除动作中直接创建新人物

## Prototype / ANN 与 `rebuild-artifacts`

phase2 的 live artifact 真相收敛为：

- 所有 active person 的 prototype 都由 active trusted sample 重新推导
- live ANN 总是由全部 active prototype 聚合构建

因此：

- phase1 的 run-scoped prepared `ann_bundle` 只保留为 bootstrap 基线与审计产物
- phase2 的 live ANN 不再等价于某一轮 run 的单独 artifact

`python -m hikbox_pictures.cli rebuild-artifacts --workspace <workspace>` 的职责固定为：

- 扫描当前全部 active person
- 用 active trusted sample 重建 prototype
- 用 active prototype 重建 live ANN

它明确不负责：

- 重建 review
- 重建 cluster
- 修改 assignment
- 修改 trusted sample

若某个 person 没有 active trusted sample：

- 该 person 的 active prototype 必须失活
- 该 person 必须从 live ANN 中移除
- 但人物档案与 active assignments 仍然保留

这类人物在 UI 中应明确展示为：

- 有人物档案
- 但当前不参与自动归属

## Review Regeneration

phase2 需要一套正式的 review regeneration 机制，但必须与 full rebuild 分开。

原则如下：

- WebUI 动作只做局部回写与局部 regeneration，不做全库重建。
- 日常扫描结束时，只做本轮受影响 observation / person / cluster 的 scoped regeneration。
- 全库 review 重建必须有显式 CLI 或脚本入口，不能隐藏在普通页面动作里。
- `create-person` / `assign-person` / `confirm-person` / `merge` / `exclude-assignments` 提交成功后，必须立即触发一次 scoped regeneration（同请求链路内或紧随其后的同步任务），不得延迟到“下次 scan 再说”。

regeneration 至少覆盖三类对象：

- `low_confidence_assignment`
- `possible_merge`
- `new_person`

其中：

- `new_person` 的来源永远是 `identity_cluster_resolution(review_pending)`，不是重新扫描 observation review。
- `low_confidence_assignment` 的来源是 direct auto 失败但仍有候选人物的 observation。
- `possible_merge` 的来源是 active trusted prototype 对。

旧 review 失效时，必须写成 `superseded`，而不是物理删除。

regeneration 的再入必须使用“证据指纹”硬约束，防止同一对象重复出队：

- 生成候选时必须先计算 `evidence_fingerprint`，并写入 review。
- 若同一业务对象已存在 open review，则只允许刷新该条 review，不允许再插入新 open review。
- 若同一业务对象最近一次 `resolved/ignored` 的 `evidence_fingerprint` 与当前一致，则禁止重新生成 review。
- 只有当 `evidence_fingerprint` 变化时，才允许重新生成 review。

三类指纹最小组成如下：

- `new_person`：`cluster_id + source_run_id + runtime_profile_id + cluster_member_digest`
- `low_confidence_assignment`：`face_observation_id + runtime_profile_id + candidate_person_set_digest + topk_distance_digest`
- `possible_merge`：`pair_person_low_id + pair_person_high_id + runtime_profile_id + 双方prototype版本 + distance/margin诊断摘要`

## 导出系统收口

phase2 的导出系统继续以 active `person` 为模板主体，但要补齐以下规则：

- `merged` person 不允许再作为模板候选人物展示。
- `ignored` person 默认不出现在模板候选人物里。
- 模板预览与导出执行时，只消费 active person。
- merge 动作会自动重写 `export_template_person` 关联，避免模板挂在已经 merge 的 source person 上。
- 预览页必须区分：
  - 人物存在但无 active assignment
  - 人物有 assignment 但无 prototype
  - 人物已 merged / ignored
- 对“active 但无 assignment/无 prototype”的空壳人物，模板列表默认可见但应标注风险（如“当前可能导出为空”），是否选择由用户决定，不做自动隐藏。

phase2 不自动恢复 phase1 清空前的旧模板；导出模板仍以重建后的人物库为准重新建立。

## WebUI 收口

## 人物列表

正式人物列表至少展示：

- `display_name`
- `confirmed`
- `status`
- active assignment 数
- active trusted sample 数
- prototype / ANN 状态
- pending review 数

匿名人物允许正常展示，但必须能直接 rename。

## 人物详情页

正式人物详情页至少展示四组信息：

- 人物基础信息
- active trusted sample
- active assignment
- cluster origin 与 pending review 摘要

assignment 区必须清楚区分：

- 只是 active assignment
- 已经进入 trusted pool
- 已确认但未入池

## Review 队列

正式 review 页只保留三个队列：

- `new_person`
- `low_confidence_assignment`
- `possible_merge`

必须满足：

- `new_person` 直接展示 cluster 证据，不再 regroup
- `low_confidence_assignment` 显式要求选择目标人物
- `possible_merge` 显式要求选择保留方向
- `ignore` 独立成真实状态，不再伪装成 `dismissed`

## `/identity-tuning`

phase1 的 `/identity-tuning` 保持只读，不承担 phase2 的正式 review 或人物维护动作。

phase2 的正式人物系统页面与 `/identity-tuning` 之间的边界固定为：

- `/identity-tuning` 看 run / cluster 证据
- 正式页面看 live person / review / export 状态

## API 与 CLI 收口

## Review API

phase2 的 review API 需要收敛为显式业务动作。最小接口集合如下：

- `POST /api/reviews/{id}/actions/create-person`
- `POST /api/reviews/{id}/actions/assign-person`
- `POST /api/reviews/{id}/actions/confirm-person`
- `POST /api/reviews/{id}/actions/reject-to-unassigned`
- `POST /api/reviews/{id}/actions/merge-into-primary`
- `POST /api/reviews/{id}/actions/merge-into-secondary`
- `POST /api/reviews/{id}/actions/keep-separate`
- `POST /api/reviews/{id}/actions/ignore`

明确删除：

- `POST /api/reviews/{id}/actions/resolve`
- `POST /api/reviews/{id}/actions/dismiss`

## Runtime Profile API

phase2 必须补 runtime profile 管理端点，最小集合如下：

- `GET /api/runtime-profiles`
- `GET /api/runtime-profiles/{id}`
- `POST /api/runtime-profiles`
- `POST /api/runtime-profiles/{id}/actions/validate`
- `POST /api/runtime-profiles/{id}/actions/activate`

## People API

phase2 的 people API 最小集合如下：

- `POST /api/people/{id}/actions/rename`
- `POST /api/people/{id}/actions/merge`
- `POST /api/people/{id}/actions/confirm-assignments`
- `POST /api/people/{id}/actions/exclude-assignments`

明确删除：

- `POST /api/people/{id}/actions/split`
- `POST /api/people/{id}/actions/lock-assignment`

## CLI

现有 CLI 中：

- `scan`
- `scan status`
- `scan abort`
- `scan new`
- `rebuild-artifacts`
- `serve`
- `export run`

都继续保留。

phase2 需要新增一个显式运维入口：

- `python -m hikbox_pictures.cli reviews regenerate --workspace <workspace> [--person-id ...] [--observation-id ...]`

并新增 runtime profile 管理入口：

- `python -m hikbox_pictures.cli runtime-profile list --workspace <workspace>`
- `python -m hikbox_pictures.cli runtime-profile create --workspace <workspace> --from-json <path>`
- `python -m hikbox_pictures.cli runtime-profile validate --workspace <workspace> --profile-id <id>`
- `python -m hikbox_pictures.cli runtime-profile activate --workspace <workspace> --profile-id <id>`

其中 `reviews regenerate` 的职责固定为：

- 做 scoped 或全库的 review regeneration
- 不改动 cluster 真相
- 不改动 trusted sample 真相
- 不执行 full rebuild

## 一次切断迁移顺序（无双轨）

phase2 迁移策略固定为“一次切断”，不保留短期双轨兼容：

1. 先做 expand migration（同版本内第一步）：新增 `review_item` 新字段、`identity_artifact_state`（含 `ann_epoch` 与发布状态字段）、新索引与兼容期所需列；此时允许旧值临时存在，但不得对外暴露新旧双轨。
2. 紧接着做数据回填（同版本内第二步）：把历史 `possible_split` 全量转 `superseded` 或清理；把 `dismissed` 全量收敛为 `ignored`；补齐 `pair_person_low_id / pair_person_high_id / evidence_fingerprint / resolution_action`；把历史 `resolution_state = unresolved` 收敛到 `review_pending` 或 `discarded`。
3. 再做 contract migration（同版本内第三步）：收紧 CHECK/NOT NULL/枚举约束，彻底移除旧语义取值；显式增加 `run_kind` 与 `is_review_target/is_materialization_owner` 的 DB 级一致性约束；必要时通过表重建保证 schema 中不再接受旧状态。
4. 同版本切换服务层：删除 `resolve/dismiss/split/lock-assignment` 写路径，替换为 phase2 显式动作。
5. 同版本切换 API 与 WebUI：正式路由只保留 phase2 动作，不再暴露任何旧端点或旧按钮。
6. 同版本改造测试与夹具：契约测试、API 测试、WebUI 用例、seed fixture 全部改为 phase2 语义；不允许保留旧语义测试“临时兼容”。
7. 同版本更新 README 与运维脚本：命令、动作、状态机、故障恢复说明统一到 phase2 口径。
8. 完成以上步骤后，删除遗留字段和代码分支；迁移窗口结束后不再接受“回退到旧语义”。

迁移窗口内若任一步骤未完成，整版本不得发布。

## 验证与验收

phase2 至少要满足以下验收动作：

1. 在 owner run 已存在的 workspace 上执行日常 `scan`，新 observation 能完成质量判定、direct auto、`low_confidence_assignment` 入队和 incremental candidate 入池。
2. 扫描收口后，系统能生成 `snapshot_kind = incremental` 的 snapshot 与 `run_kind = incremental` 的 cluster run。
3. incremental run 中的 `review_pending` cluster 会生成稳定的 cluster-backed `new_person` review，WebUI 不再临时 regroup。
4. `low_confidence_assignment` 必须显式选择 `target_person_id`；缺参请求必须失败。
5. `new_person create-person` 后，新人物会得到 `review_seed`、prototype、ANN 和 `person_cluster_origin(review_materialize)`。
6. `new_person assign-person` 后，cluster 会写成 `adopted`，目标人物得到新的 `manual` assignments，且只有通过 gate 的 observation 才进入 `manual_confirm`。
7. 人物详情页批量 `confirm-assignments` 时，允许部分 observation 入池、部分只确认归属，并返回逐条失败原因。
8. `exclude-assignments` 后，相关 trusted sample 会同步失活，prototype / ANN 会立即重建，observation 会回到未归属池。
9. `possible_merge` 明确选择保留方向后，source person 会 merge，assignment / trusted sample / export template 关联会正确迁移。
10. 正式 review 页面不再出现 `possible_split`，正式 people API 不再暴露 `split` 与裸 `lock`。
11. `ignore review` 不再落成 `dismissed`，而是独立状态。
12. `rebuild-artifacts` 只重建 prototype / ANN，不会偷偷改 assignment、review 或 cluster。
13. `new_person create-person` 与 incremental auto materialize 都会把 cluster 写成 `resolution_state = materialized` 且 `publish_state = published`，不会出现无 `publish_state` 的 materialized 记录。
14. 并发触发同一 review 动作不会重复建人或重复写入 trusted sample；ANN 文件不会出现并发覆盖损坏。
15. 对无 active prototype 的人物执行 `confirm-assignments` 时，可以通过启动门恢复 trusted pool；prototype 建起后自动切回严格门。
16. `possible_merge keep-separate` 后，证据指纹不变时不会重复生成同一人物对的 open review。
17. 迁移发布后，`resolve/dismiss/split/lock-assignment/possible_split` 在 API、页面、README、测试中都不再出现。
18. 当库内存在 phase2 live delta 时，`activate-run` 必须被硬拒绝，且返回可审计错误码（如 `phase2_delta_present`）。
19. 人为制造 ANN 发布中断后，重启恢复逻辑必须把 `identity_artifact_state` 收敛到一致状态，不得出现“epoch 已推进但 live ANN 不匹配”或反向状态。
20. 任一 `resolved/ignored` review 都必须带正确 `resolution_action`，不得出现关闭状态但动作缺失的记录。
21. incremental run 的 `materialized/review_pending/discarded` 判定必须与 phase1 existence/materialize/trusted-seed gate 一致，不允许出现第二套增量 gate。
22. 当 incremental candidate 池为空时，本轮必须显式 no-op，且不创建 snapshot/run 脏记录。
23. 同一时刻启动第二个 `scan` 会话必须被拒绝（单运行会话约束生效），不得出现并行 running session。
24. `new_person assign-person` 完成后，对应 cluster 必须写 `publish_state = not_applicable`，不得留空或误写 `published`。
25. runtime profile 的 `create/validate/activate/list` 管理入口必须可用，且激活在 `scan running` 期间必须被拒绝。

## README 收口要求

phase2 完成后，README 必须按“日常运行”和“phase1 诊断入口”分开写。

至少要清楚覆盖：

- 日常扫描后会发生什么：
  - 质量判定
  - direct auto
  - `low_confidence_assignment`
  - incremental cluster run
  - `new_person` review
- 人物详情页的正式动作：
  - rename
  - merge
  - confirm assignments
  - exclude assignments
- review 页的正式动作：
  - create person
  - assign person
  - confirm person
  - reject to unassigned
  - merge direction
  - ignore
- `rebuild-artifacts` 的真实职责
- `reviews regenerate` 的运维职责
- `/identity-tuning` 只属于 phase1 诊断，不是正式 review 页面

## 结论

v3.1 phase2 的核心，不是把当前代码里的旧功能逐项修补，而是把“phase1 cluster 真相”和“日常人物系统”真正接起来。

本阶段完成后，系统必须满足以下判断：

- bootstrap、incremental discovery、review、manual confirm、merge、export 不再各说各话。
- cluster-backed `new_person` 成为正式真相，前端不再拼接 observation review。
- trusted pool、prototype、ANN 与人物归属在任何日常动作后都保持一致。
- 旧的 `possible_split`、`split`、通用 `resolve`、旧 profile 混装语义整体退场。

只有在这一步收口完成之后，HikBox Pictures 才算真正从“可调参 bootstrap 原型”进入“可长期运行的身份系统”。
