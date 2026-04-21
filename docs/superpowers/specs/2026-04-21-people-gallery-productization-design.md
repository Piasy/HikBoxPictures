# HikBox Pictures 人物图库产品化设计（v5 冻结语义，统一重构）

## 1. 背景与目标

当前仓库 `docs/group_pics_algo.md` 的 v5 已完成算法原型验证，识别与归类效果可接受。本轮目标不是继续调算法，而是把现有原型重构为可长期维护的产品形态。

本设计的核心原则：

- 算法行为冻结：保持 v5 归属语义，目标“业务一致”。
- 首版迁移保守：不兼容旧原型产物，不做导入迁移，只接受从源目录全量重扫重建。
- 产品能力补齐：工作区、扫描恢复、人物维护、导出账本、可观测与验收。
- 工程风险可控：明确数据真相、状态机、约束、参数快照与验收口径。

## 2. 已确认决策（硬约束）

1. 本地单机、单用户、仅 `localhost` WebUI。
2. 统一重构（方案 3），但算法行为冻结。
3. 一致性目标：与当前原型业务一致，允许少量边缘样本差异。
4. 不兼容旧 `manifest/pipeline.db` 导入；首版从零建库。该约束仅针对“旧原型数据迁移”，不等于禁用新系统内部增量扫描。
5. `det_size` 固定 `640`。
6. `workers` 默认 `max(1, cpu_count // 2)`。
7. 子进程/并发仅用于链路第一步（`detect + aligned + crop/context` 产物生成）；`batch_size=300` 定义为该阶段“单轮总处理照片数上限”。
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
19. WebUI 采用 FastAPI + 纯 HTML 模板（Jinja2）；不引入 React/Vue 等前端框架。
20. `external_root/artifacts` 仅保留 `crops/aligned/context`，删除 `thumbs/` 与 `ann/` 设计。
21. embedding 独立存储到 `workspace/.hikbox/embedding.db`，与业务真相库 `library.db` 分离。
22. 首版不做复杂并发协同：若存在 `running|aborting` 扫描会话，`serve` 命令启动时直接报错退出；不保障多标签页并发一致性；导出进行中禁止修改人物归属与合并。

补充说明：

- 首版迁移策略固定为：**无兼容层、无导入器、无自动迁移、仅全量重扫建库**。
- 新系统初始化时，只信任源目录原始照片与当前 `workspace/.hikbox/`、`external_root/` 下的新产物。
- 旧原型中的 `manifest.json`、`pipeline.db`、review HTML、JSON diff、缓存目录都不作为输入真相。
- 旧原型中的命名结果、排除关系、合并历史、导出模板、导出账本、审计记录均不继承；需要在新库上重新生成或重新维护。

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
- 跨 `library_source` 的重复照片去重（相同 `fingerprint` 仍按独立资产处理）。
- 人物库首页/详情页的大规模分页或虚拟滚动优化。
- 磁盘空间预检测、配额管理与自动回收。

## 4. 架构总览

- **真相层（SQLite）**：`library.db`（人物、归属、排除、扫描会话、导出账本、审计）+ `embedding.db`（向量）。
- **引擎层（v5 冻结）**：detect/embed/cluster/person merge/recall/assignment。
- **调度层**：单活扫描会话 + detect 阶段子进程批处理 + 资产级幂等续跑。
- **Web/API 层**：人物库、人物详情、扫描页、导出页、日志页。
- **产物层**：`external_root/artifacts`、`external_root/logs`。

## 5. 路径与工作区模型

### 5.1 workspace（本机）

```text
<workspace>/
  .hikbox/
    library.db
    embedding.db
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
  logs/
```

说明：

- 不要求固定 `exports/` 子目录。
- 导出目标由模板 `output_root` 决定。

### 5.3 首版初始化与旧产物处理

首版产品化版本的初始化方式固定如下：

1. 准备 `workspace`、`external_root` 与待扫描源目录。
2. 创建新的 `workspace/.hikbox/library.db` 与 `workspace/.hikbox/embedding.db`。
3. 从源目录重新执行 `discover -> metadata -> detect -> embed -> cluster -> assignment`。
4. 基于新库重新生成人物、排除、导出模板与后续运行账本。

边界说明：

