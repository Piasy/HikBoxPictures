# HikBox Pictures v3.1 快速验证导出工具设计文档

## 目标

本设计的目标不是继续扩张 `src/hikbox_pictures` 的正式产品面，而是在仓库内新增一套面向 `.hikbox` workspace 的快速验证工具，用最短路径验证 v3.1 的两个核心问题：

- 当前 phase1 跑出的自动聚类结果是否足够合理，能否直接作为后续 identity seed 的来源。
- 其他 observation 往这些 seed identity 上做 assign 时，`auto_assign / review / reject` 的分布和证据是否站得住。

本工具必须满足以下要求：

- 直接消费 `.hikbox/.hikbox/library.db` 与现有 artifact，不接扫描主链。
- 输出类似 `export_observation_neighbors.py` 的本地离线证据包，而不是常驻 WebUI。
- 默认只导出单个 `index.html`，所有验证信息在一页内折叠查看。
- 允许为了快速验证临时改变参数和 seed 选择，但不把这些实验结果回写成正式运行时真相。

## 设计前提

本设计建立在以下前提上：

- 当前 workspace 已经具备 phase1 的 `identity_observation_snapshot`、`identity_cluster_run`、`identity_cluster`、`identity_cluster_member`、`identity_cluster_resolution` 等基础数据。
- 当前 `.hikbox` 可能还没有 live `person`、`person_trusted_sample`、`person_prototype`，因此不能把“assign 是否有效”建立在正式人物层之上。
- 当前 phase1 的 `/identity-tuning` 和 `export_observation_neighbors.py` 已经证明“本地 HTML + crop/context 证据文件”的调试形式可用，应优先复用这种模式。
- 用户当前目标是快速验证，不要求主 CLI/API/WebUI 兼容，也不要求设计成长期正式入口。

## 非目标

本设计明确不覆盖以下内容：

- 不新增正式 WebUI、FastAPI 路由或主导航入口。
- 不把快速验证工具并入 `hikbox_pictures.cli` 主命令树。
- 不实现正式的 review queue、人物维护、merge、rename、export 模板等 phase2 产品能力。
- 不把实验结果回写为 `person`、`person_face_assignment`、`person_trusted_sample` 或 `person_prototype`。
- 不新增 `prototype_*` 持久化实验表；本轮验证结果只在导出阶段内存计算，最终落为离线证据包。
- 不重新实现另一套完整 clustering 流程来替代现有 phase1 run。
- 不做多页 HTML 站点；导出结果固定为单页 `index.html`。

## 核心判断

这次快速验证不应该再发明一套旁路 clustering 语义。真正需要验证的是：

1. 当前 phase1 的 `final cluster` 输出能否长成可用 identity seed。
2. 以这些 seed 为基础时，其余 observation 的 assign 行为是否符合预期。

因此本工具不再“重新自动聚类一次”，而是直接把现有 `identity_cluster_run` 作为自动聚类真相输入，再围绕它做临时 identity seed 构建和 assign 验证。

这有两个直接收益：

- 验证对象就是你真正关心的 v3.1 产物，而不是另一套临时算法输出。
- 一旦验证通过，后续 phase2 只需要把这些原型判断沉淀到正式运行时，而不是先解决两套语义之间的对齐问题。

## 总体方案

工具整体采用“独立脚本 + 独立服务模块 + 单页离线证据包”的结构。

### 代码落点

- 新 CLI 脚本：`scripts/export_identity_v3_1_report.py`
- 新实验模块目录：`src/hikbox_experiments/identity_v3_1/`
- 复用现有能力：
  - `hikbox_pictures.workspace` 读取 workspace
  - `hikbox_pictures.services.preview_artifact_service` 生成 crop/context/preview
  - `hikbox_pictures.services.observation_neighbor_export_service` 的导出目录组织和 HTML 书写风格

实验模块内部建议拆成以下职责：

- `query_service.py`
  - 负责从现有 phase1 表中读取 run、cluster、member、snapshot、observation、embedding 数据。
- `assignment_service.py`
  - 负责临时 seed identity 构建、候选召回、assign 分类。
- `export_service.py`
  - 负责组织 manifest、导出图片资产、拼装单页 HTML。

## 输入数据契约

### 基础 run 选择

工具必须支持两种基础 run 选择方式：

- 默认读取当前 `is_review_target = 1` 的 `identity_cluster_run`
- 允许通过 `--base-run-id` 显式指定某个 `succeeded` run

