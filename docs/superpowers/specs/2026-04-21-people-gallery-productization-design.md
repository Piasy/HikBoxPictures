# HikBox Pictures 人物图库产品化设计（v5 冻结语义，统一重构）

## 1. 背景与目标

当前仓库 `docs/group_pics_algo.md` 的 v5 已完成算法原型验证，识别与归类效果可接受。本轮目标不是继续调算法，而是把现有原型重构为可长期维护的产品形态。

本设计的核心原则：

- 算法行为冻结：保持 v5 归属语义，目标“业务一致”。
- 产品能力补齐：工作区、扫描恢复、人物维护、导出账本、可观测与验收。
- 工程风险可控：明确数据真相、状态机、约束、参数快照与验收口径。

## 2. 已确认决策（硬约束）

1. 本地单机、单用户、仅 `localhost` WebUI。
2. 统一重构（方案 3），但算法行为冻结。
3. 一致性目标：与当前原型业务一致，允许少量边缘样本差异。
4. 不兼容旧 `manifest/pipeline.db` 导入，仅支持重扫建库。
5. `det_size` 固定 `640`。
6. `workers` 默认 `max(1, cpu_count // 2)`。
7. 子进程批处理保留，默认 `batch_size=300`，并且是“单轮总处理照片数上限”。
8. `flip embedding` 必须入库，不再用 JSON 缓存。
9. 保留 `external_root` 设计。
10. `output_root` 为模板显式绝对路径，可在 `external_root` 外。
11. 删除独立“待审核”页面。
12. 删除 `Identity Run` 证据页。
13. 人物库首页分“已命名/匿名”两区，不做搜索筛选。
14. 人物维护集中在详情页：重命名、单条排除、批量排除。
15. 排除后：永不自动归回该人物；后续可归入其他人物。
16. 首页支持多选批量合并；只允许撤销“全局最近一次合并”。
17. 导出模板仅可选择已命名人物。
18. Live Photo 导出始终开启（不提供开关）。

## 3. 范围与非范围

### 3.1 范围内

- workspace/external_root 路径模型。
- 多源扫描、阶段化执行、断点恢复。
- `crop/aligned/context` 产物生成。
- v5 冻结语义引擎接入。
- 人物库首页 + 人物详情维护。
- 批量合并与“最近一次撤销”。
- 导出模板、预览、执行、交付账本。
- Live Photo 配对导出。
- only/group 分桶与 `YYYY-MM` 目录组织。
- 运行日志与轻量审计。

### 3.2 范围外

- 多用户协作、账号系统、远程访问。
- 视频内容识别或视频人脸分析。
- 模型训练、阈值优化、`det_size=1280` 试验。
- 历史原型产物自动迁移。

## 4. 架构总览

- **真相层（SQLite）**：人物、归属、排除、扫描会话、导出账本、审计样本。
- **引擎层（v5 冻结）**：detect/embed/cluster/person merge/recall/assignment。
- **调度层**：单活扫描会话 + 子进程批处理 + 资产级幂等续跑。
- **Web/API 层**：人物库、人物详情、扫描页、导出页、日志页。
- **产物层**：`external_root/artifacts`、`external_root/logs`。

## 5. 路径与工作区模型

### 5.1 workspace（本机）

```text
<workspace>/
  .hikbox/
    library.db
    config.json
```

`config.json`：

```json
{
  "version": 1,
  "external_root": "/absolute/path/to/external-root"
}
```

### 5.2 external_root（可网络盘）

```text
<external_root>/
  artifacts/
    crops/
    aligned/
    context/
    thumbs/
    ann/
  logs/
```

说明：

- 不要求固定 `exports/` 子目录。
- 导出目标由模板 `output_root` 决定。

## 6. 核心数据模型（可执行约束）

> 本节只写实现必须满足的关键字段与约束，不限制内部 ORM 组织方式。

### 6.1 资产与扫描