- 不读取旧 `manifest.json`/`pipeline.db` 作为导入源。
- 不要求兼容旧产物目录结构；旧 `crop/aligned/context/review` 产物可保留作人工参考，但不参与新系统判定。
- 若用户希望保留旧结果，只能以“人工对照参考”的方式使用，不能作为程序输入。

### 5.4 首版容量基线（用于运维预估）

- 向量容量粗估：`照片数 * 平均脸数 * 2(main+flip) * 512维 * 4Bytes/float32`（即约 `2048Bytes/向量`）。
- 例如 `100000` 张照片、平均 `2` 张脸时，`embedding.db` 约 `~800MB`（不含索引与元数据）。
- `library.db` 主要为元数据与关系表，体量通常远小于 `embedding.db`，分库后便于备份与巡检。

## 6. 核心数据模型（以 `docs/db_schema.md` 为准）

> 本文不重复完整 DDL。完整表结构、字段、索引、约束与 migration 规则见 [docs/db_schema.md](../../db_schema.md)。

### 6.1 资产与扫描（`library.db`）

本设计依赖以下表与关键字段：

- `library_source`：`root_path`、`label`、`enabled`。
- `photo_asset`：`primary_path`、`primary_fingerprint`、`capture_datetime`、`capture_month`、`is_live_photo`、`live_mov_path`。
- `face_observation`：`quality_score`、`active`、`pending_reassign`。
- `scan_session`：`run_kind`、`status`。
- `scan_checkpoint`：`stage`、`cursor_json`。
- `scan_batch` / `scan_batch_item`：`status`（仅 detect 阶段 claim/ack 与失败恢复）。

### 6.2 向量与人物真相

- `embedding.db.face_embedding`：`face_observation_id`、`variant(main|flip)`、`vector_blob`；唯一键 `(face_observation_id, feature_type, model_key, variant)`。
- `library.db.person`：`person_uuid`、`display_name`、`is_named`、`status`、`merged_into_person_id`。
- `assignment_run`：`scan_session_id`、`algorithm_version`、`param_snapshot_json`、`run_kind`、`status`。
- `person_face_assignment`：`person_id`、`face_observation_id`、`assignment_run_id`、`assignment_source`、`active`。
- `person_face_exclusion`：`person_id`、`face_observation_id`、`active`。

关键语义：

- `person_uuid` 为创建时固定 UUIDv4；合并保留样本数更大者；样本数相同取 `selected_person_ids[0]`。
- `assignment_source` 仅允许 `hdbscan|person_consensus|merge|undo`，不存在 `manual`。
- `noise` 与 `low_quality_ignored` 仅表示本轮“未归属”判定，不写入 `person_face_assignment`。
- 同一 observation 同时最多一条 active assignment；同一 `(person_id, face_observation_id)` 同时最多一条 active exclusion。

### 6.3 合并与撤销

- `merge_operation`：`selected_person_ids_json`、`winner_person_id`、`winner_person_uuid`、`status`。
- `merge_operation_person_delta`、`merge_operation_assignment_delta` 与 `merge_operation_exclusion_delta`：记录可回滚快照。

约束：首版仅允许撤销“全局最近一次且未撤销”的合并。

### 6.4 导出

- `export_template`：`name`、`output_root`。
- `export_template_person`：`template_id`、`person_id`。
- `export_run`：`status`、`summary_json`。
- `export_delivery`：`media_kind`、`bucket`、`month_key`、`destination_path`、`delivery_status`。

约束：

- `export_template_person` 只能引用 `person.is_named=1 AND status='active'`。
- 同名文件冲突写 `export_delivery.delivery_status='skipped_exists'`，不覆盖、不改名。
- 首版不提供模板删除能力（无删除 API/CLI），以避免历史 `export_run` 账本失联。

### 6.5 可观测与审计

- `ops_event`：`event_type`、`severity`、`scan_session_id`、`export_run_id`、`payload_json`。
- `scan_audit_item`：`assignment_run_id`、`audit_type`、`face_observation_id`、`person_id`、`evidence_json`。

## 7. v5 冻结契约（不可隐式漂移）

### 7.1 冻结链路（冻结的是 2026-04-21 代码档位）

必须保留以下判定链路语义：

