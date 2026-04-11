# HikBox Pictures 人物图库与智能导出设计文档

## 目标

将当前基于“参考图模板匹配”的一次性 CLI，升级为一个本地优先的人物图库系统。新系统的核心能力是：

- 对整库照片执行一次 `gallery building`，把照片结构化为可维护的人物库。
- 在后续新增照片时支持增量更新，而不是每次全量重建。
- 提供以“人物库优先”为核心的信息架构，并通过 WebUI 完成人物命名、合并、拆分、忽略和低置信审核。
- 提供“智能导出模板”，支持任意人群组合导出，并在库更新后自动补齐新增命中资产、跳过已经成功导出的资产。
- 导出时继续保留 Live Photo 配对导出能力，以及 `YYYY-MM` 的目录结构。

导出目录语义从 `only-two/group` 调整为 `only/group`：

- `only`：照片里包含模板要求的全部人物，且不存在额外显著人物。
- `group`：照片里包含模板要求的全部人物，但还存在额外显著人物，或存在显著但尚未完成归属的人脸。

## 背景与问题

当前实现已经从 `InsightFace` 迁移到 `DeepFace`，并在“多参考图模板匹配”上做了强化；但它仍然是一次性查询工具，主要路径仍是“给定参考图 -> 扫描整库 -> 即时判定”。

这种结构存在三个根本问题：

- 缺少整库人物组织层，无法沉淀稳定的人物实体，也无法支撑任意人群组合导出。
- 识别与检索仍强依赖运行时匹配，不能把一次建库、多次查询的收益沉淀下来。
- 随着图库规模进入几十万张照片量级，继续依赖运行时全库匹配和重度人工挑图，维护成本与性能成本都会快速上升。

因此，新系统不再以“参考图查询器”为核心，而是转向“本地人物图库 + 增量维护 + 智能导出模板”的产品形态。

## 范围

范围内：

- 本地工作区初始化、整库建库、增量更新。
- 基于 `DeepFace` 的人脸检测与 face embedding 提取。
- 自动聚类、人物建议、人工维护、人物命名。
- 本地 WebUI 与本地 API。
- 智能导出模板、导出预览、导出执行、增量补齐、导出账本。
- `HEIC` 与配对 `MOV` 的导出与补齐。
- `only/group` 分桶与 `YYYY-MM` 目录结构。

范围外：

- 视频内容分析。
- 云端部署、多用户协作、账号系统、远程访问权限控制。
- 自定义识别模型训练或 backbone 微调。
- 首版直接接入上半身 embedding、服饰 embedding、时序图模型等多模态特征。
- 首版支持排除表达式、布尔查询树、`至少命中 N 人` 等复杂导出表达式。
- 兼容保留旧的“`--ref-a-dir/--ref-b-dir` 一次性导出”主流程。

## 已选方案概览

本轮设计已经明确以下决策：

- 产品形态采用“方案 1：纯聚类人物库 + 手工维护”。
- WebUI 第一版就存在，不做会被丢弃的临时审核页。
- 信息架构采用“人物库优先”。
- 人物详情页采用“维护工作台”形态，而不是纯相册浏览页。
- 待审核入口采用“分类型队列”，至少包括：新人物、可合并、可拆分、低置信归属。
- 导出器采用“智能导出模板”，而不是一次性查询导出。
- 持久化采用“当前状态为真相”的长期可维护模型，不使用事件日志回放恢复业务状态。
- 业务真相保存在 `SQLite`；大体积且可重建的衍生物保存在文件系统 artifact 目录。
- 人物库真相与机器聚类结果分层：`auto_cluster` 代表机器建议，`person` 与 `person_face_assignment` 代表用户真相。
- 按几十万张照片量级设计性能路径：`SQLite` 存向量真相，`ANN` 负责召回，精排只在小候选集上执行，导出完全不依赖运行时全库向量匹配。

## 产品工作流

### 1. 初始化与首次建库

1. 用户在本地创建一个 HikBox 工作区，并配置照片库根目录。
2. 系统执行首次全库扫描，登记照片资产与 Live Photo 配对信息。
3. 系统为每张检测到的人脸创建 `face_observation`，并生成对应的 `face_embedding`。
4. 系统基于高置信边和局部聚类规则生成 `auto_cluster`，同时尝试形成初始人物候选。
5. WebUI 以“人物库优先”的首页展示已确认人物、候选人物和待审核队列。