- `library_source`
- `scan_session`
- `scan_session_source`
- `scan_checkpoint`
- `scan_batch`（新增，显式批次 claim/ack）
- `scan_batch_item`（新增，批次内资产映射）
- `photo_asset`
- `face_observation`

`photo_asset` 关键字段：

- `id`
- `library_source_id`
- `primary_path`
- `primary_fingerprint`
- `fingerprint_algo`（固定 `sha256`）
- `file_size`
- `mtime_ns`
- `capture_datetime`
- `capture_month`
- `is_live_photo`
- `live_mov_path`
- `live_mov_size`
- `live_mov_mtime_ns`

`face_observation` 关键字段：

- `id`
- `photo_asset_id`
- `bbox_*`
- `quality_score`
- `active`
- `pending_reassign`（`0/1`，排除后置 `1`，assignment 成功后清零）

### 6.2 向量与人物真相

- `face_embedding`
- `person`
- `assignment_run`（新增，归属版本）
- `person_face_assignment`
- `person_face_exclusion`

`face_embedding` 约束：

- `feature_type='face'`
- `variant in ('main','flip')`
- 唯一键：`(face_observation_id, feature_type, model_key, variant)`

`person` 关键字段：

- `id`
- `person_uuid`（`TEXT NOT NULL UNIQUE`，跨运行稳定 ID）
- `display_name`（可空；空表示匿名人物）
- `is_named`（`0/1`，与 `display_name` 一致）
- `status`（`active|merged|ignored`）
- `merged_into_person_id`（可空）
- `created_at`
- `updated_at`

`assignment_run` 关键字段：

- `id`
- `algorithm_version`（例如 `v5_frozen_2026_04_20`）
- `param_snapshot_json`（完整参数快照）
- `run_kind`（`scan_full|scan_incremental|scan_resume|reassign`）
- `started_at`
- `finished_at`
- `status`

`person_face_assignment` 关键字段与约束：

- `id`
- `person_id`
- `face_observation_id`
- `assignment_run_id`
- `assignment_source`（`auto|manual|merge|undo`）
- `active`
- `confidence` / `margin`（可空）

硬约束：

- 同一 observation 同一时刻最多一个 active assignment。
- 该约束必须由 DB 级唯一约束保证（例如 partial unique index）。

`person_face_exclusion` 关键字段与约束：

- `id`
- `person_id`
- `face_observation_id`
- `reason`（`manual_exclude`）
- `active`
- `created_at`
- `updated_at`

硬约束：

- 同一 `(person_id, face_observation_id)` 只能有一条 active exclusion。
- 自动归属候选阶段必须强制过滤 active exclusion。

### 6.3 合并与撤销

- `merge_operation`
- `merge_operation_person_delta`
- `merge_operation_assignment_delta`

约束：

- `merge_operation` 必须记录全量可回滚快照。
- 首版只允许撤销“全局最近一次且未撤销”的操作。

### 6.4 导出

- `export_template`
- `export_template_person`
- `export_run`
- `export_delivery`

约束：

- `export_template_person` 只能引用 `person.is_named=1 AND status='active'`。
- `export_delivery` 用于增量跳过/补齐。

### 6.5 可观测与审计

- `ops_event`
- `scan_audit_item`（新增，轻量人工审计样本）

## 7. v5 冻结契约（不可隐式漂移）

### 7.1 冻结链路

必须保留以下判定链路语义：

1. detect + aligned + `crop/context` 产物生成。
2. embedding：生成 `main` 与 `flip`。
3. HDBSCAN 微簇。
4. face 级质量门控。
5. 低质量微簇回退。
6. noise 的 `person_consensus` 回挂。
7. 二阶段 AHC 人物合并。
8. 非-noise 微簇 `cluster->person` recall。

### 7.2 默认参数快照（首版冻结值）