1. detect + aligned + `crop/context` 产物生成。
2. embedding：生成 `main` 与 `flip`。
3. HDBSCAN 微簇。
4. face 级质量门控。
5. 低质量微簇回退。
6. 预备 AHC merge（仅用于构建 person 结构，供后续 consensus 使用）。
7. noise 的 `person_consensus` 回挂，晚融合固定为 `max(sim_main, sim_flip)`。
8. 最终 AHC 人物合并。
9. 非-noise 微簇 `cluster->person` recall。

### 7.2 默认参数快照（首版冻结值）

- `det_size=640`
- `preview_max_side=480`
- `min_cluster_size=2`
- `min_samples=1`
- `person_merge_threshold=0.26`
- `person_linkage='single'`
- `person_rep_top_k=3`
- `person_knn_k=8`
- `person_enable_same_photo_cannot_link=false`
- `embedding_enable_flip=true`
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

约束：

- 不提供 `embedding_flip_weight` 参数或开关。
- 晚融合补充通道固定为 `max(sim_main, sim_flip)`。

衍生公式冻结：

- `quality_score = magface_quality * max(0.05, det_confidence) * sqrt(max(face_area_ratio, 1e-9))`
- 微簇回退证据分：`quality_evidence = top1 + 0.5 * top2`

### 7.3 冻结闸门

- 每次 `assignment_run` 必须落 `algorithm_version + param_snapshot_json`。
- 任何参数或判定逻辑改动，必须显式升级 `algorithm_version`。
- 未升级版本号但行为发生变化视为阻断缺陷。

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
- 全文件 `sha256`（`256` 位哈希，`64` 字符小写十六进制）。
- 增量扫描先比较 `(file_size, mtime_ns)`，变化再重算哈希。
- 首版简化策略：只要 `(file_size, mtime_ns)` 任一变化，资产按“全阶段重跑”处理（`metadata -> detect -> embed -> cluster -> assignment`），不区分“仅 metadata 变化”和“像素变化”。

## 9. Live Photo 规则（识别、配对、导出）

### 9.1 配对触发条件

仅当静态文件扩展名为 `HEIC/HEIF`（大小写不敏感）时尝试配对。

配对时机：

- 在扫描链路的 `metadata` 阶段完成配对，并把结果写入 `photo_asset.live_mov_path/live_mov_size/live_mov_mtime_ns`。
- 导出阶段不再做目录扫描配对，只读取库中已落盘的配对结果。

说明：

- 首版有意不支持 JPG Live Photo 配对（例如部分 Android/旧 iPhone 变体），以降低识别歧义与维护复杂度。

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
- 导出静态图时，若 `photo_asset.live_mov_path` 存在，则对应 `MOV` 与静态图写入同一目标年月目录。
- 若 `photo_asset.live_mov_path` 为空或文件缺失，导出阶段仅导出静态图，静默跳过 `MOV`，不额外写 warning。

## 10. 排除与再归属规则

执行排除（单条或批量）时，必须同事务执行：

1. 停用当前 active assignment。
2. 写入/激活 `person_face_exclusion`。
3. 将 `face_observation.pending_reassign=1`。

后续自动归属：

- 候选人物若命中 active exclusion，直接硬过滤。
- observation 仍可归入其他人物或匿名人物。

`pending_reassign` 触发时机：

- 不做排除后的即时后台重算。
- 在用户下一次手动发起扫描（`start_or_resume` 或 `start_new`）时，自动纳入归属输入集合处理。
- `pending_reassign` 不触发独立 `reassign` 会话类型，而是并入常规扫描会话的归属阶段处理。

下次 scan 的归属输入集合：

- 新增/变化资产 observation；
- 无 active assignment 的 observation；
- `pending_reassign=1` observation。

合并与撤销补充约束：

- 批量合并时，所有 loser 人物上的 active exclusion 必须迁移到 winner（同一 `(winner_person_id, face_observation_id)` 已存在 active exclusion 时跳过重复写入）。
- 撤销最近一次合并时，必须按 `merge_operation_exclusion_delta` 回滚本次排除迁移变更。

## 11. 扫描状态机与并发执行（首版简化语义）

### 11.1 阶段定义

`discover -> metadata -> detect -> embed -> cluster -> assignment`

### 11.2 单活会话与状态迁移

状态迁移：

