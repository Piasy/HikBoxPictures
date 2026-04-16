# HikBox Pictures v3 人物重建与高信任样本池设计文档

## 目标

本设计用于把当前人物归属体系从“在旧人物库上继续修补”切换到“保留底层观测，重建人物层”。

本次设计的目标是：

- 保留现有照片资产、metadata、人脸 observation 和 embedding 结果。
- 清空旧的人物、归属、排除、review、导出等身份层与下游结果。
- 让 `quality_score` 成为 observation 级主流程信号，而不是只留在 schema 中。
- 用高质量 observation 上的高信任 cluster 重新长出人物。
- 让人物原型只由高信任正样本池构建，而不是由全部自动归属样本反向污染。
- 让“新人物发现”进入主流程，而不是只停留在 review 页面里的临时聚组展示。

## 非目标

本次不做的事情包括：

- 不重跑 metadata 提取。
- 不重做人脸检测。
- 不重算 embedding，前提是 face model 不变。
- 首版不接入姿态模型，也不依赖 `pose_score` 做主流程决策。
- 不继承旧 `person`、旧 `assignment`、旧人工确认、旧排除、旧 `confidence`。
- 不尝试自动恢复旧导出模板与旧导出账本。

## 背景判断

当前 v2 已经证明“人物库驱动的 observation 归属”这条方向是对的，但实现上存在四个根本问题：

- 自动归属过于依赖 top1 绝对距离，缺少歧义约束。
- observation 自身质量没有进入主流程，低质量样本也会直接参与归属。
- 普通自动归属样本会反向污染人物原型，形成误差自我强化。
- 新人物发现没有真正进入主流程，导致很多 observation 只能停留在 review 辅助层。

结合当前库的实际情况，首版质量门不能只靠“绝对大脸”思路。当前 active observation 约 30914 条，`face_area_ratio` 中位数约为 `0.0037`，90 分位约为 `0.0358`。这说明图库里大量 observation 本来就是中小脸，首版质量分必须使用稳健归一化和分层门控，而不是写死一个过高的面积阈值。

## 已选方案概览

v3 首版采用以下方案：

- 分阶段实施：阶段 1 先交付重建脚本、质量分回填、bootstrap、初始 trusted seed、prototype / ANN 与调参专用 WebUI；阶段 2 再补日常归属、review、`manual_confirm`、导出与正式 WebUI 收口。
- 保留底层：`photo_asset`、`face_observation`、`face_embedding`、扫描会话与源目录相关数据继续保留。
- 清空人物层：`person`、`person_face_assignment`、`person_prototype`、`person_face_exclusion`、`review_item`、`export_template` 及其下游结果全部重建。
- 质量优先：全量回填 `sharpness_score` 与 `quality_score` 由 `scripts/` 下的显式重建脚本触发；日常新增 observation 在扫描阶段继续复用 `src/` 中同一套实现即时计算并写入这两个字段，再进入自动归属和增量新人物发现。
- 高信任 bootstrap：只在高质量 observation 上建立严格 kNN 图和稳定 cluster。
- 自动长人物：高信任 cluster 自动落匿名 `person`，中等信任 cluster 持久化后转成 `new_person` review，供用户选择“新建人物”或“归入现有人物”。
- 原型隔离：prototype 只由 trusted positive pool 生成，普通 auto assignment 不直接进入 prototype。
- review 重建：全量 review queue 只在显式重建脚本执行时按新规则重新生成；WebUI 操作只处理当前 review，不触发全库重建。
- 执行边界：破坏性数据清空放在 `scripts/` 下的一次性脚本中，schema 变更走 migration，不进入主 CLI。
- 导出重置：导出模板和导出账本全部清空，不自动恢复；等人物命名稳定后由用户重新创建。

## 分阶段实施总览

本设计按两个阶段落地，先把“可重建、可验证、可调参”的基础链路立住，再补齐其余身份层工作流。

### 阶段 1：重建脚本、Bootstrap 与初始人物生成

- 目标：在不依赖旧人物库的前提下，稳定产出一批可追溯、可调参的匿名初始人物。
- 范围：数据清空与备份、质量分回填、threshold profile 生成 / 激活、bootstrap cluster 持久化、匿名 `person` materialize、`bootstrap_seed` trusted pool、prototype / ANN 重建、调参专用只读 WebUI。
- 交付物：`scripts/rebuild_identities_v3.py` 的阶段 1 能力、`scripts/evaluate_identity_thresholds.py`、cluster / person 诊断数据、调参验收页。
- 不包含：剩余 observation 日常归属、增量新人物发现写回、正式 review 队列与动作、人物详情页人工确认归属、导出模板恢复。

### 阶段 2：其余身份层与日常工作流收敛

- 目标：在阶段 1 锁定的 profile 和 bootstrap 结果之上，补齐日常扫描、review、trusted gate、导出与正式 WebUI。
- 范围：日常 auto assignment、增量 new person cluster、`manual_confirm` trusted gate、review 重建与显式动作 API、人物详情页动作收口、导出模板与 README 重写。
- 交付物：正式 WebUI、完整 review / assignment / trusted pool 工作流、README 新版。

### 阶段切换条件

- 阶段 1 必须先在副本 workspace 上完成 dry-run、重建、调参 WebUI 验收，并锁定一份可接受的 active `identity_threshold_profile`。
- 阶段 2 以阶段 1 已落库的 `identity_threshold_profile`、`auto_cluster*`、匿名 `person`、`person_trusted_sample` 为基线继续补齐，不再引入临时表或平行数据契约。

## 阶段 1：重建脚本、Bootstrap 与初始人物生成

本阶段的目标不是把全部身份层一次做完，而是先把“清库 -> 质量回填 -> bootstrap -> 匿名人物 -> prototype / ANN -> 调参验收”这条闭环跑通。

## 数据保留与清空

### 保留

以下数据保留，不重做：

- `library_source`
- `scan_session`
- `scan_session_source`
- `photo_asset`
- `face_observation`
- `face_embedding`
- `ops_event`

保留这些数据意味着，只要 embedding 模型不变，本轮不需要重做：

- metadata 提取
- 人脸检测
- embedding 计算

### 清空

以下表和对应业务状态全部清空：

- `auto_cluster_batch`
- `auto_cluster`
- `auto_cluster_member`
- `person`
- `person_face_assignment`
- `person_prototype`
- `person_face_exclusion`
- `review_item`
- `export_template`
- `export_template_person`
- `export_run`
- `export_delivery`

同时清空以下派生物：

- 由 `<workspace>/.hikbox/config.json` 解析出的 `artifacts_dir / "ann"` 下的旧 ANN 索引

同时作废以下历史语义：

- 旧人工确认种子
- 旧自动归属结果
- 旧 `confidence`

### 备份与安全要求

重建前必须先备份旧 DB。

建议备份位置：

- `<workspace>/.hikbox/backups/library-YYYYMMDD-HHMMSS-pre-v3.db`

重建脚本必须支持 `--dry-run`，先给出将要清空的数据量摘要，再执行真正的 destructive 操作。

### 执行边界

- 破坏性数据清空由 `scripts/` 下的显式一次性脚本执行，不放入主 CLI，也不放入 migration。
- migration 只负责 schema 变更和可重复的结构升级，例如新增 `person_trusted_sample`、删除旧 `confidence` 字段、收敛 `review_item.review_type` 枚举。
- 一次性脚本负责备份 DB、输出 dry-run 摘要、清空人物层与导出层数据、清空 ANN artifacts、全量回填质量分并驱动本轮重建。
- 质量分计算、bootstrap、prototype 构建、review 重建等具体逻辑实现应放在 `src/` 服务层，由脚本负责编排调用，而不是把算法直接堆进脚本。

## 数据模型调整

数据模型也按两个阶段落地，但阶段 1 必须直接采用最终契约，不引入只为过渡存在的临时字段或临时表。阶段划分如下：

- 阶段 1 先落：`face_observation`、`person_trusted_sample`、`person.origin_cluster_id`、`identity_threshold_profile`、`person_face_assignment` 的诊断字段、`auto_cluster_batch` / `auto_cluster` / `auto_cluster_member`。
- 阶段 2 补齐：`review_item` 的 cluster-backed 契约、`manual_confirm` 写路径、`possible_merge` 相关阈值与动作、导出与正式 WebUI 依赖的其余读写收口。

### `face_observation`

继续使用现有字段：

- `face_area_ratio`
- `sharpness_score`
- `pose_score`
- `quality_score`

本轮要求：