- `min_cluster_size=2`
- `min_samples=1`
- `person_merge_threshold=0.26`
- `person_linkage='single'`
- `person_rep_top_k=3`
- `person_knn_k=8`
- `person_enable_same_photo_cannot_link=false`
- `embedding_enable_flip=true`
- `embedding_flip_weight=1.0`（晚融合补充通道）
- `person_consensus_distance_threshold=0.24`
- `person_consensus_margin_threshold=0.04`
- `person_consensus_rep_top_k=3`
- `face_min_quality_for_assignment=0.25`
- `low_quality_micro_cluster_max_size=3`
- `low_quality_micro_cluster_top2_weight=0.5`
- `low_quality_micro_cluster_min_quality_evidence=0.72`
- `person_cluster_recall_distance_threshold=0.32`
- `person_cluster_recall_margin_threshold=0.04`
- `person_cluster_recall_top_n=5`
- `person_cluster_recall_min_votes=3`
- `person_cluster_recall_source_max_cluster_size=20`
- `person_cluster_recall_source_max_person_faces=8`
- `person_cluster_recall_target_min_person_faces=40`
- `person_cluster_recall_max_rounds=2`

衍生公式冻结：

- `quality_score = magface_quality * max(0.05, det_confidence) * sqrt(max(face_area_ratio, 1e-9))`
- 微簇回退证据分：`quality_evidence = top1 + 0.5 * top2`

### 7.3 冻结闸门

- 每次 `assignment_run` 必须落 `algorithm_version + param_snapshot_json`。
- 任何参数或判定逻辑改动，必须显式升级 `algorithm_version`。
- 未升级版本号但行为发生变化视为阻断缺陷。

### 7.4 可验收一致性口径

在固定金标回归集上，同一输入、同一 `algorithm_version`、同一参数快照下：

- 产出 `assignment_signature`：
  - 对 active assignment 按 `face_observation_id` 排序，拼接 `observation_id->person_uuid`。
  - 对 noise 拼接 `observation_id->noise`。
  - 全量字符串取 `sha256`。
- 与基线签名不一致时，必须输出差异样本清单并人工签字确认后方可发布。

## 8. 时间与指纹规则

### 8.1 `capture_datetime`

metadata 阶段按优先级解析：

1. EXIF `DateTimeOriginal`（36867）
2. EXIF `DateTimeDigitized`（36868）
3. EXIF `DateTime`（306）
4. 文件 `birthtime`（若系统可用）
5. 文件 `mtime`

规则：

- 存 ISO8601。
- EXIF 无时区时按运行机器本地时区解释。
- `capture_month` 由 `capture_datetime` 生成。

### 8.2 `primary_fingerprint`

- 仅对静态识别输入文件计算。
- 全文件 `sha256`（64 位小写十六进制）。
- 增量扫描先比较 `(file_size, mtime_ns)`，变化再重算哈希。

## 9. Live Photo 规则（识别、配对、导出）

### 9.1 配对触发条件

仅当静态文件扩展名为 `HEIC/HEIF`（大小写不敏感）时尝试配对。

### 9.2 同目录匹配规则

给定静态文件：

- `filename_with_ext`：例如 `IMG_7379.HEIF`
- `stem`：例如 `IMG_7379`

在同目录匹配隐藏 `MOV`（后缀大小写不敏感），满足任一模式：

1. `^\.` + `stem` + `_[0-9]+\.mov$`
2. `^\.` + `filename_with_ext` + `_[0-9]+\.mov$`

示例均有效：

- `IMG_8175.HEIC` ↔ `.IMG_8175_1771856408349261.MOV`
- `IMG_7379.HEIF` ↔ `.IMG_7379.HEIF_1771856408349261.MOV`

若命中多个：

- 优先按文件名中的数字时间戳降序；
- 若时间戳相同，再按 `mtime_ns` 取最新。

### 9.3 行为约束

- `MOV` 永不参与人脸识别/聚类。
- 配对成功：导出时与静态图写入同一目标年月目录。
- 配对失败：仍导出静态图，并在 `export_run` 摘要写 `warning`。

## 10. 排除与再归属规则

执行排除（单条或批量）时，必须同事务执行：

1. 停用当前 active assignment。
2. 写入/激活 `person_face_exclusion`。
3. 将 `face_observation.pending_reassign=1`。