### 2. 日常增量更新

1. 用户触发“扫描新照片”或系统根据配置执行手动增量扫描。
2. 只处理新增或变更的资产，不默认重扫未变更资产。
3. 新 observation 先尝试归属到已有 `person`。
4. 高置信 observation 自动归属；边界 observation 进入待审核队列；明显新的人形成人物候选。
5. 任何已经被人工锁定的人物归属，不会因为后续自动聚类而被静默覆盖。

### 3. 人物维护

用户日常维护路径分为四类：

- 新人物：命名、设封面、确认是否保留为人物。
- 可合并：对两个高度相似的人物执行合并、忽略、永不提示。
- 可拆分：当一个人物内部差异过大时，把部分 observation 拆出为新人物或并入他人。
- 低置信归属：确认单张或一批 observation 是否属于某个人物。

### 4. 智能导出模板

1. 用户在 WebUI 中创建一个导出模板，选择若干人物。
2. 首版模板语义固定为“这些人物必须同时出现”；允许照片中还有其他人物。
3. 用户可设置时间范围、输出根目录、是否导出 `group`、是否导出 Live Photo `MOV` 配对等规则。
4. 执行导出时，系统先展示预览统计，再写入导出结果。
5. 当照片库后续更新后，用户重跑同一模板时，系统自动补齐新增命中资产，并跳过已经成功交付且目标文件仍存在的资产。

## WebUI 信息架构

### 首页：人物库优先

首页采用人物库优先布局，首要任务是管理人物而不是即时检索。页面包含：

- 人物卡片网格：显示人物封面、姓名、照片数、待审核徽标。
- 左侧导航：人物库、待审核、导出模板、最近导出、设置。
- 顶部搜索与过滤：按姓名、是否已确认、是否存在待审核、照片数量范围等筛选。

### 人物详情页：维护工作台

人物详情页不是纯浏览相册，而是一页内完成维护：

- 中央区域：该人物的 observation 样本网格，支持多选与批量操作。
- 右侧检查器：显示当前 observation 的来源照片、人脸质量、相似人物建议、最近归属历史。
- 右侧动作区：重命名、设封面、合并、拆分、移出人物、忽略、锁定。
- 顶部统计：人物名称、封面、样本数、照片数、最近更新时间、低置信项数量。

### 待审核：分类型队列

待审核入口按类型分队列，而不是放进一个统一收件箱：

- 新人物
- 可合并
- 可拆分
- 低置信归属

每类队列使用专门的卡片布局和操作按钮，避免不同任务共享同一种上下文，降低人工维护成本。

### 导出模板

导出模板页面包含：

- 模板列表：模板名称、人物数、输出根目录、上次运行状态、最近新增命中数。
- 模板编辑器：人物选择器、时间范围、`only/group` 选项、Live Photo 配对开关、输出目录设置。
- 预览区域：显示按当前规则命中的 `only/group` 数量和最近新增资产数。
- 执行历史：展示每次 `export_run` 的统计、失败项、补齐数量、跳过数量。

## 运行架构

### 总体结构

系统保持本地优先，不引入远程服务：

- Python 后端负责：扫描、检测、embedding 提取、聚类、人物真相维护、导出执行。
- WebUI 只连接本机 `localhost` 上的服务，不提供远程访问。
- 长耗时任务在后台任务执行器中运行，避免阻塞请求线程。

### CLI 角色调整

现有 CLI 从“一次性找合照”调整为“本地图库系统控制面”：

- `init`：初始化工作区与数据库。
- `serve`：启动本地 API 与 WebUI。
- `scan`：执行首次建库或增量扫描。
- `rebuild-artifacts`：重建 ANN、缩略图、封面等可重建衍生物。
- `export run`：在无界面模式下执行某个导出模板。

CLI 继续存在，但 WebUI 成为主要使用方式。

## 工作区与持久化布局

系统使用独立工作区目录保存数据库、artifact 与导出结果。工作区内布局为：

```text
<workspace>/
  .hikbox/
    library.db
    artifacts/
      thumbs/
      face-crops/
      ann/
    exports/
```

约束如下：

- `library.db` 是唯一业务真相。
- `artifacts/` 只保存可删除、可重建的派生数据。
- `exports/` 保存导出产物；即使目标目录配置为其他位置，系统也会在数据库中记录交付账本。
- 若未来需要审计，可新增 `audit_event` 表，但它不是恢复系统状态的依赖项。