- 回填 `sharpness_score`，该过程需要读取现有 face crop 或原图重裁后的像素数据，不能只依赖 DB；该字段在 v3 中统一表示“原始清晰度度量值”，例如拉普拉斯方差或等价梯度能量原值，不是 `0~1` 归一化后的子分
- 回填 `quality_score`，其中面积分来自现有 `face_area_ratio`，清晰度子分来自对原始 `sharpness_score` 做 `log1p + 分位点归一化` 后得到的运行时派生值 `sharpness_norm_score`
- 文中后续若出现 `area_score`、`sharpness_norm_score`、`pose_norm_score`，都表示按当前 active profile 派生的 `0~1` 子分；除最终 `quality_score` 外，这些子分首版不单独落库
- `pose_score` 字段保留，但 v3 首版不纳入主流程，也不要求必须回填

### 新增 `person_trusted_sample`

需要新增一张显式的高信任正样本池表，而不是继续把“当前归属”和“可用于定义原型的样本”混在一起。

建议字段如下：

- `id`
- `person_id`
- `face_observation_id`
- `trust_source`
- `trust_score`
- `quality_score_snapshot`
- `threshold_profile_id`
- `source_review_id`
- `source_auto_cluster_id`
- `active`
- `created_at`
- `updated_at`

其中 `trust_source` 首版只支持：

- `bootstrap_seed`
- `manual_confirm`

阶段划分上：

- 阶段 1 先支持 `bootstrap_seed`
- 阶段 2 再支持 `manual_confirm`

v3 首版不做 `auto_promoted`。

原因不是概念洁癖，而是当前库的样本结构决定了首版不能这么做：按 2026-04-16 当前工作区 DB 快照，active assignment 共 `30052` 条，其中 `auto=29981`、`manual+locked=71`。如果首版允许 `auto_promoted`，trusted pool 很快会重新被大规模 auto assignment 淹没，把 prototype 拉回“auto 样本自举 auto 样本”的旧路径。

这张表的语义是：

- 这里的 observation 才有资格参与人物原型构建
- `person_face_assignment` 只表达“当前归属”
- `person_trusted_sample` 表达“可定义这个人长什么样的样本”
- `threshold_profile_id` 用于记录该 trusted sample 当时通过的阈值 profile，保证 trusted gate 可追溯
- `source_review_id` / `source_auto_cluster_id` 用于记录它是从哪条 review、哪个 cluster 进入 trusted pool；纯人物详情页人工确认归属时这两个字段可以为空
- `quality_score_snapshot` 固定记录样本入池当时的 observation 质量分，不因后续 profile 重算回写历史值
- `trust_score` 是 `0~1` 闭区间的显式可信度系数，必须可跨 `trust_source` 直接比较；v3 首版 `bootstrap_seed` 与 `manual_confirm` 都固定写 `1.0`，后续若新增来源或分档，必须同步补 spec，不允许在实现层私自派生

结构约束还需要直接下沉到 schema：

- `active = 1` 的记录中，同一 `face_observation_id` 只能存在一条 trusted sample，建议以部分唯一索引 `uq_person_trusted_sample_active_observation` 固化
- 允许保留 inactive 历史记录，但任何会迁移、失活或改写 trusted sample 的动作，都必须在同一事务中先失活旧记录，再写入新记录并触发 prototype / ANN 重建，避免同一 observation 在切换瞬间同时定义两个人

### `person`

`person` 需要新增一个明确的来源字段：

- `origin_cluster_id`

其语义固定为：

- v3 首版所有建人都必须由 cluster 驱动，只允许两种入口：系统自动 materialize 匿名 person，或用户从 cluster-backed `new_person` review 执行“新建人物”
- 自动 materialize 的匿名 person，`origin_cluster_id` 必须指向对应 `auto_cluster.id`
- 用户从 cluster-backed `new_person` review 执行“新建人物”时，新 person 的 `origin_cluster_id` 也必须指向该 cluster
- 因此 `origin_cluster_id` 应视为人物创建时的必填来源字段，而不是仅供人工建人路径使用
- 后续发生 merge 时，不回写或覆盖历史 `origin_cluster_id`；它只表示“这个 person 最初从哪来”

### 新增 `identity_threshold_profile`

v3 需要新增一张显式的阈值 / 归一化 profile 表，作为全量 rebuild、阈值评估和日常增量扫描共享的单一事实来源。

这张表不使用 JSON 字段做主存储。所有会影响 rebuild、日常扫描、review 和 trusted gate 结果的参数，都必须以独立列落表。

建议字段如下：

- `id`
- `profile_name`
- `profile_version`
- `quality_formula_version`
- `embedding_feature_type`
- `embedding_model_key`
- `embedding_distance_metric`
- `embedding_schema_version`
- `quality_area_weight`
- `quality_sharpness_weight`
- `quality_pose_weight`
- `area_log_p10`
- `area_log_p90`
- `sharpness_log_p10`
- `sharpness_log_p90`
- `pose_score_p10`
- `pose_score_p90`
- `low_quality_threshold`
- `high_quality_threshold`
- `trusted_seed_quality_threshold`
- `bootstrap_edge_accept_threshold`
- `bootstrap_edge_candidate_threshold`
- `bootstrap_margin_threshold`
- `bootstrap_min_cluster_size`
- `bootstrap_min_distinct_photo_count`
- `bootstrap_min_high_quality_count`
- `bootstrap_seed_min_count`
- `bootstrap_seed_max_count`
- `assignment_auto_min_quality`
- `assignment_auto_distance_threshold`
- `assignment_auto_margin_threshold`
- `assignment_review_distance_threshold`
- `assignment_require_photo_conflict_free`
- `trusted_min_quality`
- `trusted_centroid_distance_threshold`
- `trusted_margin_threshold`
- `trusted_block_exact_duplicate`
- `trusted_block_burst_duplicate`
- `burst_time_window_seconds`
- `possible_merge_distance_threshold`
- `possible_merge_margin_threshold`
- `active`
- `created_at`
- `activated_at`

其中：

- `embedding_feature_type`、`embedding_model_key`、`embedding_distance_metric`、`embedding_schema_version` 共同绑定 profile 所属的 embedding 空间；任一项变化都必须新建并重新激活 profile，不能把旧距离阈值直接套到新向量空间上
- `quality_pose_weight`、`pose_score_p10`、`pose_score_p90` 是为未来姿态质量门预留的显式列；v3 首版固定为 `0` 或 `NULL`，但列先建好
- `burst_time_window_seconds` 是 burst 去重窗口的显式列，避免重复样本规则散落在代码常量里
- `possible_merge_distance_threshold`、`possible_merge_margin_threshold` 为 `possible_merge` 判定预留显式列；若首版暂不启用，可先为 `NULL`
- `assignment_require_photo_conflict_free`、`trusted_block_exact_duplicate`、`trusted_block_burst_duplicate` 这类布尔规则，也必须独立成列，而不是塞进 JSON 配置

结构约束进一步固定为：

- `profile_name`、`profile_version` 使用显式文本 / 版本列
- `embedding_feature_type`、`embedding_model_key`、`embedding_distance_metric`、`embedding_schema_version` 必须为 `NOT NULL`
- 布尔字段统一用 `INTEGER(0/1)` 或等价布尔列表达，不使用字符串枚举
- 除 `quality_pose_weight`、`pose_score_p10`、`pose_score_p90`、`possible_merge_distance_threshold`、`possible_merge_margin_threshold` 外，首版其余阈值列都应为 `NOT NULL`
- `assignment_auto_distance_threshold`、`assignment_auto_margin_threshold`、`assignment_review_distance_threshold` 虽然初值允许由评估脚本产出，但在 profile 被激活前必须填满，不允许留空激活
- `bootstrap_min_high_quality_count` 不得小于 `bootstrap_seed_min_count`；profile 激活前必须显式校验该关系
- 必须有部分唯一索引 `uq_identity_threshold_profile_active`，保证任一时刻最多只有一条 `active=1`
- 首版 active profile 的具体初值统一见下文“当前库阈值初算（2026-04-16）”；本文其他章节只引用字段名，不在多处重复写数字，避免漂移

落地约束为：

- 同一 workspace 任一时刻只能有一个 active profile 供日常扫描使用
- `--threshold-profile <json>` 导入的 candidate JSON 必须同时携带 `embedding_feature_type`、`embedding_model_key`、`embedding_distance_metric`、`embedding_schema_version`；若与当前 workspace 的 `face_embedding` 空间不一致，重建脚本必须拒绝激活并中止
- active profile 的切换必须在单事务内完成“旧 profile 失活 -> 新 profile 激活”，并受 `uq_identity_threshold_profile_active` 保护
- 全量 rebuild 可以从 `--threshold-profile <json>` 导入一份 profile 并激活，也可以基于当前库重新生成一份 profile 后激活
- 阈值评估脚本生成的 candidate JSON 必须与表字段一一对应，键名直接映射到列名，避免评估逻辑和正式执行逻辑各写一套
- 日常增量扫描默认复用当前 active profile，不在每次扫描时重算全库分位点
- 只有显式全量 rebuild 或显式激活新 profile，才允许改变日常扫描使用的归一化 / 阈值配置