| 当前状态 | 触发动作 | 下一状态 | 说明 |
| --- | --- | --- | --- |
| `pending` | `start` | `running` | 新会话启动 |
| `running` | `abort` | `aborting` | 停止 claim 新批次 |
| `aborting` | `grace_timeout/force_kill` | `interrupted` | 未 ack 批次回退 pending |
| `running` | `all_done` | `completed` | 全阶段完成 |
| `running` | `unexpected_error` | `interrupted` | 可恢复失败 |
| `interrupted` | `start_or_resume` | `running` | 恢复最近可恢复会话 |
| `interrupted` | `start_new` | `abandoned` | 旧会话废弃，随后创建新会话 |
| 任意 | `fatal_error` | `failed` | 不可恢复错误 |

动作规则：

- 任意时刻只允许一个 active scan 会话（`running|aborting`）。
- `run_kind` 仅保留 `scan_full|scan_incremental|scan_resume`；`pending_reassign` 归属并入常规扫描，不创建独立 `reassign` 会话。
- `start_or_resume`：有 active 会话则直接返回该会话；无 active 时优先恢复最近 `interrupted` 会话；仍无可恢复会话时创建新会话。
- `start_new`：若存在 active 会话返回 `409`；否则将最近 `interrupted` 会话标记 `abandoned`（若有），再创建新会话。
- `abort`：会话置 `aborting`，等待在跑子进程 grace timeout；超时后终止并回退未 ack 批次。

### 11.3 批次 claim/ack 模型

- 子进程批处理仅用于阶段 1：`detect + aligned + crop/context` 产物生成。
- `batch_size` 表示该阶段单轮总照片数上限（默认 300）。
- `workers=N` 时本轮总量均分到 N 个子进程（例：300/3 => 每子进程 100，余数按前序 worker +1）。
- `discover`、`metadata`、`embed`、`cluster`、`assignment` 均由主进程串行执行，不使用 `scan_batch` claim/ack。
- detect 阶段主进程职责：
  - 事务内 claim 批次（写 `scan_batch/scan_batch_item`）；
  - 派发子进程；
  - 汇总结果并统一写库；
  - ack 批次成功/失败。
- detect 阶段子进程职责：
  - 加载检测模型；
  - 处理分配批次并生成 `aligned/crop/context` 产物；
  - 输出结果；
  - 释放模型并退出。

### 11.4 SQLite 锁与写入策略

- 使用 `WAL` + `busy_timeout`。
- 子进程禁止直接写业务真相表（避免多写者锁冲突）。
- 主进程单写者提交阶段结果。
- `embedding.db` 也由主进程统一落盘，避免双库并发写放大锁竞争。

### 11.5 半批次失败处理

- 子进程崩溃：该批次置 `failed` 或重试；未 ack 不推进资产阶段状态。
- 批次中间已写出的产物文件视为临时产物，下次重跑允许覆盖。
- 产物写入采用临时文件 + rename，避免部分写损坏。

## 12. WebUI 交互设计

### 12.1 技术方案

- 服务端渲染：FastAPI + Jinja2 纯 HTML 模板。
- 交互增强：仅使用少量原生 JS（表单提交、局部刷新、确认弹窗）。
- 首版不引入前后端分离框架。

### 12.2 导航

保留：人物库、源目录与扫描、导出模板、运行日志。

删除：待审核、Identity Run 证据页。

### 12.3 人物库首页

- 两分区：已命名人物、匿名人物。
- 无搜索筛选。
- 支持多选批量合并。
- 提供“撤销最近一次合并”。
- 首版不承诺分页/虚拟滚动优化（按当前数据规模直接渲染）。

### 12.4 人物详情页

- 主体为 active 样本网格。
- 默认仅展示 `context`。
- 点击样本展开 `crop + context`。
- Live 样本在 context 标 `Live`。
- 支持单条/批量排除。
- 首版不承诺分页/虚拟滚动优化（按当前数据规模直接渲染）。

### 12.5 源目录与扫描页

- 展示会话状态、source 进度、失败统计。
- 支持恢复/停止/放弃并新建。
- 显示当前 `det_size/workers/batch_size`。

并发与可用性约束：

- `serve` 启动前必须检查扫描会话；若存在 `running|aborting` 会话，`serve` 直接报错退出，不进入 WebUI 运行态。
- 首版不保障多标签页并发一致性，按单标签页使用假设设计。

### 12.6 导出模板页