## 数据模型

### `library_source`

记录受管照片库根目录与扫描状态。首版只支持一个主照片源，但表结构保留未来扩展多个来源的余地。

关键字段：

- `id`
- `root_path`
- `last_scan_started_at`
- `last_scan_finished_at`
- `last_successful_scan_marker`
- `active`

### `photo_asset`

一张静态照片对应一条资产记录。

关键字段：

- `id`
- `library_source_id`
- `primary_path`
- `primary_fingerprint`
- `file_size`
- `mtime`
- `capture_datetime`
- `capture_month`
- `width`
- `height`
- `is_heic`
- `live_mov_path`
- `live_mov_fingerprint`
- `processing_status`
- `indexed_at`

约束：

- `primary_fingerprint` 是静态照片的稳定内容指纹，用于检测重命名、迁移和内容变化。
- Live Photo `MOV` 的 fingerprint 独立记录，便于单独补齐与校验。

### `face_observation`

一张照片里的每一张检测到的人脸是一条 observation。

关键字段：

- `id`
- `photo_asset_id`
- `bbox_top`
- `bbox_right`
- `bbox_bottom`
- `bbox_left`
- `face_area_ratio`
- `sharpness_score`
- `pose_score`
- `quality_score`
- `crop_path`
- `detector_key`
- `detector_version`
- `observed_at`
- `active`

### `face_embedding`

embedding 作为业务真相保存在数据库中，但不直接承担高频相似度查询。

关键字段：

- `id`
- `face_observation_id`
- `feature_type`
- `model_key`
- `dimension`
- `vector_blob`
- `normalized`
- `generated_at`

约束：

- 首版 `feature_type` 固定为 `face`。
- schema 预留未来接入 `upper_body` 或其他特征的扩展位。
- `vector_blob` 使用连续 `float32` BLOB 存储，保证写入简单、备份简单、重建 artifact 简单。

### `auto_cluster_batch` / `auto_cluster` / `auto_cluster_member`

记录机器聚类结果及其成员关系。它们是机器建议层，而不是人物真相。

关键字段：

- `auto_cluster_batch.id`、`model_key`、`algorithm_version`、`created_at`
- `auto_cluster.id`、`batch_id`、`confidence`、`representative_observation_id`
- `auto_cluster_member.cluster_id`、`face_observation_id`、`membership_score`

约束：

- 机器聚类结果允许整体重建。
- 人工维护不会直接依赖某一批 `auto_cluster` 的存在。

### `person`

人物是真正面向用户的稳定实体，也是导出模板引用的主键。

关键字段：

- `id`
- `display_name`
- `cover_observation_id`
- `status`
- `notes`
- `confirmed`
- `ignored`
- `merged_into_person_id`
- `created_at`
- `updated_at`

约束：

- `status` 只允许 `active`、`merged`、`ignored`。
- 一旦人物被合并，旧人物不再参与导出模板匹配，但保留历史可追踪性。

### `person_face_assignment`

人物归属真相表，明确说明某个 observation 当前属于哪个人物。

关键字段：

- `id`
- `person_id`
- `face_observation_id`
- `assignment_source`
- `confidence`
- `locked`
- `confirmed_at`
- `active`

约束：

- 同一个 `face_observation` 同一时刻最多只能有一个 `active` assignment。
- `locked=true` 的 assignment 不会被后续自动聚类静默改写。
- `assignment_source` 只允许 `auto`、`manual`、`merge`、`split`。

### `person_prototype`

人物原型是查询加速层使用的代表向量集合，用于把 observation 级全表匹配降为人物级候选召回。

关键字段：

- `id`
- `person_id`
- `prototype_type`
- `source_observation_id`
- `model_key`
- `vector_blob`
- `quality_score`
- `active`
- `updated_at`

约束：

- 每个人物至少维护 `1` 个 `centroid` 原型。
- 每个人物额外维护 `1` 个 `medoid` 与最多 `8` 个 `exemplar` 原型。
- 原型由已确认且高质量的 assignment 派生生成；人物归属变化后，对应 prototype 被标记为脏并异步重建。

### `review_item`

待审核项不是日志，而是当前待处理问题的显式实体。

关键字段：