### `person_face_assignment`

保留现有表的主体语义，但在 v3 schema 中删除 `confidence` 字段：

- 阶段 1 先用于 bootstrap materialize 结果与调参诊断落库；阶段 2 再补日常 auto / review / `manual_confirm` 写路径
- `assignment_source` 继续表示归属来源，但 v3 schema 中应收敛为 `bootstrap`、`auto`、`manual`、`merge`；旧 `split` 枚举从 schema、API 和 UI 中删除
- 阶段 1 bootstrap materialize 产生的 assignment 必须写成 `assignment_source='bootstrap'`，不得与日常 prototype 驱动的普通 `auto` 混写
- 新增 `diagnostic_json`，用于持久化显式诊断信息
- 新增 `threshold_profile_id`，用于记录当前 assignment 最近一次判定或确认使用的 profile
- 用户在 review 或人物详情页执行人工确认归属时，当前 assignment 应写成 `manual`，并锁定为 `locked=1`
- `confirmed_at` 用于记录这条归属何时被人工确认
- 人工确认归属只表示“这条 observation 当前确实属于这个 person”，不等于自动进入 trusted pool
- 普通 auto assignment 默认不进入 trusted pool
- 自动判定的解释改为显式距离、margin、quality 和照片级冲突明细，不再维护单一综合分

`diagnostic_json` 首版至少要能表达：

- `decision_kind`，例如 `bootstrap_materialize`、`auto_assignment`、`manual_confirm`、`manual_only`
- `auto_cluster_id`
- `model_key`
- `top1_person_id`、`top1_distance`
- `top2_person_id`、`top2_distance`
- `margin`
- `quality_score`
- `photo_conflict`
- `threshold_profile_id`
- trusted gate 的通过 / 拒绝结果及失败原因

约束为：

- 保留并继续依赖 `uq_person_face_assignment_active_observation` 这类部分唯一索引：`active = 1` 的记录中，同一 `face_observation_id` 只能有一条有效归属
- bootstrap materialize 写入时，`assignment_source` 必须为 `bootstrap`，`diagnostic_json.decision_kind` 必须为 `bootstrap_materialize`，并带上 `auto_cluster_id`
- auto assignment 写入时，`diagnostic_json` 和 `threshold_profile_id` 必须同时落盘
- review 驱动的人工确认归属写入 `manual` assignment 时，也必须把 review 当时展示给用户的显式诊断结果带上，而不是只留下 `manual + locked=1`
- 人物详情页和 review 页后续展示解释信息时，应优先读取这些显式诊断字段，而不是再引入旧 `confidence`

### `auto_cluster_batch` / `auto_cluster` / `auto_cluster_member`

v3 仍保留三张 cluster 持久化表：`auto_cluster_batch`、`auto_cluster`、`auto_cluster_member`。

本文后续一律直接写表名，不再使用 cluster 表通配写法。

这三张表从阶段 1 就必须落地，因为 bootstrap 调参和验收页需要直接读取持久化 cluster 结果；阶段 2 再在同一结构上承接增量新人物发现和 `new_person` review。

v3 中它们应承担两类用途：

- 记录本次全量 bootstrap 的 cluster 输出，以及后续每次增量新人物发现的 cluster 输出
- 为 `new_person` review 和后续追溯提供证据
- 作为 `new_person` review 的事实来源，WebUI 不再依赖临时聚组来决定 review 单位

落地到字段级别，首版至少补充以下契约：

- `auto_cluster_batch` 至少补 `batch_type`、`threshold_profile_id`、`scan_session_id`
- `batch_type` 只允许 `bootstrap` 或 `incremental`
- `threshold_profile_id` 必须指向本批 cluster 运行所使用的 `identity_threshold_profile`
- `scan_session_id` 在全量 rebuild 时可为空，在增量新人物发现时必须指向触发它的扫描会话
- `auto_cluster` 至少补 `cluster_status`、`resolved_person_id`、`diagnostic_json`
- `cluster_status` 首版固定收敛为 `materialized`、`review_pending`、`review_resolved`、`ignored`、`discarded`
- `resolved_person_id` 用于记录这个 cluster 最终落到了哪个 `person`
- `diagnostic_json` 必须包含 `cluster_size`、`distinct_photo_count`、内部距离统计、外部 margin、代表样本、seed 候选数、最终 `selected_seed_count`、`materialize_decision` / `reject_reason` 和 `threshold_profile_id`
- `auto_cluster_member` 至少补 `quality_score_snapshot` 和 `is_seed_candidate`

v3 不再保留 `auto_cluster.confidence` 这类单一综合分。

状态回写必须明确：

- 高信任 cluster 自动 materialize 成 person 后，写 `cluster_status='materialized'`，并回填 `resolved_person_id`
- 中等信任 cluster 在阶段 1 先写 `cluster_status='review_pending'`，表示“候选 cluster 已保留、等待调参验收或阶段 2 正式 review”；满足结构条件但因 `seed_insufficient_after_dedup` 等原因无法安全 materialize 的 cluster 也归入该状态；阶段 2 生成 `new_person` review 后继续复用该状态
- 用户处理 `new_person` review 为“新建人物”或“归入现有人物”后，写 `cluster_status='review_resolved'`，并回填 `resolved_person_id`
- 用户忽略该 `new_person` review 后，写 `cluster_status='ignored'`
- 明显不稳定而被系统丢弃的 cluster 写 `cluster_status='discarded'`

### `review_item`（阶段 2 落地）

这一节属于阶段 2。阶段 1 的阈值重调 WebUI 直接读取 `auto_cluster*`、匿名 `person` 和 trusted seed 结果，不依赖正式 `review_item` 队列。

v3 schema 中，`review_item.review_type` 枚举应收敛为：

- `new_person`
- `possible_merge`
- `low_confidence_assignment`

为支撑 cluster-backed review 和显式目标动作，需要补齐以下契约：

- 新增 `auto_cluster_id`；它对 `new_person` 为必填，对其他 review 为空
- `new_person` 的事实来源必须是 `auto_cluster_id`，`face_observation_id` 若保留，只允许作为预览代表样本，不再作为真实 review 单位
- `low_confidence_assignment` 仍是 observation-backed review，`face_observation_id` 必填
- `low_confidence_assignment.primary_person_id` 用于记录当前建议的 top1 person；`secondary_person_id` 用于记录 top2 person，若不存在可为空
- `possible_merge.primary_person_id` / `secondary_person_id` 固定表示待比较的两个人物
- `payload_json` 必须包含 `threshold_profile_id`、显式诊断明细和 UI 预览所需的 observation/person 证据清单，而不只是一个模糊候选列表

同时必须明确一个 API 约束：

- v3 首版不再把“无目标语义的通用 `resolve`”作为主要写路径；凡是会改变 person / assignment / merge 结果的 review，都必须走显式目标动作接口

## 质量评分

### 定位

`quality_score` 是 observation 质量分，不是身份分。

它回答的是：

- 这张脸适不适合参与自动归属
- 这张脸适不适合参与新人物 bootstrap
- 这张脸适不适合进入 trusted pool

它不回答：

- 这张脸是不是某个人

### 首版组成

由于首版不接姿态模型，`quality_score` 只由两部分组成：

- 面积分
- 清晰度分

首版公式采用：

- `quality_score = quality_area_weight * area_score + quality_sharpness_weight * sharpness_norm_score + quality_pose_weight * pose_norm_score`

其中：

- v3 首版 `quality_pose_weight` 固定为 `0`，因此当前 active profile 下实际只由面积分和清晰度分生效
- 具体权重数值统一见下文“当前库阈值初算（2026-04-16）”

为避免把底层字段和归一化子分混名，本文后续统一约定：

- `face_area_ratio`、`sharpness_score`、`pose_score` 指 observation 上的底层度量字段
- `area_score`、`sharpness_norm_score`、`pose_norm_score` 指按 active profile 派生的 `0~1` 子分

### 面积分

`face_area_ratio` 的分布在当前库中高度偏斜，因此面积分不能直接在线性空间归一化。

首版采用：

- 对 `face_area_ratio` 先取对数空间
- 使用当前 active observation 的稳健分位点做归一化
- 低于低分位点的 observation 接近 0
- 高于高分位点的 observation 接近 1

推荐使用：

- `log_area = log10(max(face_area_ratio, 1e-6))`
- 以 `p10(log_area)` 和 `p90(log_area)` 做线性归一化并裁剪到 `0~1`

### 清晰度分

首版使用 face crop 上的清晰度近似值。

建议使用：