后续自动归属：

- 候选人物若命中 active exclusion，直接硬过滤。
- observation 仍可归入其他人物或匿名人物。

下次 scan 的归属输入集合：

- 新增/变化资产 observation；
- 无 active assignment 的 observation；
- `pending_reassign=1` observation。

## 11. 扫描状态机与并发执行（落地语义）

### 11.1 阶段定义

`discover -> metadata -> detect -> embed -> cluster -> assignment`

### 11.2 单活会话规则

- 任意时刻只允许一个 active scan 会话（`running|aborting`）。
- `start_or_resume`：
  - 若有 active 会话，返回该会话（幂等）；
  - 否则优先恢复最近 resumable 会话；
  - 若无可恢复会话，创建新会话。
- `start_new`：
  - 默认若存在 active/resumable 会话返回 `409`；
  - 显式 `abandon_resumable=true` 时，将旧 resumable 标记 `abandoned`，再创建新会话。
- `abort`：
  - 会话置 `aborting`，停止新批次 claim；
  - 等待在跑子进程 grace timeout；超时后终止；
  - 未 ack 批次回退到 pending；会话置 `interrupted`。

### 11.3 批次 claim/ack 模型

- `batch_size` 表示单轮总照片数上限（默认 300）。
- `workers=N` 时本轮总量均分到 N 个子进程。
  - 例：300/3 => 每子进程 100（余数按前序 worker +1）。
- 主进程职责：
  - 事务内 claim 批次（写 `scan_batch/scan_batch_item`）；
  - 派发子进程；
  - 汇总结果并统一写库；
  - ack 批次成功/失败。
- 子进程职责：
  - 加载模型；
  - 处理分配批次；
  - 输出结果；
  - 释放模型并退出。

### 11.4 SQLite 锁与写入策略

- 使用 `WAL` + `busy_timeout`。
- 子进程禁止直接写业务真相表（避免多写者锁冲突）。
- 主进程单写者提交阶段结果。

### 11.5 半批次失败处理

- 子进程崩溃：该批次置 failed 或重试；未 ack 不推进资产阶段状态。
- 批次中间已写出的产物文件视为临时产物，下次重跑允许覆盖。
- 产物写入采用临时文件 + rename，避免部分写损坏。

## 12. WebUI 交互设计

### 12.1 导航

保留：人物库、源目录与扫描、导出模板、运行日志。

删除：待审核、Identity Run 证据页。

### 12.2 人物库首页

- 两分区：已命名人物、匿名人物。
- 无搜索筛选。
- 支持多选批量合并。
- 提供“撤销最近一次合并”。

### 12.3 人物详情页

- 主体为 active 样本网格。
- 默认仅展示 `context`。
- 点击样本展开 `crop + context`。
- Live 样本在 context 标 `Live`。
- 支持单条/批量排除。

### 12.4 源目录与扫描页

- 展示会话状态、source 进度、失败统计。
- 支持恢复/停止/放弃并新建。
- 显示当前 `det_size/workers/batch_size`。

### 12.5 导出模板页

- 模板创建/编辑/预览/执行/历史。
- 仅可选择已命名人物。
- Live 配对导出默认开启且不可关闭。
- 展示 only/group 统计和样例。

### 12.6 运行日志页

- 展示关键事件。
- 支持 run 维度过滤。

## 13. 导出规则（only/group 与 YYYY-MM）

### 13.1 命中规则

照片命中模板需满足：

- 模板中每个已选人物在该照片里至少有一个 active assignment。

### 13.2 only/group 分桶

- `selected_faces`：属于模板人物的 active assignment。
- `selected_min_area`：上述人脸最小面积。
- `threshold = selected_min_area / 4`。

归 `group` 条件：

1. 有额外非模板人物人脸且面积 `>= threshold`；或
2. 有未归属人脸且面积 `>= threshold`；或
3. 存在额外人脸面积缺失。

否则归 `only`。

### 13.3 目录组织

`output_root` 为模板显式绝对路径，导出结构：