- `id`
- `review_type`
- `primary_person_id`
- `secondary_person_id`
- `face_observation_id`
- `payload_json`
- `priority`
- `status`
- `created_at`
- `resolved_at`

约束：

- `review_type` 至少包括 `new_person`、`possible_merge`、`possible_split`、`low_confidence_assignment`。
- 被用户标记为“永不再提示”的项会保留记录并转为 `dismissed`，不再反复进入队列。

### `export_template` / `export_template_person`

长期存在的智能导出规则。

关键字段：

- `export_template.id`
- `name`
- `output_root`
- `include_group`
- `export_live_mov`
- `start_datetime`
- `end_datetime`
- `enabled`
- `created_at`
- `updated_at`

- `export_template_person.template_id`
- `person_id`
- `position`

约束：

- 首版模板表达式固定为“选中的所有人物都必须出现”。
- 模板不支持排除条件，也不支持“至少命中 N 人”。

### `export_run`

一次实际执行快照。

关键字段：

- `id`
- `template_id`
- `spec_hash`
- `status`
- `matched_only_count`
- `matched_group_count`
- `exported_count`
- `skipped_count`
- `failed_count`
- `started_at`
- `finished_at`

### `export_delivery`

交付账本，用于实现增量补齐和“已导出资产自动跳过”。

关键字段：

- `id`
- `template_id`
- `spec_hash`
- `photo_asset_id`
- `asset_variant`
- `bucket`
- `target_path`
- `source_fingerprint`
- `status`
- `last_exported_at`
- `last_verified_at`

约束：

- `asset_variant` 首版只允许 `primary` 和 `live_mov`。
- 唯一键为：`(template_id, spec_hash, photo_asset_id, asset_variant)`。
- `source_fingerprint` 必须是该变体对应源文件的指纹，用于检测源文件变化后的重新导出需求。

## 人物发现、聚类与增量更新策略

### 首次建库

首轮建库按以下顺序执行：

1. 扫描照片源，登记 `photo_asset`。
2. 检测人脸并生成 `face_observation` 与 `face_embedding`。
3. 按 observation 质量先做一轮质量筛选，降低模糊、极小、姿态过差样本对聚类的干扰。
4. 为 observation 构建近邻候选图，不做 observation 级全量两两比较。
5. 用严格阈值和 `mutual k-NN` 规则先形成高置信 seed cluster。
6. 只在局部候选簇内做第二阶段扩展，不做全局 HAC。
7. 同一张静态照片中的两张脸默认添加 `cannot-link` 约束，不自动聚为同一人物。

### 增量更新

增量更新不做全库重聚类，而是采用“人物优先归属 + 未知池局部聚类”策略：

1. 只处理新增或发生内容变化的 `photo_asset`。
2. 新 observation 先进入 `person_prototype` 的 ANN 召回路径，取 top-k 候选人物。
3. 仅对候选人物的少量 exemplar 原型做精确距离计算。
4. 超过严格自动归属阈值的 observation 直接写入 `person_face_assignment`。
5. 落在灰区的 observation 进入 `review_item`。
6. 与所有已有 `person` 都不够接近的 observation 进入未归属池，并只在未归属池局部聚类以形成新人物候选。

这套策略的目标是把长期系统的工作重点从“反复全库重聚类”转为“稳定维护既有人物 + 控制未知池规模”。

### 阈值分层

系统使用多层阈值，而不是一个统一距离阈值：

- `auto_assign_threshold`：高置信自动归属阈值，最严格。
- `review_threshold`：进入低置信审核的上界阈值。
- `merge_suggestion_threshold`：人物之间进入可合并队列的阈值。
- `split_suspicion_threshold`：人物内部差异过大时进入可拆分队列的阈值。

这些阈值按 `model_key` 与 `feature_type` 维护，避免把“自动归属”“合并建议”“拆分警告”混成一种语义。

## 性能与规模策略

本系统按几十万张照片量级设计，性能策略如下：

- embedding 真相保存在 `SQLite`，但 `SQLite` 不直接承担高频相似度检索。
- `ANN` 索引保存在 `<workspace>/.hikbox/artifacts/ann/`，作为可重建加速层。
- 默认使用 `hnswlib` 构建本地 HNSW 索引，先在人物原型层召回，再在小候选集做精排。
- 导出模板预览与执行完全基于已确认的人物归属关系，不在运行时重新扫全库 embedding。
- 不允许把“运行时模板导出”实现为 observation 级全库向量扫描。
- 不允许把“增量更新”实现为每次都对全库 observation 做暴力匹配。
- 不允许把“库更新”默认实现为全局 HAC 重跑。