- 拉普拉斯方差或等价的梯度能量指标；其原始值直接写入 `face_observation.sharpness_score`
- 对原始值先取 `log1p`
- 再按 active observation 的稳健分位点做 `0~1` 归一化

### 质量门分层

首版把 observation 按质量分成三层：

- `quality_score < low_quality_threshold`：低质量，不直接 auto，不参与 bootstrap，不进入 trusted pool
- `low_quality_threshold <= quality_score < high_quality_threshold`：中质量，可以参与候选匹配和 review，但不用于 bootstrap seed
- `quality_score >= high_quality_threshold`：高质量，可以进入 bootstrap 候选池和高置信 auto 判定

更严格的 trusted seed 门建议为：

- `quality_score >= trusted_seed_quality_threshold`

### 归一化 profile 与持久化

`quality_score` 的分位点归一化不能只存在于脚本内存里，必须持久化到 `identity_threshold_profile`。

首版落地规则为：

- 全量 rebuild 在回填质量分之前，先基于当前 active observation 计算 `area_log_p10/p90`、`sharpness_log_p10/p90`
- 这些显式统计值写入 `identity_threshold_profile`，并作为本轮 rebuild 的 active profile
- 日常增量扫描对新 observation 即时计算并写入原始 `sharpness_score` 后，必须复用当前 active profile 的 `area_log_p10/p90` 与 `sharpness_log_p10/p90` 计算 `area_score` / `sharpness_norm_score`，再继续计算写入 `quality_score`
- 日常增量扫描不得在每次扫描收口时重算全库分位点，否则同一 observation 在不同日期会得到不可追溯的漂移分数
- 只有显式全量 rebuild 或显式 profile 激活，才允许改变后续 observation 的归一化基线

这也意味着：

- `scripts/evaluate_identity_thresholds.py` 生成的 candidate profile JSON 必须能直接作为 `--threshold-profile <json>` 输入，且键名与 `identity_threshold_profile` 列名一致
- WebUI 不直接改全局阈值；阈值变更只能通过新 profile 的评估、试跑和显式激活进入正式库

## Bootstrap 与初始人物生成

### 总体思路

bootstrap 不依赖旧 `person`，也不继承旧 auto assignment。

它的目标不是“尽可能多地产生 person”，而是“先长出一批足够干净的人物”。

### 步骤

1. 回填全部 active observation 的 `sharpness_score` 和 `quality_score`。
2. 选出 `quality_score >= high_quality_threshold` 的 observation，形成 bootstrap 候选池。
3. 使用 observation embedding 构建严格的 ANN 近邻图。
4. 只保留互为近邻、距离足够近且歧义低的边。
5. 在图上生成稳定 cluster。
6. 只对高信任 cluster 自动创建匿名 `person`。

### 边约束

首版边约束采用保守策略：

- 边必须来自互为近邻的 observation
- 边不能只依赖 ANN 召回，还必须通过精确距离复核
- 边的接受阈值必须单独离线校准，不沿用当前 auto assignment 阈值
- 若 observation 对多个候选都接近，则该边丢弃
- 若同一张照片中的候选关系导致明显冲突，则该边丢弃
- 首版可先基于现有 DB 初算一版阈值：用已锁定或人工确认样本做弱正例，用同照片且已归属到不同 person 的 observation 做硬负例
- 再用一小批人工校准样本做复核和微调

这里不在 spec 中写死具体距离数值，原因是：

- 现有阈值来自 v2 的旧判定逻辑
- v3 需要先给出基于现有 DB 的初值，再结合一小批人工校准正负例重新定 bootstrap 阈值
- bootstrap 边阈值应明显严于日常 auto assignment 阈值

### 当前库阈值初算（2026-04-16）

基于当前工作区 `.hikbox` DB 快照，首版 active `identity_threshold_profile` 统一以本节为准。

除本节外，本文其他章节只引用 profile 字段名，不重复写数字。

统计口径如下：

- 弱正例：`manual + locked` observation 中，同一 person、不同照片的最近同人距离
- 硬负例：同一照片中、已归属到不同 person 的最近异人距离
- 歧义 margin：`manual + locked` observation 的“最近异人距离 - 最近同人距离”
- 面积归一化基线：全部 active observation 的 `log10(max(face_area_ratio, 1e-6))`
- 清晰度归一化基线：全部 active observation 的 `log1p(face_observation.sharpness_score)`；这里的 `sharpness_score` 指原始清晰度度量值；这组值必须在一次性脚本先完成全量 `sharpness_score` 回填后即时计算并写入 profile，不在 spec 中手写漂浮常量

当前库统计结果：

- 弱正例最近同人距离：`p95 = 0.860`，`max = 0.875`
- 硬负例最近异人距离：`p01 = 0.821`，`p05 = 0.979`
- 歧义 margin：`p10 = 0.280`，`p05 = 0.240`
- `face_area_ratio`：`p10 = 0.000702`，`p50 = 0.003730`，`p90 = 0.035839`
- `log10(face_area_ratio)`：`p10 = -3.153629`，`p50 = -2.428310`，`p90 = -1.445643`
- `full_own_centroid_distance`：`p95 = 0.863`
- `full_second_candidate_margin`：`p10 = 0.354`

据此，首版 active profile 先写为：

| 字段 | 当前初值 | 说明 |
| --- | --- | --- |
| `embedding_feature_type` | `face` | 与当前 `face_embedding.feature_type` 对齐。 |
| `embedding_model_key` | `由重建脚本从当前 workspace 的 active face_embedding 解析；若检测到多种有效 model_key 并存则直接报错终止` | 距离阈值只能绑定到单一 embedding 模型。 |
| `embedding_distance_metric` | `cosine` | 与当前 DeepFace / ANN 距离空间对齐。 |
| `embedding_schema_version` | `face_embedding.v1` | 绑定当前向量维度、归一化与序列化契约；后续若 contract 变化必须新建 profile。 |
| `quality_area_weight` | `0.60` | 首版面积分权重。 |
| `quality_sharpness_weight` | `0.40` | 首版清晰度分权重。 |
| `quality_pose_weight` | `0` | 首版不接姿态模型。 |
| `area_log_p10` | `-3.153629` | 来自当前库 active observation。 |
| `area_log_p90` | `-1.445643` | 来自当前库 active observation。 |
| `sharpness_log_p10` | `由重建脚本在全量回填 sharpness_score 后即时计算` | 当前库尚未完成 `sharpness_score` 回填，不手写常量。 |
| `sharpness_log_p90` | `由重建脚本在全量回填 sharpness_score 后即时计算` | 同上。 |
| `pose_score_p10` | `NULL` | 首版不启用姿态门。 |
| `pose_score_p90` | `NULL` | 首版不启用姿态门。 |
| `low_quality_threshold` | `0.45` | 低质量 observation 上限。 |
| `high_quality_threshold` | `0.75` | 高质量 observation 起点，也是 `assignment_auto_min_quality` 的首版对齐值。 |
| `trusted_seed_quality_threshold` | `0.85` | bootstrap seed 的质量门。 |
| `bootstrap_edge_accept_threshold` | `0.80` | 严格边接受阈值。 |
| `bootstrap_edge_candidate_threshold` | `0.88` | 中等信任候选边上限。 |
| `bootstrap_margin_threshold` | `0.28` | 边歧义 margin 下限。 |
| `bootstrap_min_cluster_size` | `3` | 自动 materialize 的最小 cluster 大小。 |
| `bootstrap_min_distinct_photo_count` | `3` | 自动 materialize 的最小不同照片数。 |
| `bootstrap_min_high_quality_count` | `3` | 自动 materialize 的前置高质量 observation 下限；首版与 `bootstrap_seed_min_count` 对齐，最终仍需通过 seed 去重后的下限校验。 |
| `bootstrap_seed_min_count` | `3` | 新人物初始 trusted seed 最少数量。 |
| `bootstrap_seed_max_count` | `8` | 新人物初始 trusted seed 最多数量。 |
| `assignment_auto_min_quality` | `0.75` | 自动归属最低质量门，首版与 `high_quality_threshold` 对齐。 |
| `assignment_auto_distance_threshold` | `0.88` | 首版先与 `trusted_centroid_distance_threshold` 对齐，作为直接 auto 的保守上限。 |
| `assignment_auto_margin_threshold` | `0.35` | 参考 `full_second_candidate_margin p10 = 0.354` 取整。 |
| `assignment_review_distance_threshold` | `0.98` | 参考硬负例最近异人距离 `p05 = 0.979` 取整，超过该值直接留在未归属池。 |
| `assignment_require_photo_conflict_free` | `1` | 首版要求无照片级冲突才允许 auto。 |
| `trusted_min_quality` | `0.85` | 人工确认样本进入 trusted gate 的质量门。 |
| `trusted_centroid_distance_threshold` | `0.88` | 参考 `full_own_centroid_distance p95 = 0.863` 取整。 |
| `trusted_margin_threshold` | `0.35` | 参考 `full_second_candidate_margin p10 = 0.354` 取整。 |
| `trusted_block_exact_duplicate` | `1` | 启用 exact duplicate 去重。 |
| `trusted_block_burst_duplicate` | `1` | 启用 burst 去重。 |
| `burst_time_window_seconds` | `3` | burst 判定时间窗。 |
| `possible_merge_distance_threshold` | `NULL` | 首版暂不依赖自动阈值批量产出 `possible_merge`。 |
| `possible_merge_margin_threshold` | `NULL` | 同上。 |