```text
<output_root>/
  only/YYYY-MM/
  group/YYYY-MM/
```

- `YYYY-MM` 优先来自 `capture_datetime`，缺失时回退文件 `mtime`。
- 静态图写入对应 bucket/month。
- 若存在 Live 配对 `MOV`，同目录写入。

## 14. 轻量审计机制（替代原 review 页）

不保留独立审核队列，但必须保留可复核能力：

### 14.1 `scan_audit_item` 采样来源

每次 `assignment_run` 后生成审计样本，至少三类：

1. `low_margin_auto_assign`：自动归属但 margin 接近阈值的样本。
2. `reassign_after_exclusion`：因排除重新归属的样本。
3. `new_anonymous_person`：新匿名人物的代表样本。

### 14.2 审计入口

- 在“源目录与扫描”页显示“本次扫描审计摘要”。
- 每条样本可跳转到对应人物详情并定位样本。
- 导出前预览继续作为交付前人工确认入口。

说明：这不是待审核队列，不承载“必须处理完才能继续”的工作流，仅用于人工验收与回归复核。

## 15. API 与模块边界

### 15.1 页面路由

- `GET /`
- `GET /people/{id}`
- `GET /sources`
- `GET /exports`
- `GET /logs`

### 15.2 核心动作 API

- `POST /api/scan/start_or_resume`
- `POST /api/scan/abort`
- `POST /api/scan/start_new`
- `POST /api/people/{id}/actions/rename`
- `POST /api/people/{id}/actions/exclude-assignment`
- `POST /api/people/{id}/actions/exclude-assignments`
- `POST /api/people/actions/merge-batch`
- `POST /api/people/actions/undo-last-merge`
- `GET/POST/PUT/DELETE /api/export/templates...`
- `POST /api/export/templates/{id}/actions/run`

### 15.3 审计 API（新增）

- `GET /api/scan/{session_id}/audit-items`

## 16. 失败处理与可恢复性

- 单图失败不阻断整批。
- 子进程崩溃仅影响当前批次。
- 恢复默认续跑最近 resumable 会话。
- 排除、合并、撤销必须事务一致。
- Live `MOV` 缺失只记 warning，不影响静态图导出。

## 17. 验收标准（实现前置检查清单）

1. DB 固定在 `workspace/.hikbox/library.db`。
2. `crop/aligned/context/logs` 写到 `external_root`。
3. `scan` 默认 `workers=max(1,cpu//2)`、`batch_size=300`，且 `batch_size` 为单轮总量上限。
4. 子进程一批一退出，模型内存可释放。
5. `face_embedding` 每 observation 存 `main/flip` 两条记录。
6. `person` 含稳定 `person_uuid`，并有 `is_named` 语义。
7. 同 observation 只能有一个 active assignment（DB 级约束）。
8. `assignment_run` 记录 `algorithm_version+param_snapshot_json`。
9. v5 默认参数快照与冻结公式全部落盘。
10. Live 配对同时支持 `HEIC`/`HEIF`，支持 `.<stem>_<ts>.MOV` 与 `.<filename_with_ext>_<ts>.MOV`。
11. 人物库首页仅已命名/匿名分区，无搜索筛选。
12. 无待审核页、无 Identity Run 证据页。
13. 排除后样本不会再自动归回原人物，但可归入其他人物。
14. 首页支持批量合并与“撤销全局最近一次合并”。
15. 导出模板仅可选已命名人物，目录为 `only/group/YYYY-MM`。
16. 保留轻量审计入口，可对低 margin/重归属/新匿名样本复核。

## 18. 风险与后续

### 18.1 已知风险

- 统一重构改动面大，需严格回归。
- 网络盘 I/O 波动会影响扫描和导出尾延迟。
- “仅撤销最近一次合并”需要 UI 强提示，防止误预期。

### 18.2 后续扩展（不在首版）

- 细粒度合并历史回滚。
- 更复杂导出表达式。
- 原图层级预览回归（如后续产品再次需要）。