若指定 run 不存在或不是 `succeeded`，脚本必须直接失败。

### 自动聚类输入

自动聚类输入固定来自所选 run 的 `cluster_stage = 'final'` 结果，并只消费以下状态：

- `resolution_state = 'materialized'`
- `resolution_state = 'review_pending'`

其中：

- `materialized` cluster 用于构建临时 seed identity
- `review_pending` cluster 用于验证“未物化但可能有价值”的候选群

`discarded`、`ignored`、`unresolved` cluster 不进入本轮 seed identity 构建。

### assign 候选 observation

默认 assign 候选集合由以下两部分组成：

- 所选 run 中 `review_pending` cluster 的 retained 成员
- 所选 snapshot 中 `pool_kind = 'attachment'` 的 observation

默认排除以下 observation：

- 已经属于 seed identity 的 observation
- 缺少 normalized face embedding 的 observation
- 与 seed identity 维度不一致的 observation

后续可通过 CLI 参数控制只看其中一部分，但默认必须同时覆盖 `review_pending retained` 和 `attachment` 两类来源。

## 临时 seed identity 构建

### seed 来源

每个 `materialized` final cluster 默认生成一个临时 seed identity。

此外，工具必须支持两类临时覆盖：

- `--promote-cluster-ids`
  - 把指定 `review_pending cluster` 临时提升为 seed identity
- `--disable-seed-cluster-ids`
  - 暂时禁用指定 seed cluster，不参与 assign

这些覆盖只影响本次导出，不回写数据库。

### prototype 向量生成

每个临时 seed identity 的 prototype 生成规则如下：

1. 优先使用 `identity_cluster_member.is_selected_trusted_seed = 1` 的成员。
2. 如果该 cluster 没有选中 trusted seed，则回退到所有 `decision_status != 'rejected'` 的 retained 成员。
3. 对所选成员的 normalized embedding 求均值后再归一化，得到临时 prototype。

必须在 manifest 中记录每个 seed identity 的：

- `source_cluster_id`
- `seed_member_count`
- `fallback_used`
- `prototype_dimension`

若某个 cluster 无法产出任何有效 prototype，则该 seed identity 标记为无效，并在页面摘要中列为错误项。

## assign 验证算法

### 候选召回

每个待 assign observation 都要对当前启用的 seed identity 做最近邻召回，至少产出 top-k 候选。

距离度量必须与当前原型实现保持一致：

- 对 normalized embedding 使用 L2 距离
- 该度量与当前 `AnnIndexStore.search()` 保持同口径

### 判定字段

每条 observation 的 assign 结果至少要计算并导出：

- `best_identity_id`
- `best_cluster_id`
- `best_distance`
- `second_best_distance`
- `distance_margin`
- `same_photo_conflict`
- `decision`
- `reason_code`

### 判定分类

assign 结果只分三类：

- `auto_assign`
- `review`
- `reject`

默认判定逻辑如下：

- 当没有任何有效 seed 候选时，直接记为 `reject`
- 当 `best_distance <= auto_max_distance` 且 `distance_margin >= min_margin` 且不存在同图冲突时，记为 `auto_assign`
- 当 `best_distance <= review_max_distance` 但未满足 `auto_assign` 条件时，记为 `review`
- 其他情况记为 `reject`

默认暴露以下 CLI 参数用于调参：

- `--top-k`
- `--auto-max-distance`
- `--review-max-distance`
- `--min-margin`
- `--assign-source`

参数只影响本次导出，不产生持久化 profile。

## 导出物结构

### 输出目录

默认输出目录必须放在：

- `.tmp/v3_1-identity-prototype/<timestamp>/`

允许通过 `--output-root` 显式覆盖，但仍建议指向 `.tmp/` 下的任务子目录。

### 固定文件

每轮导出至少生成：

- `index.html`
- `manifest.json`
- `assets/`

其中：

- `index.html` 是唯一页面入口
- `manifest.json` 是结构化摘要，便于脚本化比较不同参数结果
- `assets/` 保存 crop/context/preview 等图片文件

### 资产组织

导出资产组织方式应尽量贴近 `ObservationNeighborExportService`，避免又发明一套目录结构。

建议按 observation 组织文件：

- `assets/observations/obs-<id>/crop.jpg`
- `assets/observations/obs-<id>/context.jpg`
- `assets/observations/obs-<id>/preview.jpg`