这组值不是永久常量，而是当前库在 2026-04-16 的首版起点；如果后续重建脚本因调参再次执行，应重新跑一次同口径统计，并生成新的 profile。

### 阈值重调工作流

上面的各种阈值只代表当前库在 2026-04-16 的首版初值，不保证后续效果一定满意。

因此，v3 需要配套一个显式的“阈值复核与重调”工作流，而不是靠手改常量后直接在正式库上重建。

建议新增一个非破坏性评估脚本，放在 `scripts/` 下，例如：

- `source .venv/bin/activate && python scripts/evaluate_identity_thresholds.py --workspace <workspace> --output-dir .tmp/identity-threshold-tuning/<timestamp>/`

该评估脚本应复用 `src/` 中的同一套质量分、bootstrap、cluster 与 review 判定逻辑，但只做分析和报表，不写回正式 DB。

评估脚本至少输出：

- 当前阈值配置摘要
- 候选阈值配置摘要
- bootstrap 预计生成的 auto person 数
- `new_person` / `low_confidence_assignment` 预计数量
- cluster 大小分布、照片数分布、quality 分布
- trusted gate 通过率与拒绝原因分布
- 与上一版阈值相比的差异摘要

正式的阈值重调流程应固定为：

1. 先在正式 workspace 上运行评估脚本，输出候选阈值报告到 `.tmp/identity-threshold-tuning/<timestamp>/`。
2. 根据报告生成一份明确的阈值配置文件，例如 `.tmp/identity-threshold-tuning/<timestamp>/candidate-thresholds.json`。
3. 复制一份临时 workspace 或从 DB 备份恢复一份副本，只在副本上执行带新阈值的全量重建脚本。
4. 在副本 workspace 中用 WebUI 和统计摘要验收效果，重点看：误拆、误并、`new_person` 数量、低置信队列数量、trusted sample 质量。
5. 只有当副本效果满意时，才回到正式 workspace 重新执行全量重建。

重建脚本因此只需要支持显式 profile 输入，例如：

- `--threshold-profile <json>`

该 JSON 的键名必须与 `identity_threshold_profile` 列名一一对应，不再并行维护另一套零散的单参数阈值入口。

原则上：

- 阈值评估是非破坏性的日常调参工具
- 阈值生效仍通过显式全量重建脚本完成
- 不允许在 WebUI 中直接修改全局阈值并立即影响正式库

### Cluster 接受条件

一个 cluster 要自动 materialize 成 `person`，至少满足：

- cluster 大小不少于 `bootstrap_min_cluster_size`
- 来自不少于 `bootstrap_min_distinct_photo_count` 张不同照片
- 内部距离分布足够紧
- 与外部最近候选 cluster 有明显间隔
- cluster 中至少有 `bootstrap_min_high_quality_count` 条 observation 满足 `quality_score >= trusted_seed_quality_threshold`
- 同一波近重复连拍只按 1 张独立照片计数，不因 burst 重复抬高置信；burst 判定口径与下文 “Bootstrap seed” 的照片去重规则一致
- 按下文 “Bootstrap seed” 规则完成 exact / burst 去重后，最终可落库的 `bootstrap_seed` 数不少于 `bootstrap_seed_min_count`

不满足自动 materialize 但仍有一定一致性的 cluster：

- 写入 `auto_cluster_batch`、`auto_cluster`、`auto_cluster_member`
- 转成 `new_person` review

其中必须明确一条硬约束：

- 若 cluster 通过了结构条件，但 exact / burst 去重后 `bootstrap_seed` 数仍不足 `bootstrap_seed_min_count`，则该 cluster 只能降级为 `review_pending`，并在 `auto_cluster.diagnostic_json.reject_reason` 中记录 `seed_insufficient_after_dedup`；不得出现“person 已创建，但 seed / prototype / ANN 起不来”的中间态

明显不稳定或歧义过高的 cluster：

- 只保留原始 observation，等待后续更多样本

### 匿名 `person`

自动 materialize 的人物先创建为匿名 person。

命名策略采用：

- `未命名人物 0001`
- `未命名人物 0002`

后续需要在 WebUI 中补齐人物重命名入口，由用户在人物列表或详情页完成改名。

### Bootstrap seed

即使 cluster 自动变成 `person`，也不能把整个 cluster 全量灌进 prototype。

首版 seed 选择规则：

- seed 挑样不是 materialize 之后的附带步骤，而是 materialize 的最终 gate；只有最终 `selected_seed_count` 落在 `[bootstrap_seed_min_count, bootstrap_seed_max_count]` 区间内的 cluster，才允许自动创建 `person`
- 只从 cluster 中挑 `quality_score >= trusted_seed_quality_threshold` 的 observation
- 优先选 cluster 核心区的样本
- 只在当前 cluster 的 seed 选样阶段做照片去重，不做全库照片级去重
- 同一 `primary_fingerprint` 的样本只保留质量最高的一张
- 若 `capture_datetime` 可用，则同 source 且落在 `burst_time_window_seconds` 窗口内的连拍样本视为同一 burst，只保留质量最高的一张
- 缺少时间信息时，首版只做 exact duplicate 去重，不强行推断近重复
- 每个新 person 初始 trusted seed 数由 `bootstrap_seed_min_count` / `bootstrap_seed_max_count` 控制

这些 seed 写入：

- `person_trusted_sample(trust_source='bootstrap_seed')`

若 cluster 原始结构条件满足，但 exact / burst 去重后最终 seed 数不足 `bootstrap_seed_min_count`：

- 不创建 `person`
- 不创建 prototype / ANN 入口
- cluster 只保留为 `review_pending`
- 调参页和后续阶段 2 `new_person` review 通过 `reject_reason='seed_insufficient_after_dedup'` 追溯该原因

## 人物原型

### 构建原则

prototype 只由 `person_trusted_sample` 生成，不再直接扫描全部 active assignment。

这条原则是 v3 的硬约束。

### 原型生成方式

首版使用加权 centroid：

- 向量输入来自 active `person_trusted_sample`
- 样本权重固定为 `sample_weight = quality_score_snapshot * trust_score`
- `trust_score` 的量纲固定为 `0~1`，具体取值规则见上文 `person_trusted_sample`
- v3 首版 `bootstrap_seed` / `manual_confirm` 都写 `1.0`，因此当前原型权重主要由 `quality_score_snapshot` 决定；后续若引入新来源，必须继续沿用同一量纲
- 权重过低的样本不参与 centroid

建议同时保留：

- 一个 `centroid` 原型用于 ANN 召回
- 一个高质量 `exemplar` 用于 UI 展示和人工复核

### Cover 选择

`person.cover_observation_id` 应优先从 trusted pool 中选择：

- 质量高
- 居于 cluster 核心
- 人脸框稳定

## 重建脚本

重建脚本是阶段 1 的主交付，也是两个阶段共用的唯一全量入口。

### 使用场景

- v2 向 v3 切换时，执行一次全量身份层重建
- 只有在质量分、bootstrap、cluster 接受条件或 schema 发生破坏性调整时，才允许再次手动执行
- 不属于日常扫描、日常 review、日常导出的常规路径
- 如果只是新增照片、处理少量 review 或修正个别人物，不运行该脚本

### 阶段 1 首版范围

- 阶段 1 的目标是支撑 bootstrap 与初始人物生成验收，因此脚本首版先做到：清库、质量回填、bootstrap cluster 持久化、仅对最终 seed 足量的 cluster 自动 materialize 匿名 `person`、`bootstrap_seed` trusted pool、prototype / ANN 重建、调参摘要输出。
- 阶段 1 完成后，脚本必须输出 `materialized` / `review_pending` / `discarded` cluster 摘要，并把调参 WebUI 所需诊断数据落库。
- 阶段 1 不在正式库中生成完整 review queue，也不跑剩余 observation 的全量归属。
- 阶段 2 在同一脚本上继续追加“剩余 observation 的归属与增量新人物发现”“review 重建”等后续阶段，而不是另起第二套全量脚本。

### 建议入口

- `source .venv/bin/activate && python scripts/rebuild_identities_v3.py --workspace <workspace> --dry-run`
- `source .venv/bin/activate && python scripts/rebuild_identities_v3.py --workspace <workspace> --backup-db`