- 模板创建/编辑/预览/执行/历史。
- 仅可选择已命名人物。
- Live 配对导出默认开启且不可关闭。
- 展示 only/group 统计和样例。
- 导出运行中，人物归属/合并相关入口需禁用并提示“导出进行中，暂不可修改”。

### 12.7 运行日志页

- 展示关键事件。
- 支持 run 维度过滤。

## 13. 导出规则（only/group 与 YYYY-MM）

### 13.1 命中规则

照片命中模板需满足：

- 模板中每个已选人物在该照片里至少有一个 active assignment。

### 13.2 only/group 分桶

- `selected_faces`：属于模板人物的 active assignment。
- `selected_min_area`：上述人脸最小绝对像素面积，计算式为 `(bbox_x2 - bbox_x1) * (bbox_y2 - bbox_y1)`。
- `threshold = selected_min_area / 4`。
- `bbox_x1/y1/x2/y2` 均为原图尺度的绝对像素坐标（非归一化坐标）。

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
- 若 `photo_asset.live_mov_path` 存在且文件可读，`MOV` 同目录写入。

### 13.4 同名冲突处理

- 目标路径下已有同名文件时直接跳过。
- 不覆盖、不重命名。
- 在 `export_delivery` 记录 `delivery_status='skipped_exists'`，并计入 `export_run` 摘要。

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
- `GET /sources/{session_id}/audit`
- `GET /exports`
- `GET /exports/{id}`
- `GET /logs`

### 15.2 通用响应格式

成功：

```json
{
  "ok": true,
  "data": {}
}
```

失败：

```json
{
  "ok": false,
  "error": {
    "code": "ERROR_CODE",
    "message": "可读错误描述"
  }
}
```

常见错误码：

- `VALIDATION_ERROR`
- `NOT_FOUND`
- `SCAN_ACTIVE_CONFLICT`
- `EXPORT_RUNNING_LOCK`
- `ILLEGAL_STATE`

### 15.3 核心动作 API

说明：以下“响应”示例均表示通用响应体中的 `data` 字段内容。

`POST /api/scan/start_or_resume`

- 请求体：`{}`
- 响应：`{ session_id, status, resumed }`

`POST /api/scan/start_new`

- 请求体：`{}`
- 响应：`{ session_id, status }`
- 冲突：存在 active scan 时返回 `409 + SCAN_ACTIVE_CONFLICT`。

`POST /api/scan/abort`

- 请求体：`{ "session_id": 123 }`
- 响应：`{ session_id, status: "aborting" }`

`POST /api/people/{id}/actions/rename`

- 请求体：`{ "display_name": "张三" }`
- 响应：`{ person_id, display_name, is_named }`
- 命名策略：允许重名；若存在同名人物，UI 仅提示不阻断提交。

`POST /api/people/{id}/actions/exclude-assignment`

- 请求体：`{ "face_observation_id": 1001 }`
- 响应：`{ person_id, face_observation_id, pending_reassign: 1 }`

`POST /api/people/{id}/actions/exclude-assignments`

- 请求体：`{ "face_observation_ids": [1001, 1002] }`
- 响应：`{ person_id, excluded_count }`

`POST /api/people/actions/merge-batch`

- 请求体：`{ "selected_person_ids": [11, 22, 33] }`
- 响应：`{ merge_operation_id, winner_person_id, winner_person_uuid }`
- 规则：样本数相同时，按 `selected_person_ids[0]` 作为 tie-break winner。

`POST /api/people/actions/undo-last-merge`

- 请求体：`{}`
- 响应：`{ merge_operation_id, status: "undone" }`

`GET /api/export/templates`

- 响应：`{ items: [...] }`

`POST /api/export/templates`

- 请求体：`{ "name": "家庭合照", "output_root": "/abs/path" }`
- 响应：`{ template_id }`

`PUT /api/export/templates/{id}`

- 请求体：`{ "name": "新名称", "output_root": "/abs/path", "person_ids": [1,2] }`
- 响应：`{ template_id, updated: true }`

- 首版不提供模板删除 API。

`POST /api/export/templates/{id}/actions/run`

- 请求体：`{}`
- 响应：`{ export_run_id, status: "running" }`
- 锁：导出运行中触发人物归属/合并写操作返回 `409 + EXPORT_RUNNING_LOCK`。

### 15.4 审计 API（新增）