如某些 observation 只需要 crop/context，也允许不生成 preview，但必须在 manifest 中记录缺失项。

## 单页 HTML 结构

`index.html` 固定为单页结构，使用 `<details>` + 卡片布局折叠不同验证区块。

页面至少包含以下区块：

### 1. 顶部摘要

展示：

- workspace 路径
- base run id
- snapshot id
- 导出时间
- 参数摘要
- seed identity 数量
- `auto_assign / review / reject` 总计数
- 错误和警告摘要

### 2. seed identities

展示每个启用 seed identity 的：

- source cluster id
- resolution state
- prototype 成员数
- fallback 是否触发
- representative crop/context
- 若干 seed 成员卡片

### 3. 覆盖项摘要

展示：

- `promoted cluster ids`
- `disabled seed cluster ids`
- 无法生成 prototype 的 cluster

### 4. review_pending clusters

展示当前 base run 中 `review_pending` cluster 的摘要，方便对比：

- cluster id
- retained member count
- distinct photo count
- representative / retained / excluded 计数
- 是否被提升为临时 seed

### 5. assignment buckets

按 `auto_assign`、`review`、`reject` 三个 bucket 分区展示 observation 卡片。

每张 observation 卡片至少展示：

- crop
- context 或 preview
- observation id / photo id
- source kind
- 原 cluster id（若存在）
- best candidate cluster
- top-k 候选及距离
- margin
- same-photo-conflict
- decision
- reason_code

单页允许较长，但必须保证每个 bucket 自带计数和折叠入口，避免整页完全摊开。

## manifest 契约

`manifest.json` 至少包含以下顶层字段：

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

其中 `assignment_summary` 至少包含：

- `candidate_count`
- `auto_assign_count`
- `review_count`
- `reject_count`
- `same_photo_conflict_count`
- `missing_embedding_count`

manifest 的主要用途是：

- 快速比较不同参数导出的结果差异
- 在不打开 HTML 时先看整体统计
- 为后续脚本化批量调参提供稳定输入

## 错误处理

### 硬失败

以下情况必须直接失败并返回非零退出码：

- workspace 不存在或配置损坏
- 找不到 base run
- base run 不是 `succeeded`
- 没有任何可用 seed identity
- 所有候选 observation 都缺少可用 embedding

### 软失败

以下情况不应中断整轮导出，而应记录到 manifest 和页面摘要：

- 个别 observation 的 crop/context 导出失败
- 个别 cluster 无法产出 prototype
- 个别 observation 因维度不一致被跳过
- 个别 preview 原图缺失

## CLI 方案

第一版只提供一个正式入口：

- `scripts/export_identity_v3_1_report.py`

建议参数如下：

- `--workspace`
- `--base-run-id`
- `--promote-cluster-ids`
- `--disable-seed-cluster-ids`
- `--assign-source`
- `--top-k`
- `--auto-max-distance`
- `--review-max-distance`
- `--min-margin`
- `--output-root`

脚本输出保持与现有导出脚本一致的风格，打印：

- `output_dir`
- `index_path`
- `manifest_path`

## 测试与验收

第一版不追求大而全，只要求形成稳定离线闭环。最小测试集至少覆盖：

- CLI 参数解析与默认输出目录
- 在夹具 workspace 上成功导出 `index.html`、`manifest.json` 和图片资产
- `manifest.json` 包含约定字段和 bucket 计数
- `index.html` 包含 `seed identities`、`review_pending clusters`、`auto_assign`、`review`、`reject` 几个区块
- `--promote-cluster-ids` 和 `--disable-seed-cluster-ids` 能改变 seed identity 集合
- 当没有有效 seed identity 时，脚本返回非零退出码

验收标准如下：

- 不启动服务也能完成一次完整验证
- 用户只看 `index.html` 就能快速判断某轮参数下的 seed 与 assign 表现
- 用户只看 `manifest.json` 就能比较两轮导出的统计差异
- 工具完全不依赖 live `person` 层即可工作

## 后续衔接

如果该工具验证结果证明方向正确，后续 phase2 可以直接吸收以下结论：

- 哪些 `materialized / promoted` cluster 适合作为日常 identity seed
- assign 的距离门、margin 门和同图冲突门应该如何收敛
- `review_pending` cluster 与 `attachment` observation 在正式产品中应如何进入 review queue

但在本阶段，这些结论只作为快速验证依据，不自动转化为正式 schema 或正式运行时接口。