### 建议支持参数

- `--dry-run`
- `--backup-db`
- `--skip-ann-rebuild`
- `--threshold-profile <json>`
- `--skip-review-regeneration`：阶段 2 参数；阶段 1 可暂不实现或固定为空操作

其中：

- 传入 `--threshold-profile <json>` 时，脚本必须先校验其中的 embedding 绑定字段与当前 workspace `face_embedding` 空间一致，再把该 profile 写入 `identity_threshold_profile` 并激活，然后执行后续阶段
- 未传入 `--threshold-profile <json>` 时，脚本也必须明确输出本轮使用或新生成的 active `threshold_profile_id`

### 执行阶段

阶段 1 必须按以下顺序执行并输出摘要：

1. 备份 DB
2. 清空人物层与导出层
3. 回填质量分
4. 执行 bootstrap，并把 cluster 结果完整落入 `auto_cluster_batch`、`auto_cluster`、`auto_cluster_member`
5. 按 Bootstrap seed 规则做 exact / burst 去重挑样；只有最终 seed 足量的高信任 cluster 才生成 `bootstrap_seed` trusted pool 并自动 materialize 匿名 `person`，其余降为 `review_pending`
6. 构建 prototype
7. 重建 ANN
8. 输出调参摘要与诊断快照，供阈值评估脚本和调参 WebUI 复核

阶段 2 在同一脚本上继续追加：

9. 跑剩余 observation 的归属与增量新人物发现
10. 生成 review

### 幂等性要求

该脚本必须是幂等的：

- 同一批底层 observation 不应因重复执行而生成重复 person
- 重复执行应先清空本轮要重建的 derived 数据，再重新生成

## 阶段 2：其余身份层与日常工作流收敛

阶段 2 以前一阶段锁定的 active profile、bootstrap person 和 cluster 证据为基线，继续补齐日常扫描、review、`manual_confirm` 和导出。

## 日常自动归属

### 流程

bootstrap 完成后，日常新增 observation 的处理流程为：

1. 对新增 observation 即时计算并写入 `sharpness_score`，再用当前 active `identity_threshold_profile` 计算并写入 `quality_score`。
2. 低质量 observation 不做直接 auto，转 review 或留在未归属池。
3. 中高质量 observation 才进入 ANN 召回。
4. ANN 召回后用 prototype 做精排。
5. 判定时同时看距离、margin、quality 和照片级冲突。
6. 满足高置信条件时写入 auto assignment。
7. 不满足条件时进入 `low_confidence_assignment` 或未归属池。
8. 高质量但未命中已有人物的 observation 进入增量新人物发现流程。

额外约束：

- 日常扫描不需要新增独立的 durable stage，但新增 observation 在进入 auto assignment 之前，必须先完成 `sharpness_score` / `quality_score` 的计算与落库
- 这一步必须复用全量 rebuild 使用的同一套质量分服务和同一份 active `identity_threshold_profile`
- 若 workspace 没有 active `identity_threshold_profile`，扫描不得继续进入自动归属

### 新人物发现流程

高质量但未命中已有人物的 observation 不直接写成零散临时 review，而进入增量新人物发现流程：

1. 先进入高质量未归属池。
2. 在每轮扫描收口后自动触发增量 cluster 任务；全量 rebuild 时则由显式重建脚本统一执行。
3. 复用 bootstrap 的 cluster 规则，但只在“未归属高质量 observation”子集上运行。
4. 高信任 cluster 直接 materialize 匿名 `person`。
5. 中等信任 cluster 持久化到 `auto_cluster_batch`、`auto_cluster`、`auto_cluster_member`，并生成一个 cluster-backed `new_person` review。
6. 低信任样本继续留在未归属池，等待后续更多样本。

### 判定信号

首版 auto assignment 至少同时看四类信号：

- top1 绝对距离
- top1 与 top2 的间隔
- observation `quality_score`
- 照片级冲突检查

只有“高质量 + 低歧义 + 无冲突”同时满足时，才允许直接 auto。

实现约束补充为：

- `top1` / `top2` 必须来自同一轮精排结果，不允许一个值来自 ANN 召回、另一个值来自另一路回退逻辑
- 照片级冲突检查必须在 assignment 落库前执行，不能先写 auto assignment、再在事后修补
- 低质量 observation 即使 `top1` 距离足够近，也不得越过质量门直接 auto
- 所有 auto / review / keep-unassigned 决策都必须把所用 `threshold_profile_id` 带入落库结果

### 诊断明细

v3 不再保留 `confidence` 字段。

若需要解释自动判定或 cluster 判定，应记录显式明细，而不是重新引入一个单一综合分。首版至少保留：

- top1 距离
- top1 与 top2 的 margin
- observation `quality_score`
- 照片级冲突检查结果
- cluster 的照片数、内部距离统计和外部 margin

显式诊断信息的最终落点固定为：

- auto assignment 的诊断信息写入 `person_face_assignment.diagnostic_json`
- cluster 判定信息写入 `auto_cluster.diagnostic_json`
- review 生成时，把当前用户需要看到的诊断快照复制进 `review_item.payload_json`

这样约束的目的不是重复存储，而是保证三件事：

- 人物详情页可以追溯当前 assignment 当时为什么成立
- review 页可以稳定展示当时的候选解释，而不是每次查询时临时重算
- 阈值评估脚本、重建脚本和日常扫描都能围绕同一套显式诊断字段出报表

## Trusted Pool 的更新

阶段 1 已完成 `bootstrap_seed` 入池规则；本节描述阶段 2 追加的 `manual_confirm` 与 trusted gate 工作流。

### 允许进入 trusted pool 的样本

只有以下两类 observation 可以进入 trusted pool：

- `bootstrap_seed`
- 通过 trusted gate 的人工确认样本（`manual_confirm`）

### `manual_confirm` 的产生

`manual_confirm` 不是“所有 manual assignment”的别名，而是“人工确认后又通过 trusted gate 的样本”。

允许产生 `manual_confirm` 的入口包括：

- `new_person` review 被用户处理为“新建人物”或“归入现有人物”
- `low_confidence_assignment` review 被用户显式确认到某个目标人物
- 人物详情页中，用户对单条或批量 active assignment 执行显式“人工确认归属”动作

统一处理流程为：

1. 先把当前 observation 的归属写成 `manual`，并锁定为 `locked=1`。
2. 再执行 trusted gate 校验，而不是因为人工点过一次就直接入池。
3. 通过校验时，写入 `person_trusted_sample(trust_source='manual_confirm')`。
4. 未通过校验时，保留 `manual` 归属，但不给 trusted pool 入池，并向 UI 返回失败原因。

因此，用户不需要去逐条确认全部 auto assignment；只有少量 review 样本和人物详情页上被主动挑选的样本，才会进入人工确认路径。

### 批量人工确认语义

人物详情页中的“人工确认归属”必须同时支持单条和批量操作。

批量操作规则为：

1. 用户先在人物详情页勾选多条 active assignment。
2. 提交后，后端对每条 assignment 独立执行“写 `manual` + `locked=1` -> 跑 trusted gate -> 视结果决定是否入池”的流程。
3. 批量动作不是 all-or-nothing；允许同一批里一部分 observation 成功入池，另一部分只确认归属但不入池。
4. UI 必须返回批量摘要，至少包括：选中总数、确认归属成功数、trusted 入池成功数、未入池数。
5. 对未入池项，UI 必须提供逐条失败原因，例如质量不足、burst 重复、与 centroid 距离过远、margin 不足。

### trusted gate

`manual_confirm` 的 trusted gate 首版采用保守规则：

- `quality_score >= trusted_min_quality`
- duplicate 规则由 `trusted_block_exact_duplicate`、`trusted_block_burst_duplicate`、`burst_time_window_seconds` 控制
- 若该 person 已存在 trusted centroid，则该 observation 到目标 person 的 trusted centroid 距离需满足 `distance_to_first <= trusted_centroid_distance_threshold`
- 且第二候选 person 的 centroid 必须满足 `margin = distance_to_second - distance_to_first >= trusted_margin_threshold`
- 对应的首版数值统一见上文“当前库阈值初算（2026-04-16）”中的 `trusted_*` 条目

批量场景下，trusted gate 也按 observation 逐条评估，不因同批里某条失败而整体回滚。

若该 person 还没有 trusted centroid：

- 不允许仅凭人物详情页上的单张“人工确认归属”直接起一个新的 trusted sample
- 初始 trusted sample 仍应来自 `bootstrap_seed` 或 `new_person` / `low_confidence_assignment` 这类带上下文证据的人工处理结果

### 首版边界