- `GET /api/scan/{session_id}/audit-items`
  - 响应：`{ items: [{ audit_type, face_observation_id, person_id, evidence_json }] }`

### 15.5 CLI 命令设计（首版，从零定义）

CLI 目标：

- 覆盖 WebUI 的核心能力，支持纯命令行完成初始化、扫描、人物维护、导出与诊断。
- 默认人类可读输出；`--json` 输出结构化结果，便于脚本集成。
- 统一入口命令名：`hikbox`（具体安装名可在实现阶段映射为 `hikbox-pictures`）。

全局选项：

- `--workspace <path>`：工作区根目录，默认当前目录。
- `--json`：JSON 输出。
- `--quiet`：仅输出错误。

命令树：

```text
hikbox
  init
  config
    show
    set-external-root <abs_path>
  source
    list
    add <abs_path> [--label <name>]
    remove <source_id>
    enable <source_id>
    disable <source_id>
    relabel <source_id> <label>
  scan
    start-or-resume
    start-new
    abort <session_id>
    status [--session-id <id>|--latest]
    list [--limit <n>]
  serve
    start [--host 127.0.0.1] [--port 8000]
  people
    list [--named|--anonymous]
    show <person_id>
    rename <person_id> <display_name>
    exclude <person_id> --face-observation-id <id>
    exclude-batch <person_id> --face-observation-ids <id1,id2,...>
    merge --selected-person-ids <id1,id2,...>
    undo-last-merge
  export
    template
      list
      create --name <name> --output-root <abs_path>
      update <template_id> [--name <name>] [--output-root <abs_path>] [--person-ids <id1,id2,...>]
    run <template_id>
    run-status <export_run_id>
    run-list [--template-id <id>] [--limit <n>]
  logs
    list [--scan-session-id <id>] [--export-run-id <id>] [--severity info|warning|error] [--limit <n>]
  audit
    list --scan-session-id <id>
  db
    vacuum [--library] [--embedding]
```

关键行为约束：

- `hikbox init`：
  - 初始化 `workspace/.hikbox/library.db`、`workspace/.hikbox/embedding.db` 与 `config.json`。
  - 若已存在库文件，仅做结构校验与 schema 版本检查。
- `hikbox scan start-or-resume`：对齐 `POST /api/scan/start_or_resume` 语义。
- `hikbox scan start-new`：对齐 `POST /api/scan/start_new` 语义；存在 active scan 返回冲突错误。
- `hikbox serve start`：
  - 启动前必须检查 `scan_session`。
  - 若存在 `running|aborting` 会话，直接报错退出，不启动 Web 服务。
- `hikbox people merge`：
  - 合并保留规则：样本数多者胜；样本数相同取 `--selected-person-ids` 第 1 个。
  - loser 人物 active exclusion 迁移到 winner；撤销时按快照回滚 exclusion 变更。
- `hikbox export run`：
  - 导出期间禁止人物归属/合并写操作。
  - `live_mov_path` 缺失或文件不可读时，静默跳过 `MOV`。
  - 同名目标文件按 `skipped_exists` 处理。
- 首版不提供 `hikbox export template delete`。

退出码（首版建议）：

- `0`：成功。
- `2`：参数校验失败（映射 `VALIDATION_ERROR`）。
- `3`：资源不存在（映射 `NOT_FOUND`）。
- `4`：扫描会话冲突（映射 `SCAN_ACTIVE_CONFLICT`）。
- `5`：导出锁冲突（映射 `EXPORT_RUNNING_LOCK`）。
- `6`：状态非法（映射 `ILLEGAL_STATE`）。
- `7`：`serve` 被 active scan 阻断（`SERVE_BLOCKED_BY_ACTIVE_SCAN`）。
- `1`：其他未分类错误。

典型用法：

```bash
# 1) 初始化工作区
hikbox init --workspace /data/hikbox_ws
hikbox config set-external-root /data/hikbox_external --workspace /data/hikbox_ws

# 2) 注册源目录并启动扫描
hikbox source add /photos/family --label family --workspace /data/hikbox_ws
hikbox scan start-or-resume --workspace /data/hikbox_ws
hikbox scan status --latest --workspace /data/hikbox_ws

# 3) 启动 WebUI（若扫描仍在 running/aborting 会直接失败退出）
hikbox serve start --workspace /data/hikbox_ws --host 127.0.0.1 --port 8000

# 4) 人物维护
hikbox people merge --selected-person-ids 11,22,33 --workspace /data/hikbox_ws
hikbox people undo-last-merge --workspace /data/hikbox_ws

# 5) 导出
hikbox export template create --name "家庭合照" --output-root /exports/family --workspace /data/hikbox_ws
hikbox export template update 1 --person-ids 11,22 --workspace /data/hikbox_ws
hikbox export run 1 --workspace /data/hikbox_ws
```