性能上需要优先保证三件事：

- WebUI 浏览人物和待审核队列时不依赖向量全表扫。
- 增量更新时新 observation 的自动归属时间与已有图库规模近似亚线性增长。
- 导出重跑时主要消耗在关系查询与文件 I/O，而不是 embedding 检索。

## 导出模板与交付语义

### 模板命中规则

某张照片命中一个导出模板，当且仅当：

- 模板里选中的每一个 `person`，都在该照片中至少出现一次 `active` assignment。
- 命中 assignment 必须来自不同的 `face_observation`。
- 照片时间满足模板时间过滤条件。

### `only/group` 判定

对命中模板的照片，系统先找出模板所要求人物在该照片中的命中 observation，然后计算：

- `selected_min_area`：所有命中 observation 中面积最小的一张脸的面积。
- `significant_extra_face_threshold = selected_min_area / 4`

若存在以下任一情况，则该照片归入 `group`：

- 存在不属于模板人物的 observation，且面积大于等于 `significant_extra_face_threshold`。
- 存在尚未归属到任何人物的 observation，且面积大于等于 `significant_extra_face_threshold`。
- 存在无法取得面积信息的额外 observation。

只有当所有额外 observation 都小于该阈值时，照片才允许归入 `only`。

### `spec_hash` 语义

同一个导出模板是否视为“同一份导出规则”，由标准化后的 `spec_hash` 决定。`spec_hash` 必须包含：

- 模板关联人物集合
- 时间范围
- 输出根目录
- `include_group`
- `export_live_mov`
- 当前导出规则版本号

模板名称、描述等展示字段不参与 `spec_hash`。

### 自动跳过与补齐

当用户在库更新后重新执行同一个模板，且 `spec_hash` 不变时：

- 若某张命中照片的 `export_delivery` 已存在、状态为成功，且目标文件仍存在，则直接跳过。
- 若账本存在但目标文件丢失，则重新导出并修正账本。
- 若账本存在但 `source_fingerprint` 与当前源文件不一致，则重新导出并更新账本。
- 若新增照片命中模板，则补齐导出并新增账本记录。

### 过期导出

当模板规则发生变化或人物归属调整导致旧结果不再命中时，系统默认：

- 不自动删除历史导出文件。
- 把对应的 `export_delivery` 标记为 `stale`。
- 由 WebUI 提供显式“清理过期导出”操作。

这是为了避免人物维护中的一次误操作直接删除用户已经交付或整理过的文件。

## 失败处理与可恢复性

- 扫描、embedding、聚类、导出都必须是可恢复任务。
- 任何单张照片的人脸解码失败，不应中断整批建库或导出，只记录为失败项。
- ANN、缩略图、人脸裁剪等 artifact 丢失后，系统必须能从 `library.db` 重新生成。
- 数据库升级必须走显式 migration，不允许隐式破坏旧工作区。

## 非目标与后续扩展位

为控制首版复杂度，以下能力明确延后：

- 上半身 embedding、服饰连续性、时间上下文图建模。
- 视频抽帧与视频人物识别。
- 表达式级导出规则，例如“必须包含 A/B，不能包含 C，至少出现 3 人”。
- 多用户协同审核与远程同步。

但 schema 与架构已经为后续扩展留出接口：

- `face_embedding.feature_type` 预留多特征扩展位。
- `review_item.payload_json` 可承载更复杂的候选解释数据。
- `export_template` 与 `spec_hash` 可扩展到更复杂规则版本。

## 结论

HikBox Pictures 的下一阶段不再是“一次性参考图查询器”，而是一个本地人物图库系统。它以 `SQLite` 为业务真相，以 `ANN` 为加速层，以 WebUI 为主要操作界面，以“人物维护”和“智能导出模板”作为长期价值承载。

这套设计的关键不在于单个模型名，而在于把整库人物组织、增量维护、人工纠错、导出记账与性能分层统一进一个稳定系统里。只有这样，人物识别效果、使用体验和长期可维护性才会一起提升，而不是继续停留在“一次匹配脚本不断堆参数”的路径上。