- 首版不做自动晋升 trusted pool
- 不使用“长期稳定自动样本”或“多次运行后稳定归属”作为准入规则
- 普通 auto assignment 即使高质量，也只表达当前归属，不自动变成定义 prototype 的样本

### 不允许的路径

以下 observation 不允许直接进入 trusted pool：

- 普通 auto assignment
- 低质量 observation
- 只出现一次、尚未进入 bootstrap 或人工确认的 observation

## Review 重建

本节属于阶段 2。阶段 1 的调参页直接读取 `auto_cluster` 和匿名 `person` 结果，不依赖正式 review queue。

人物层重建完成后，再按新规则重新生成 review。

这里的“全量重建”只由 `scripts/` 下的显式脚本触发；WebUI 上的 review 动作只更新相关 review 状态与归属，不触发全库 review 重算。

v3 首版保留的 review 类型仅包括 `new_person`、`possible_merge`、`low_confidence_assignment`。

### review 动作接口收敛

v3 首版需要把 review 动作从“通用 resolve / dismiss”收敛为“显式目标动作”：

- 凡是会改变 person、assignment、trusted sample 或 merge 方向的 review，都必须提交显式目标参数
- 删除无目标语义的通用 `resolve` 写路径，不允许继续承担“猜一个默认目标然后落库”的职责
- WebUI 可以为了交互方便预选默认值，但真正请求体里必须把目标 `person_id` 或 merge 保留方向显式带上
- review 动作完成后，除了更新 `review_item.status`，还必须同步回写 assignment、trusted sample、cluster 或 prototype 相关状态；不能只关掉 review

### `new_person`

来源于：

- 中等信任但未自动 materialize 的持久化 cluster

处理动作为：

- 新建人物
- 归入现有人物
- 忽略

`new_person` 的 review 单位是 cluster，不是前端临时把多条 review 再拼成候选组。

落地约束为：

- `new_person` 请求体必须显式带 `auto_cluster_id`
- “新建人物”动作会创建新的 active person，并把 `person.origin_cluster_id` 回填到该 cluster
- “归入现有人物”动作必须显式带 `target_person_id`
- 两种写路径都会把该 cluster 的相关 observation 写成 `manual + locked=1`
- 是否进入 trusted pool，再走统一的 `manual_confirm` gate
- 动作成功后，把对应 cluster 写成 `review_resolved`，并回填 `resolved_person_id`
- “忽略”只更新 review 和 cluster 状态，不直接把 observation 改写成其他人物

### `low_confidence_assignment`

来源于：

- 中质量 observation 的边界归属
- 高质量但 margin 不足的 observation

处理动作为：

- 确认归入某个人物
- 驳回为未归属
- 忽略

这里必须明确目标人物语义：

- `low_confidence_assignment` 不允许再使用“无参数确认”的旧语义
- 用户执行“确认归入某个人物”时，必须显式提交 `target_person_id`
- UI 可以把 top1 候选预选为默认值，但请求体仍必须显式提交该 `target_person_id`
- 不允许后端在缺少目标参数时偷偷默认吃 top1

动作落地规则为：

- “确认归入某个人物”后，该 observation 写成 `manual + locked=1`
- 写入时同步落 `diagnostic_json`、`threshold_profile_id` 和 `confirmed_at`
- 若通过 trusted gate，才追加为 `manual_confirm` 样本
- “驳回为未归属”时，不创建 assignment；高质量 observation 重新进入高质量未归属池，等待下一轮增量 cluster；中质量 observation 保持未归属
- `low_confidence_assignment` 不允许因为一次“驳回”就立刻生成 observation-backed `new_person` review；新人物发现仍必须回到 cluster 流程
- “忽略”只关闭当前 review，不改变 observation 当前是否归属

### `possible_merge`

来源于：

- 两个 person 的 trusted prototype 距离很近
- 但系统不允许直接自动合并

处理动作为：

- 保留 `primary_person_id`，并把 `secondary_person_id` merge 进去
- 保留 `secondary_person_id`，并把 `primary_person_id` merge 进去
- 保持分离 / 忽略

落地约束为：

- `possible_merge` 不允许使用“通用确认”代替 merge 方向选择
- merge 请求必须显式说明保留哪个 `person_id`
- merge 完成后，source person 变为 merged，active assignment 与 active trusted sample 一并迁移到 target
- 迁移完成后，必须基于 target 的 active trusted sample 重建 prototype 和 ANN；source 的 prototype / ANN 入口必须失效

### 人物详情页动作边界

- 人物详情页只保留两类动作：`人工确认归属`、`排除归属`
- 删除 `split` 相关逻辑、UI、API 和 CLI 入口；v3 首版不再允许从人物详情页把一条 assignment 拆成新人物
- 删除裸 `lock` 逻辑、UI、API 和 CLI 入口；对外唯一保留的主语义是“人工确认归属”
- 人工确认归属动作统一收敛为：写 `manual + locked=1` -> 跑 trusted gate -> 返回是否入池及失败原因

人物详情页中的排除动作语义固定为：

- 排除 assignment 后，目标 observation 变成未归属
- 若该 observation 当前也存在 active `person_trusted_sample`，对应 trusted sample 必须同步失活
- 受影响人物的 prototype 和 ANN 必须基于剩余 active trusted sample 立即重建
- 被排除 observation 若质量足够高，应回到高质量未归属池，等待下一轮增量 cluster
- 不允许再像旧模型那样，在排除动作后立刻生成 observation-backed `new_person` review

### 派生产物重建

- `python -m hikbox_pictures.cli rebuild-artifacts --workspace <workspace>` 继续作为正式入口保留
- 它的唯一职责是：按当前 active trusted sample 重建 prototype，再按当前 active prototype 重建 ANN
- 它不重新评估 trusted gate，不重建 review，不修改 assignment
- 若某个 person 没有 active trusted sample，则应停用其 prototype，并从 ANN 中移除对应入口

## 导出模板与导出账本

导出层的清空动作在阶段 1 重建脚本中已经发生；本节描述的是阶段 2 如何在新人物体系稳定后重新接回正式导出工作流。

导出层属于人物层下游，不继承旧状态。

本轮规则是：

- 清空 `export_template`
- 清空 `export_template_person`
- 清空 `export_run`
- 清空 `export_delivery`

但不自动重建导出模板。

原因是：

- 旧模板绑定的是旧 `person_id`
- v3 中 person 会整体重建
- 自动恢复旧模板只会制造新的错误绑定

正确顺序应为：

1. 重建人物层。
2. 完成匿名人物重命名和必要 review。
3. 由用户重新创建导出模板。

## 横切项：WebUI、验证与 README 收口

以下三节属于横切项，用来集中说明两个阶段在 WebUI、验证与文档收口上的共同要求，避免这些约束散落在脚本、review 或导出章节里各写一遍。

阶段 1 重点是保证“可重建、可调参、可验收”；阶段 2 再把正式交互、完整验证矩阵和 README 全量重写补齐。

## WebUI 影响

本节按阶段拆分 UI，避免阶段 1 为了调参验收被阶段 2 的正式交互阻塞。

### 阶段 1：阈值重调与 bootstrap 验收专用 WebUI

- 新增一个调参专用、只读的 WebUI 入口，直接展示当前 rebuild 产生的 bootstrap 结果，不以正式 review queue 作为主入口
- 页面至少展示当前 active `identity_threshold_profile` 摘要、bootstrap batch 摘要、`materialized` / `review_pending` / `discarded` cluster 数量
- 页面至少展示自动 materialize 的匿名 `person`、其 cover、seed 组成、cluster 诊断明细和代表 observation
- 对未自动 materialize 的 cluster，至少要能看到 cluster 大小、不同照片数、quality 分布、外部 margin 和被拒原因摘要
- 不提供 review resolve、人工确认归属、merge、导出模板维护等正式写操作
- 不允许在 WebUI 中直接修改全局阈值；阈值变更仍通过评估脚本 + 副本 workspace 重建完成

### 阶段 2：正式人物与 review WebUI 收口

- 人物列表允许出现匿名 `person`
- 人物列表与人物详情页需要补齐重命名入口
- 人物详情页要能区分 trusted sample 和普通 assignment
- 人物详情页需要提供单条和批量“人工确认归属”动作，并展示“已确认但未入池”的失败原因；原始裸 `lock` 不再作为主按钮暴露
- review 中的 `new_person` 要以持久化 cluster 为单位展示，并支持“新建人物”与“归入现有人物”
- `low_confidence_assignment` 必须提供显式目标人物选择；UI 可以预选 top1，但提交时必须显式带 `target_person_id`
- `possible_merge` 必须提供明确的保留方向，而不是一个无语义的“确认”按钮
- review queue 移除 `possible_split` 队列
- 导出模板页在 v3 重建后应明确提示“模板已清空，需要按新人物重新创建”

## 验证策略