## 16. 失败处理与可恢复性

- 单图失败不阻断整批。
- 子进程崩溃仅影响当前批次。
- 恢复默认续跑最近 `interrupted` 会话。
- 排除、合并、撤销必须事务一致。
- 扫描在 `assignment` 阶段被 `abort` 时，运行中的 `assignment_run` 记为 `failed`（原因：`aborted_by_user`）。
- Live `MOV` 缺失时静默跳过，不影响静态图导出。
- 目标目录同名冲突按 `skipped_exists` 处理，不覆盖。
- 导出运行中对人物归属/合并的写操作一律拒绝并返回锁定错误。

## 17. 验收标准（实现前置检查清单）

1. DB 固定在 `workspace/.hikbox/library.db` 与 `workspace/.hikbox/embedding.db`。
2. `crop/aligned/context/logs` 写到 `external_root`，不再产出 `thumbs/ann`。
3. `scan` 默认 `workers=max(1,cpu//2)`、`batch_size=300`，且 `batch_size` 为单轮总量上限。
4. 子进程批处理仅用于 detect 阶段，且子进程一批一退出，模型内存可释放。
5. `face_embedding` 每 observation 存 `main/flip` 两条记录，且存于 `embedding.db`。
6. `person_uuid` 在创建时生成 UUIDv4 并固定；合并时按“样本数优先，平局取 `selected_person_ids[0]`”规则保留。
7. `person_face_assignment.assignment_source` 仅允许 `hdbscan|person_consensus|merge|undo`，`noise/low_quality_ignored` 不落 assignment。
8. 同 observation 只能有一个 active assignment（DB 级约束）。
9. `assignment_run` 记录 `scan_session_id + algorithm_version + param_snapshot_json`。
10. v5 参数快照包含 `preview_max_side=480`，且无 `embedding_flip_weight` 参数。
11. 冻结链路明确为“预备 AHC + person_consensus + 最终 AHC”两遍 AHC 语义。
12. Live 配对在扫描 `metadata` 阶段完成并写入 `photo_asset.live_mov_*`；匹配规则支持 `HEIC`/`HEIF` 与 `.<stem>_<ts>.MOV`、`.<filename_with_ext>_<ts>.MOV`。
13. 人物库首页仅已命名/匿名分区，无搜索筛选。
14. 无待审核页、无 Identity Run 证据页。
15. 排除后样本不会再自动归回原人物，但可归入其他人物；`pending_reassign` 在下一次手动扫描时处理（不创建独立 `reassign` run_kind）。
16. 首页支持批量合并与“撤销全局最近一次合并”。
17. 合并会迁移 loser 的 active exclusion 到 winner，撤销时回滚 exclusion 迁移。
18. 导出模板仅可选已命名人物，目录为 `only/group/YYYY-MM`，同名冲突跳过；`live_mov_path` 缺失时 `MOV` 静默跳过。
19. 首版不提供模板删除能力（API/CLI 均无删除入口）。
20. 保留轻量审计入口，可对低 margin/重归属/新匿名样本复核。
21. 扫描进行中执行 `serve` 必须直接报错退出；导出进行中禁止人物归属/合并写操作。
22. 首版 `docs/db_schema.md` 直接给出全量新 schema；后续版本通过 migration 演进并同步维护文档。

## 18. 风险与后续

### 18.1 已知风险

- 统一重构改动面大，需严格回归。
- 网络盘 I/O 波动会影响扫描和导出尾延迟。
- `embedding.db` 体量增长快，需要定期 `VACUUM` 与备份策略。
- “仅撤销最近一次合并”需要 UI 强提示，防止误预期。

### 18.2 后续扩展（不在首版）

- 细粒度合并历史回滚。
- 更复杂导出表达式。
- 原图层级预览回归（如后续产品再次需要）。
- 多标签页冲突检测与更完整并发控制。