验证也按两阶段拆分：阶段 1 先验证“重建与调参闭环”，阶段 2 再补齐“日常工作流与正式交互”。

### 阶段 1

#### 数据正确性

需要覆盖：

- 清空范围正确，保留范围不被误删
- 备份 DB 文件可恢复
- migration 只负责 schema 调整，不会在普通初始化流程中触发破坏性清空
- `identity_threshold_profile` 只有一个 active profile，且全量 rebuild / 阈值评估 / 调参 WebUI 共用同一结构；profile 激活时会校验 embedding 绑定字段与当前 `face_embedding` 空间一致
- `person_trusted_sample` 通过 schema 约束保证同一 observation 不能同时成为多个 person 的 active trusted sample
- `person_face_assignment.assignment_source='bootstrap'` 能与日常 `auto` 稳定区分并可追溯
- `auto_cluster*`、匿名 `person`、`person_trusted_sample`、prototype / ANN 结果可按同一批 rebuild 输出追溯
- ANN 能在新 prototype 上重建，并与当前 active trusted sample 对应

#### 算法正确性

需要覆盖：

- `quality_score` 回填
- `face_observation.sharpness_score` 落库的是原始清晰度度量，`sharpness_norm_score` 只作为运行时子分参与 `quality_score`
- 高质量 observation 进入 bootstrap 候选池
- 自动 materialize 的 person 会生成匿名名字、seed 和 prototype，且每个 materialized person 至少拥有 `bootstrap_seed_min_count` 条经 exact / burst 去重后的 active `bootstrap_seed`
- 中等 / 丢弃 cluster 会持久化诊断信息，调参 WebUI 展示结果与落库摘要一致
- 普通 auto assignment 不会污染 prototype

#### 业务正确性

需要覆盖：

- 在副本 workspace 上可完成 `dry-run -> rebuild -> 调参 WebUI 验收` 闭环
- 调参页只展示 bootstrap 与初始人物生成结果，不依赖正式 review queue
- 用户可以基于调参页判断误拆、误并、cluster 稳定性和 seed 质量

### 阶段 2

#### 数据正确性

需要覆盖：

- 日常增量扫描复用 active `identity_threshold_profile`，不会在每次扫描时重算全库分位点
- `review_item`、`person_face_assignment`、`person_trusted_sample`、`auto_cluster*` 之间的来源与状态回写一致
- 导出模板和导出账本会被清空，且不会在全量重建后被错误恢复

#### 算法正确性

需要覆盖：

- 高质量但未命中已有人物的 observation 会进入增量新人物发现流程
- 低质量 observation 不会自动进入 trusted pool
- 人工确认只会把通过 trusted gate 的样本写入 `manual_confirm`
- 批量人工确认按 observation 逐条评估，允许部分入池、部分仅确认归属

#### 业务正确性

需要覆盖：

- review 能按新规则重新生成，且 `new_person` 以持久化 cluster 为单位展示
- `low_confidence_assignment` 必须显式选择目标人物，不存在“无参数确认默认吃 top1”
- `possible_merge` 在 review 中能显式选择 merge 保留方向
- `possible_split` 不再出现在 review queue 中
- 用户可对匿名 person 直接重命名
- 人物详情页可执行单条和批量“人工确认归属”，并正确区分“确认成功但未入池”和“确认且入池”

## README 重写工作项

README 也按两个阶段推进，但最终仍需在阶段 2 完成整篇重写，不能只在阶段 1 留零散补丁。

### 阶段 1：补最小可执行说明

- 环境准备：`source .venv/bin/activate`
- 先看重建影响：`python scripts/rebuild_identities_v3.py --workspace <workspace> --dry-run`
- 执行带备份的全量重建：`python scripts/rebuild_identities_v3.py --workspace <workspace> --backup-db`
- 明确说明阶段 1 脚本会做什么：备份 DB、清空人物层与导出层、回填质量分、bootstrap、重建 prototype / ANN、输出调参摘要
- 调参场景：先跑非破坏性评估：`python scripts/evaluate_identity_thresholds.py --workspace <workspace> --output-dir .tmp/identity-threshold-tuning/<timestamp>/`
- 在副本 workspace 上试跑：`python scripts/rebuild_identities_v3.py --workspace <workspace-copy> --backup-db --threshold-profile <json>`
- 启动服务并进入调参 WebUI：`python -m hikbox_pictures.cli serve --workspace <workspace> --host 0.0.0.0 --port 8000`
- 明确说明阶段 1 只覆盖 bootstrap 与初始人物生成，不包含正式 review / 导出工作流

### 阶段 2：完成 README 全量重写

README 新版应按“场景 -> 命令 -> 预期结果 -> 不该做什么”的方式组织，重点覆盖以下工作流：

- 一次性场景：已有 v2 workspace 首次切到 v3
  - 环境准备：`source .venv/bin/activate`
  - 先看重建影响：`python scripts/rebuild_identities_v3.py --workspace <workspace> --dry-run`
  - 执行带备份的全量重建：`python scripts/rebuild_identities_v3.py --workspace <workspace> --backup-db`
  - 明确说明该脚本会做什么：备份 DB、清空人物层与导出层、回填质量分、bootstrap、重建 prototype / ANN、重建 review
  - 明确说明什么情况下不要运行：日常新增照片、少量 review 处理、单个人物修正

- 调参场景：对 bootstrap / trusted gate 阈值不满意，需要重新评估
  - 先跑非破坏性评估：`python scripts/evaluate_identity_thresholds.py --workspace <workspace> --output-dir .tmp/identity-threshold-tuning/<timestamp>/`
  - 在副本 workspace 上试跑：`python scripts/rebuild_identities_v3.py --workspace <workspace-copy> --backup-db --threshold-profile <json>`
  - 明确说明调参不是直接在正式 workspace 上反复试错，而是“先评估、再副本试跑、最后正式重建”

- 日常场景：启动服务并进入 WebUI
  - 启动：`python -m hikbox_pictures.cli serve --workspace <workspace> --host 0.0.0.0 --port 8000`
  - 说明 WebUI 中的主要日常动作：处理 `new_person` / `low_confidence_assignment` review、人物重命名、人物详情页单条 / 批量人工确认归属、导出模板维护

- 日常场景：扫描新照片并推进增量识别
  - 执行或恢复扫描：`python -m hikbox_pictures.cli scan --workspace <workspace>`
  - 查看扫描状态：`python -m hikbox_pictures.cli scan status --workspace <workspace>`
  - 放弃旧会话并启动新扫描：`python -m hikbox_pictures.cli scan new --workspace <workspace> --abandon-resumable`
  - 中断扫描：`python -m hikbox_pictures.cli scan abort --workspace <workspace>`
  - 说明扫描完成后会发生什么：新增 observation 的质量分计算、归属、增量新人物发现、review 入队

- 日常场景：只重建派生产物，不做身份层重建
  - 使用：`python -m hikbox_pictures.cli rebuild-artifacts --workspace <workspace>`
  - 明确说明它只重建 prototype / ANN 等派生产物，不会清空人物层，不等于 v3 全量重建脚本

- 日常场景：日志与诊断
  - 查看日志：`python -m hikbox_pictures.cli logs tail --workspace <workspace>`
  - 清理日志：`python -m hikbox_pictures.cli logs prune --workspace <workspace> --days <n>`
  - 说明何时优先看日志：重建脚本失败、扫描卡住、ANN 重建异常、review 重建结果异常

- 日常场景：导出
  - 执行导出：`python -m hikbox_pictures.cli export run --workspace <workspace> --template-id <id>`
  - 明确说明 v3 全量重建后旧模板会失效，需要在 WebUI 中按新人物重新创建模板

README 必须额外强调以下约束：

- `--workspace` 只负责定位本地 workspace，`artifacts` / `logs` / `exports` 路径必须从 `<workspace>/.hikbox/config.json` 解析，不再写死旧目录假设
- migration 只负责 schema 变更；破坏性数据清空只允许通过 `scripts/rebuild_identities_v3.py` 这类显式脚本执行
- 日常工作流和一次性重建工作流必须分章写清，避免用户把全量重建脚本当成日常命令

## 结论

按两个阶段推进的目的，不是把需求拆薄，而是先把最容易影响结果质量的基础链路单独锁定。

v3 首版的关键不是把阈值再调一轮，而是把人物系统的基本语义改正：

- observation 质量和 identity 判定必须拆开
- 自动归属和 prototype 更新必须拆开
- 新人物发现必须进入主流程
- 人物原型必须建立在高信任正样本池之上

阶段 1 先把 profile / bootstrap / script / tuning 闭环做稳，阶段 2 再补日常归属、review、导出和正式 WebUI，整体返工成本最低。

只要这四点立住，后续再补 pose、服饰、时序和更复杂的多图一致性，才有意义。
