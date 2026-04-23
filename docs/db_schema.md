# 数据库 Schema 说明（首版全量设计）

本文档描述人物图库产品化首版的数据库设计。首版采用双库：

- `workspace/.hikbox/library.db`：业务真相与运行账本。
- `workspace/.hikbox/embedding.db`：向量数据。

## 1. 设计约定

- 数据库：SQLite。
- 时间字段：ISO8601 字符串（`TEXT`）。
- 布尔字段：`INTEGER`，取值 `0/1`。
- 枚举字段：`TEXT + CHECK` 约束。
- 主键：默认 `INTEGER PRIMARY KEY AUTOINCREMENT`。
- 首版仅支持“空库初始化创建全量 schema”，不做旧 `pipeline.db` 兼容导入。

## 2. 存储布局

```text
<workspace>/
  .hikbox/
    library.db
    embedding.db
    config.json

<external_root>/
  artifacts/
    crops/
    aligned/
    context/
  logs/
```

说明：

- `thumbs/` 与 `ann/` 不再属于首版产物目录。
- `library.db` 与 `embedding.db` 分离，用于降低锁冲突与备份成本。

## 3. 版本与迁移策略

### 3.1 首版（schema_version=1）

- 应用启动时若 DB 不存在，直接按本文创建全量 schema。
- 不支持对旧 prototype schema 自动 `ALTER TABLE` 兜底。
- 初始化入口为 `hikbox_pictures.product.config.initialize_workspace`。
- 初始化 SQL 固定为：
  - `hikbox_pictures/product/db/sql/library_v1.sql`
  - `hikbox_pictures/product/db/sql/embedding_v1.sql`
- scan 输入阶段依赖表（`scan_session_source`、`photo_asset`）由 `library_v1.sql` 在初始化时一次性建齐；运行时阶段代码不再隐式建表。
- 初始化策略为“幂等建表 + 固定元信息 upsert”：每次初始化都会写入（或刷新）以下键值：
  - `schema_meta.schema_version = '1'`
  - `schema_meta.product_schema_name = 'people_gallery_v1'`
  - `embedding_meta.schema_version = '1'`
  - `embedding_meta.vector_dim = '512'`
  - `embedding_meta.vector_dtype = 'float32'`

### 3.2 后续版本（schema_version>=2）

- 通过显式 migration 执行 schema 演进。
- 每次 migration 必须：
  1. 升级 `schema_meta.schema_version`。
  2. 记录 `schema_meta.last_migration`。
  3. 同步更新本文档。

## 4. `library.db` 结构

### 4.1 元信息

#### `schema_meta`

| 字段 | 类型 | 约束 | 说明 |
| --- | --- | --- | --- |
| `key` | `TEXT` | `PRIMARY KEY` | 元数据键 |
| `value` | `TEXT` | `NOT NULL` | JSON 编码值 |
| `updated_at` | `TEXT` | `NOT NULL` | 更新时间 |

首版必须包含：

- `schema_version` = `1`
- `product_schema_name` = `people_gallery_v1`

### 4.2 资产与扫描

#### `library_source`

| 字段 | 类型 | 约束 | 说明 |
| --- | --- | --- | --- |
| `id` | `INTEGER` | `PRIMARY KEY` | 源目录 ID |
| `root_path` | `TEXT` | `NOT NULL UNIQUE` | 源目录绝对路径 |
| `label` | `TEXT` | `NOT NULL` | 展示名 |
| `enabled` | `INTEGER` | `NOT NULL DEFAULT 1 CHECK (enabled IN (0,1))` | 是否启用 |
| `removed_at` | `TEXT` |  | 软删除时间（非空表示已删除） |
| `last_discovered_at` | `TEXT` |  | 最近 discover 完成时间 |
| `created_at` | `TEXT` | `NOT NULL` | 创建时间 |
| `updated_at` | `TEXT` | `NOT NULL` | 更新时间 |

索引：

- `idx_library_source_enabled(enabled)`

规则：

- `root_path` 全局唯一（包含已软删除记录）；同一路径不可重复添加。
- 删除 source 走软删除：写入 `removed_at` 且强制 `enabled=0`，不做物理删除。

#### `scan_session`

| 字段 | 类型 | 约束 | 说明 |
| --- | --- | --- | --- |
| `id` | `INTEGER` | `PRIMARY KEY` | 会话 ID |
| `run_kind` | `TEXT` | `NOT NULL CHECK (run_kind IN ('scan_full','scan_incremental','scan_resume'))` | 运行类型 |
| `status` | `TEXT` | `NOT NULL CHECK (status IN ('pending','running','aborting','interrupted','completed','abandoned','failed'))` | 会话状态 |
| `triggered_by` | `TEXT` | `NOT NULL CHECK (triggered_by IN ('manual_webui','manual_cli'))` | 触发来源 |
| `resume_from_session_id` | `INTEGER` | `REFERENCES scan_session(id)` | 恢复来源会话 |
| `started_at` | `TEXT` |  | 启动时间 |
| `finished_at` | `TEXT` |  | 完成/终止时间 |
| `last_error` | `TEXT` |  | 最近错误 |
| `created_at` | `TEXT` | `NOT NULL` | 创建时间 |
| `updated_at` | `TEXT` | `NOT NULL` | 更新时间 |

索引：

- `idx_scan_session_status(status)`
- `idx_scan_session_created_at(created_at)`
- `uniq_scan_session_single_active((1)) WHERE status IN ('running','aborting')`

规则：

- `pending_reassign` 不对应独立 `reassign` 会话类型；其处理并入常规 `scan_*` 会话。
- 通过部分唯一索引保证全局单活：任意时刻最多只有一个 `running/aborting` 会话。
- `scan_incremental` 优先只处理当前会话 source 范围内两类 active observation：`pending_reassign=1` 的 observation，以及“在本会话创建后新出现且仍无 active assignment”的 observation；历史遗留的 unassigned face 不会仅因再次落在当前 source 范围内就自动进入本轮 incremental candidate。
- 若参数快照与最近完成的持久 cluster 快照不一致，`scan_incremental` 会自动回退到**真正的 full rebuild**：assignment 输入改为全库 `asset_status='active'` 的 active face，并执行与 `scan_full` 等价的“停用旧 active assignment + 退役未复用 person + 重写 active cluster 快照”语义，而不是仅跳过增量服务。
- assignment 输入始终要求 `photo_asset.asset_status='active'`；discover/metadata 已标记为 `missing/deleted` 的资产，其历史 face 不得再进入 assignment 输入。
- full rebuild（包括由 `scan_incremental` 回退得到的 full rebuild）完成后，本次处理过的 active face 必须清空 `pending_reassign`，避免在后续 incremental session 中被重复选为 candidate。

#### `scan_session_source`

| 字段 | 类型 | 约束 | 说明 |
| --- | --- | --- | --- |
| `id` | `INTEGER` | `PRIMARY KEY` | 主键 |
| `scan_session_id` | `INTEGER` | `NOT NULL REFERENCES scan_session(id)` | 所属会话 |
| `library_source_id` | `INTEGER` | `NOT NULL REFERENCES library_source(id)` | 所属源目录 |
| `stage_status_json` | `TEXT` | `NOT NULL` | 各阶段状态摘要 |
| `processed_assets` | `INTEGER` | `NOT NULL DEFAULT 0` | 已处理资产数 |
| `failed_assets` | `INTEGER` | `NOT NULL DEFAULT 0` | 失败资产数 |
| `updated_at` | `TEXT` | `NOT NULL` | 更新时间 |

约束与索引：

- `UNIQUE(scan_session_id, library_source_id)`
- `idx_scan_session_source_session(scan_session_id)`

规则：

- discover/metadata 进度按 `library_source_id` 独立维护，`stage_status_json` 至少包含 `discover` 与 `metadata` 两个键。
- discover 在同一 `source + primary_path` 命中旧记录时，若 `file_size` 或 `mtime_ns` 任一变化（`old.file_size != new.file_size or old.mtime_ns != new.mtime_ns`），则该 source 需要触发后续阶段重跑。
- discover/metadata 对单资产异常（如 `OSError`、解析失败）按资产级容错：不中断整个 source，累计 `failed_assets` 并回写到 `scan_session_source`。

#### `scan_checkpoint`

| 字段 | 类型 | 约束 | 说明 |
| --- | --- | --- | --- |
| `id` | `INTEGER` | `PRIMARY KEY` | 主键 |
| `scan_session_id` | `INTEGER` | `NOT NULL REFERENCES scan_session(id)` | 会话 ID |
| `stage` | `TEXT` | `NOT NULL CHECK (stage IN ('discover','metadata','detect','embed','cluster','assignment'))` | 阶段 |
| `cursor_json` | `TEXT` | `NOT NULL` | 断点游标 |
| `processed_count` | `INTEGER` | `NOT NULL DEFAULT 0` | 阶段累计处理数 |
| `updated_at` | `TEXT` | `NOT NULL` | 更新时间 |

约束与索引：

- `UNIQUE(scan_session_id, stage)`

#### `scan_batch`

| 字段 | 类型 | 约束 | 说明 |
| --- | --- | --- | --- |
| `id` | `INTEGER` | `PRIMARY KEY` | 批次 ID |
| `scan_session_id` | `INTEGER` | `NOT NULL REFERENCES scan_session(id)` | 会话 ID |
| `stage` | `TEXT` | `NOT NULL CHECK (stage='detect')` | 阶段（固定为 `detect`） |
| `worker_slot` | `INTEGER` | `NOT NULL` | worker 槽位 |
| `claim_token` | `TEXT` | `NOT NULL UNIQUE` | claim token |
| `status` | `TEXT` | `NOT NULL CHECK (status IN ('claimed','running','acked','failed'))` | 批次状态 |
| `retry_count` | `INTEGER` | `NOT NULL DEFAULT 0` | 重试次数 |
| `claimed_at` | `TEXT` | `NOT NULL` | claim 时间 |
| `started_at` | `TEXT` |  | 子进程开始时间 |
| `acked_at` | `TEXT` |  | ack 时间 |
| `error_message` | `TEXT` |  | 错误信息 |

索引：

- `idx_scan_batch_session(scan_session_id)`
- `idx_scan_batch_status(status)`

规则：

- `scan_batch` 仅用于 `detect + aligned + crop/context` 产物阶段；其他阶段不走 claim/ack。
- `claim_token` 由主进程在 claim 时校验；只有 `status='running'` 且 token 匹配的批次允许 ack。
- `aborting` 场景下，未 ack 的 `claimed/running` 批次必须回退并标记失败，`scan_session` 迁移为 `interrupted`。

#### `scan_batch_item`

| 字段 | 类型 | 约束 | 说明 |
| --- | --- | --- | --- |
| `id` | `INTEGER` | `PRIMARY KEY` | 主键 |
| `scan_batch_id` | `INTEGER` | `NOT NULL REFERENCES scan_batch(id)` | 批次 ID |
| `photo_asset_id` | `INTEGER` | `NOT NULL REFERENCES photo_asset(id)` | 资产 ID |
| `item_order` | `INTEGER` | `NOT NULL` | 批次内序号 |
| `status` | `TEXT` | `NOT NULL CHECK (status IN ('pending','running','done','failed'))` | 条目状态 |
| `error_message` | `TEXT` |  | 错误 |
| `updated_at` | `TEXT` | `NOT NULL` | 更新时间 |

约束与索引：

- `UNIQUE(scan_batch_id, item_order)`
- `idx_scan_batch_item_asset(photo_asset_id)`

#### `photo_asset`

| 字段 | 类型 | 约束 | 说明 |
| --- | --- | --- | --- |
| `id` | `INTEGER` | `PRIMARY KEY` | 资产 ID |
| `library_source_id` | `INTEGER` | `NOT NULL REFERENCES library_source(id)` | 来源 |
| `primary_path` | `TEXT` | `NOT NULL` | source 内相对路径 |
| `primary_fingerprint` | `TEXT` | `NOT NULL` | sha256 指纹 |
| `fingerprint_algo` | `TEXT` | `NOT NULL CHECK (fingerprint_algo='sha256')` | 指纹算法 |
| `file_size` | `INTEGER` | `NOT NULL` | 文件大小 |
| `mtime_ns` | `INTEGER` | `NOT NULL` | mtime(ns) |
| `capture_datetime` | `TEXT` |  | 拍摄时间 |
| `capture_month` | `TEXT` |  | `YYYY-MM` |
| `is_live_photo` | `INTEGER` | `NOT NULL DEFAULT 0 CHECK (is_live_photo IN (0,1))` | 是否 Live |
| `live_mov_path` | `TEXT` |  | 配对 MOV 相对路径 |
| `live_mov_size` | `INTEGER` |  | MOV 文件大小 |
| `live_mov_mtime_ns` | `INTEGER` |  | MOV mtime(ns) |
| `asset_status` | `TEXT` | `NOT NULL DEFAULT 'active' CHECK (asset_status IN ('active','deleted','missing'))` | 资产状态 |
| `created_at` | `TEXT` | `NOT NULL` | 创建时间 |
| `updated_at` | `TEXT` | `NOT NULL` | 更新时间 |

约束与索引：

- `UNIQUE(library_source_id, primary_path)`
- `idx_photo_asset_fingerprint(primary_fingerprint)`
- `idx_photo_asset_capture_month(capture_month)`

规则：

- Live Photo 配对在扫描 `metadata` 阶段完成，结果写入 `live_mov_path/live_mov_size/live_mov_mtime_ns`。
- Live Photo 仅对 `HEIC/HEIF` 静态图尝试配对，支持隐藏 MOV 命名：`.<still_name>_<token>.MOV` 与 `.<still_stem>_<token>.MOV`。
- 匹配到多个 MOV 候选时，按 `token` 降序优先；`token` 相同按 `live_mov_mtime_ns` 降序选择。
- `capture_datetime` 以 ISO8601（含时区 offset）持久化；时间解析优先级为 `DateTimeOriginal > DateTimeDigitized > DateTime > birthtime > mtime`。
- 导出阶段仅消费已落库的 `live_mov_*` 字段，不再做实时目录配对；缺失时静默跳过 `live_mov` 导出。

#### `face_observation`

| 字段 | 类型 | 约束 | 说明 |
| --- | --- | --- | --- |
| `id` | `INTEGER` | `PRIMARY KEY` | observation ID |
| `photo_asset_id` | `INTEGER` | `NOT NULL REFERENCES photo_asset(id)` | 资产 ID |
| `face_index` | `INTEGER` | `NOT NULL` | 图内人脸序号 |
| `crop_relpath` | `TEXT` | `NOT NULL` | crop 路径 |
| `aligned_relpath` | `TEXT` | `NOT NULL` | aligned 路径 |
| `context_relpath` | `TEXT` | `NOT NULL` | context 路径 |
| `bbox_x1` | `REAL` | `NOT NULL` | 检测框左上 x（原图像素坐标） |
| `bbox_y1` | `REAL` | `NOT NULL` | 检测框左上 y（原图像素坐标） |
| `bbox_x2` | `REAL` | `NOT NULL` | 检测框右下 x（原图像素坐标） |
| `bbox_y2` | `REAL` | `NOT NULL` | 检测框右下 y（原图像素坐标） |
| `detector_confidence` | `REAL` | `NOT NULL` | 检测置信度 |
| `face_area_ratio` | `REAL` | `NOT NULL` | 人脸面积比 |
| `magface_quality` | `REAL` | `NOT NULL` | MagFace 质量 |
| `quality_score` | `REAL` | `NOT NULL` | 综合质量分 |
| `active` | `INTEGER` | `NOT NULL DEFAULT 1 CHECK (active IN (0,1))` | 是否有效 |
| `inactive_reason` | `TEXT` | `CHECK (inactive_reason IN ('asset_deleted','re_detect_replaced','manual_drop') OR inactive_reason IS NULL)` | 失效原因 |
| `pending_reassign` | `INTEGER` | `NOT NULL DEFAULT 0 CHECK (pending_reassign IN (0,1))` | 待再归属标记 |
| `created_at` | `TEXT` | `NOT NULL` | 创建时间 |
| `updated_at` | `TEXT` | `NOT NULL` | 更新时间 |

约束与索引：

- `UNIQUE(photo_asset_id, face_index)`
- `CHECK (bbox_x2 > bbox_x1 AND bbox_y2 > bbox_y1)`
- `idx_face_observation_asset(photo_asset_id)`
- `idx_face_observation_pending_reassign(pending_reassign)`

规则：

- `ack_detect_batch` 必须消费 worker payload 中的 `faces` 明细并写入 `face_observation`；禁止“无 payload 自动 done”。
- detect 产物（`crop/aligned/context`）采用临时文件写入后 `tmp_path.replace(final_path)` 原子落盘，避免半写入状态。

### 4.3 人物、归属、排除

#### `person`

| 字段 | 类型 | 约束 | 说明 |
| --- | --- | --- | --- |
| `id` | `INTEGER` | `PRIMARY KEY` | 人物 ID |
| `person_uuid` | `TEXT` | `NOT NULL UNIQUE` | 稳定 UUIDv4 |
| `display_name` | `TEXT` |  | 人物名称 |
| `is_named` | `INTEGER` | `NOT NULL DEFAULT 0 CHECK (is_named IN (0,1))` | 是否已命名 |
| `status` | `TEXT` | `NOT NULL CHECK (status IN ('active','merged'))` | 人物状态 |
| `merged_into_person_id` | `INTEGER` | `REFERENCES person(id)` | 被合并目标 |
| `created_at` | `TEXT` | `NOT NULL` | 创建时间 |
| `updated_at` | `TEXT` | `NOT NULL` | 更新时间 |

规则：

- `person_uuid` 在人物创建时生成并固定。
- 合并后保留样本数（active assignment 数）更大的人物的 `person_uuid`。
- 样本数相同则保留 `merge-batch.selected_person_ids[0]` 的 `person_uuid`。

#### `assignment_run`

| 字段 | 类型 | 约束 | 说明 |
| --- | --- | --- | --- |
| `id` | `INTEGER` | `PRIMARY KEY` | run ID |
| `scan_session_id` | `INTEGER` | `NOT NULL REFERENCES scan_session(id)` | 来源扫描会话 |
| `algorithm_version` | `TEXT` | `NOT NULL` | 算法版本 |
| `param_snapshot_json` | `TEXT` | `NOT NULL` | 参数快照 |
| `run_kind` | `TEXT` | `NOT NULL CHECK (run_kind IN ('scan_full','scan_incremental','scan_resume'))` | 运行类型 |
| `started_at` | `TEXT` | `NOT NULL` | 开始时间 |
| `finished_at` | `TEXT` |  | 结束时间 |
| `status` | `TEXT` | `NOT NULL CHECK (status IN ('running','completed','failed'))` | 状态 |
| `updated_at` | `TEXT` | `NOT NULL` | 更新时间 |

索引：

- `idx_assignment_run_started_at(started_at)`
- `idx_assignment_run_scan_session(scan_session_id, started_at)`

规则：

- 若扫描在 assignment 阶段被用户中止，运行中的 `assignment_run.status` 记为 `failed`（原因由 `last_error`/事件日志记录）。
- `param_snapshot_json` 必须完整覆盖冻结参数（spec §7.2），包含：
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
- `param_snapshot_json` 中不允许出现 `embedding_flip_weight`。
- `run_kind='scan_incremental'` 时会优先复用持久 cluster；只有在无快照或参数快照不匹配时才回退全量重建。
- 回退 full rebuild 时，输入范围提升为全库 `asset_status='active'` 的 active face，效果必须与 `scan_full` 一致，而不是只重跑当前 session source。
- 回退 full rebuild 时，对应 `assignment_run.run_kind` 也必须写成 `scan_full`，保证运行元数据与实际执行语义一致。

#### `face_cluster`

| 字段 | 类型 | 约束 | 说明 |
| --- | --- | --- | --- |
| `id` | `INTEGER` | `PRIMARY KEY` | 持久 cluster 主键 |
| `cluster_uuid` | `TEXT` | `NOT NULL UNIQUE` | cluster 稳定 UUID |
| `person_id` | `INTEGER` | `NOT NULL REFERENCES person(id)` | 当前归属人物 |
| `status` | `TEXT` | `NOT NULL CHECK (status IN ('active','replaced'))` | 当前快照状态 |
| `rebuild_scope` | `TEXT` | `NOT NULL CHECK (rebuild_scope IN ('full','local'))` | 生成该快照的重建范围 |
| `created_assignment_run_id` | `INTEGER` | `NOT NULL REFERENCES assignment_run(id)` | 首次生成该 cluster 的 assignment run |
| `updated_assignment_run_id` | `INTEGER` | `NOT NULL REFERENCES assignment_run(id)` | 最近一次刷新该 cluster 的 assignment run |
| `created_at` | `TEXT` | `NOT NULL` | 创建时间 |
| `updated_at` | `TEXT` | `NOT NULL` | 更新时间 |

约束与索引：

- `idx_face_cluster_status(status, person_id)`。
- `status='active'` 的行代表当前 cluster 真相层；全量重建会尽量复用“同一 person 且成员集合不变”的 cluster 行，保留其稳定 `cluster_uuid`；只有未复用的旧 cluster 才会被置为 `replaced`。
- full rebuild 判定 person/cluster 是否可复用时，签名只统计仍然有效的证据：`person_face_assignment.active=1` 且对应 `face_observation.active=1`、`photo_asset.asset_status='active'`。
- `rebuild_scope='local'` 表示该 cluster 在增量阶段通过局部重建或局部追加刷新，而不是整库全量重建。
- 增量 attach 采用“两阶段”判定：先仅用 `face_cluster_rep_face` 做 representative 召回候选，再只对召回到的 cluster 使用 `face_cluster_member` 做 member 精排。
- 上述 representative/member 证据在增量路径中都必须再联表过滤 `photo_asset.asset_status='active'`；来自 `missing/deleted` 资产的历史 face 不能参与 recall、rerank 或 local rebuild。

#### `face_cluster_member`

| 字段 | 类型 | 约束 | 说明 |
| --- | --- | --- | --- |
| `id` | `INTEGER` | `PRIMARY KEY` | 主键 |
| `face_cluster_id` | `INTEGER` | `NOT NULL REFERENCES face_cluster(id)` | 所属持久 cluster |
| `face_observation_id` | `INTEGER` | `NOT NULL REFERENCES face_observation(id)` | 成员 observation |
| `assignment_run_id` | `INTEGER` | `NOT NULL REFERENCES assignment_run(id)` | 写入该成员关系的 assignment run |
| `created_at` | `TEXT` | `NOT NULL` | 创建时间 |

约束与索引：

- `UNIQUE(face_cluster_id, face_observation_id)`。
- `idx_face_cluster_member_face(face_observation_id)`。
- 活跃 cluster 的全量成员关系由 `face_cluster.status='active'` + 本表联表得到。

#### `face_cluster_rep_face`

| 字段 | 类型 | 约束 | 说明 |
| --- | --- | --- | --- |
| `id` | `INTEGER` | `PRIMARY KEY` | 主键 |
| `face_cluster_id` | `INTEGER` | `NOT NULL REFERENCES face_cluster(id)` | 所属持久 cluster |
| `face_observation_id` | `INTEGER` | `NOT NULL REFERENCES face_observation(id)` | representative observation |
| `rep_rank` | `INTEGER` | `NOT NULL` | 代表脸顺位，`1` 为最优 |
| `assignment_run_id` | `INTEGER` | `NOT NULL REFERENCES assignment_run(id)` | 写入该 representative 的 assignment run |
| `created_at` | `TEXT` | `NOT NULL` | 创建时间 |

约束与索引：

- `UNIQUE(face_cluster_id, rep_rank)`。
- `idx_face_cluster_rep_face_obs(face_observation_id)`。
- representative face 默认按 cluster 内 `quality_score` 从高到低选取前 `3` 张。
- 当已存 representative 因 `face_observation.active=0` 或其 `photo_asset.asset_status!='active'` 失效时，需要从仍然有效的 cluster members 中重新选 representative，避免 cluster 因坏 rep 而失去召回能力。
- representative 只承担第一阶段召回，不参与直接硬挂；是否 attach 由召回后的 member 精排结果决定。

#### `person_face_assignment`

| 字段 | 类型 | 约束 | 说明 |
| --- | --- | --- | --- |
| `id` | `INTEGER` | `PRIMARY KEY` | 主键 |
| `person_id` | `INTEGER` | `NOT NULL REFERENCES person(id)` | 归属人物 |
| `face_observation_id` | `INTEGER` | `NOT NULL REFERENCES face_observation(id)` | observation |
| `assignment_run_id` | `INTEGER` | `NOT NULL REFERENCES assignment_run(id)` | run |
| `assignment_source` | `TEXT` | `NOT NULL CHECK (assignment_source IN ('hdbscan','person_consensus','merge','undo'))` | 来源 |
| `active` | `INTEGER` | `NOT NULL DEFAULT 1 CHECK (active IN (0,1))` | 是否生效 |
| `confidence` | `REAL` |  | 置信度 |
| `margin` | `REAL` |  | margin |
| `created_at` | `TEXT` | `NOT NULL` | 创建时间 |
| `updated_at` | `TEXT` | `NOT NULL` | 更新时间 |

约束与索引：

- 自动来源：`hdbscan|person_consensus`；不存在 `manual`。
- `noise` 与 `low_quality_ignored` 不写入 `person_face_assignment`。
- 允许值仅 `hdbscan|person_consensus|merge|undo`。
- 若增量 `local_rebuild` 后仍无法唯一映射回某个既有 cluster（如 overlap 并列），该 face 必须保持未归属，禁止硬挂到任一 person；后续可等待下一次 full rebuild 统一处理。
- partial unique：`UNIQUE(face_observation_id) WHERE active=1`。
- `idx_assignment_person(person_id, active)`。
- `idx_assignment_run(assignment_run_id)`。
- 增量 attach 复用已有持久 cluster / person 时，来源仍记为 `person_consensus`，不会引入新的 assignment_source 枚举。
- 边界不稳定的新 observation 不做硬挂；会先尝试 local rebuild，若仍无法稳定归属，则保持未归属，等待后续增量或全量重建。

#### `person_face_exclusion`

| 字段 | 类型 | 约束 | 说明 |
| --- | --- | --- | --- |
| `id` | `INTEGER` | `PRIMARY KEY` | 主键 |
| `person_id` | `INTEGER` | `NOT NULL REFERENCES person(id)` | 人物 ID |
| `face_observation_id` | `INTEGER` | `NOT NULL REFERENCES face_observation(id)` | observation |
| `reason` | `TEXT` | `NOT NULL CHECK (reason='manual_exclude')` | 排除原因 |
| `active` | `INTEGER` | `NOT NULL DEFAULT 1 CHECK (active IN (0,1))` | 是否生效 |
| `created_at` | `TEXT` | `NOT NULL` | 创建时间 |
| `updated_at` | `TEXT` | `NOT NULL` | 更新时间 |

约束与索引：

- partial unique：`UNIQUE(person_id, face_observation_id) WHERE active=1`。
- `idx_exclusion_face(face_observation_id, active)`。

### 4.4 合并与撤销

#### `merge_operation`

| 字段 | 类型 | 约束 | 说明 |
| --- | --- | --- | --- |
| `id` | `INTEGER` | `PRIMARY KEY` | 合并操作 ID |
| `selected_person_ids_json` | `TEXT` | `NOT NULL` | 本次多选人物列表 |
| `winner_person_id` | `INTEGER` | `NOT NULL REFERENCES person(id)` | 保留人物 |
| `winner_person_uuid` | `TEXT` | `NOT NULL` | 保留 UUID |
| `status` | `TEXT` | `NOT NULL CHECK (status IN ('applied','undone'))` | 操作状态 |
| `created_at` | `TEXT` | `NOT NULL` | 创建时间 |
| `undone_at` | `TEXT` |  | 撤销时间 |

#### `merge_operation_person_delta`

| 字段 | 类型 | 约束 | 说明 |
| --- | --- | --- | --- |
| `id` | `INTEGER` | `PRIMARY KEY` | 主键 |
| `merge_operation_id` | `INTEGER` | `NOT NULL REFERENCES merge_operation(id)` | 所属合并 |
| `person_id` | `INTEGER` | `NOT NULL REFERENCES person(id)` | 人物 ID |
| `before_snapshot_json` | `TEXT` | `NOT NULL` | 合并前快照 |
| `after_snapshot_json` | `TEXT` | `NOT NULL` | 合并后快照 |

#### `merge_operation_assignment_delta`

| 字段 | 类型 | 约束 | 说明 |
| --- | --- | --- | --- |
| `id` | `INTEGER` | `PRIMARY KEY` | 主键 |
| `merge_operation_id` | `INTEGER` | `NOT NULL REFERENCES merge_operation(id)` | 所属合并 |
| `face_observation_id` | `INTEGER` | `NOT NULL REFERENCES face_observation(id)` | observation |
| `before_assignment_json` | `TEXT` | `NOT NULL` | 合并前归属快照 |
| `after_assignment_json` | `TEXT` | `NOT NULL` | 合并后归属快照 |

#### `merge_operation_exclusion_delta`

| 字段 | 类型 | 约束 | 说明 |
| --- | --- | --- | --- |
| `id` | `INTEGER` | `PRIMARY KEY` | 主键 |
| `merge_operation_id` | `INTEGER` | `NOT NULL REFERENCES merge_operation(id)` | 所属合并 |
| `person_id` | `INTEGER` | `NOT NULL REFERENCES person(id)` | 人物 ID |
| `face_observation_id` | `INTEGER` | `NOT NULL REFERENCES face_observation(id)` | observation |
| `before_exclusion_json` | `TEXT` | `NOT NULL` | 合并前排除快照 |
| `after_exclusion_json` | `TEXT` | `NOT NULL` | 合并后排除快照 |

规则：

- 仅允许撤销“全局最近一次且未撤销”的 `merge_operation`。
- 合并时需将 loser 人物上的 active exclusion 迁移到 winner，并记录 `merge_operation_exclusion_delta`。
- 撤销最近一次合并时，按 `merge_operation_exclusion_delta` 回滚本次排除迁移。

### 4.5 导出账本

#### `export_template`

| 字段 | 类型 | 约束 | 说明 |
| --- | --- | --- | --- |
| `id` | `INTEGER` | `PRIMARY KEY` | 模板 ID |
| `name` | `TEXT` | `NOT NULL` | 模板名 |
| `output_root` | `TEXT` | `NOT NULL` | 目标绝对路径 |
| `enabled` | `INTEGER` | `NOT NULL DEFAULT 1 CHECK (enabled IN (0,1))` | 是否启用 |
| `created_at` | `TEXT` | `NOT NULL` | 创建时间 |
| `updated_at` | `TEXT` | `NOT NULL` | 更新时间 |

规则：

- 首版不提供模板删除 API/CLI（`enabled` 字段仅预留，不承诺删除工作流）。

#### `export_template_person`

| 字段 | 类型 | 约束 | 说明 |
| --- | --- | --- | --- |
| `id` | `INTEGER` | `PRIMARY KEY` | 主键 |
| `template_id` | `INTEGER` | `NOT NULL REFERENCES export_template(id)` | 模板 |
| `person_id` | `INTEGER` | `NOT NULL REFERENCES person(id)` | 人物 |
| `created_at` | `TEXT` | `NOT NULL` | 创建时间 |

约束与索引：

- `UNIQUE(template_id, person_id)`
- 应用层校验：仅允许 `person.is_named=1 AND person.status='active'`。

#### `export_run`

| 字段 | 类型 | 约束 | 说明 |
| --- | --- | --- | --- |
| `id` | `INTEGER` | `PRIMARY KEY` | 导出运行 ID |
| `template_id` | `INTEGER` | `NOT NULL REFERENCES export_template(id)` | 模板 |
| `status` | `TEXT` | `NOT NULL CHECK (status IN ('running','completed','failed','aborted'))` | 运行状态 |
| `summary_json` | `TEXT` | `NOT NULL` | 汇总信息 |
| `started_at` | `TEXT` | `NOT NULL` | 开始时间 |
| `finished_at` | `TEXT` |  | 结束时间 |

索引：

- `idx_export_run_status(status)`
- `idx_export_run_template(template_id, started_at)`

#### `export_delivery`

| 字段 | 类型 | 约束 | 说明 |
| --- | --- | --- | --- |
| `id` | `INTEGER` | `PRIMARY KEY` | 主键 |
| `export_run_id` | `INTEGER` | `NOT NULL REFERENCES export_run(id)` | 导出运行 |
| `photo_asset_id` | `INTEGER` | `NOT NULL REFERENCES photo_asset(id)` | 资产 |
| `media_kind` | `TEXT` | `NOT NULL CHECK (media_kind IN ('photo','live_mov'))` | 媒体类型 |
| `bucket` | `TEXT` | `NOT NULL CHECK (bucket IN ('only','group'))` | 分桶 |
| `month_key` | `TEXT` | `NOT NULL` | `YYYY-MM` |
| `destination_path` | `TEXT` | `NOT NULL` | 目标绝对路径 |
| `delivery_status` | `TEXT` | `NOT NULL CHECK (delivery_status IN ('exported','skipped_exists','failed'))` | 投递结果 |
| `error_message` | `TEXT` |  | 错误信息 |
| `created_at` | `TEXT` | `NOT NULL` | 记录时间 |

约束与索引：

- `UNIQUE(export_run_id, media_kind, destination_path)`
- `idx_export_delivery_status(delivery_status)`

规则：

- 当目标路径已存在同名文件，写 `delivery_status='skipped_exists'`，不覆盖、不重命名。

### 4.6 可观测与审计

#### `ops_event`

| 字段 | 类型 | 约束 | 说明 |
| --- | --- | --- | --- |
| `id` | `INTEGER` | `PRIMARY KEY` | 事件 ID |
| `event_type` | `TEXT` | `NOT NULL` | 事件类型 |
| `severity` | `TEXT` | `NOT NULL CHECK (severity IN ('info','warning','error'))` | 级别 |
| `scan_session_id` | `INTEGER` | `REFERENCES scan_session(id)` | 关联扫描 |
| `export_run_id` | `INTEGER` | `REFERENCES export_run(id)` | 关联导出 |
| `payload_json` | `TEXT` | `NOT NULL` | 事件详情 |
| `created_at` | `TEXT` | `NOT NULL` | 创建时间 |

索引：

- `idx_ops_event_type_created(event_type, created_at)`
- `idx_ops_event_scan(scan_session_id)`

#### `scan_audit_item`

| 字段 | 类型 | 约束 | 说明 |
| --- | --- | --- | --- |
| `id` | `INTEGER` | `PRIMARY KEY` | 主键 |
| `scan_session_id` | `INTEGER` | `NOT NULL REFERENCES scan_session(id)` | 扫描会话 |
| `assignment_run_id` | `INTEGER` | `NOT NULL REFERENCES assignment_run(id)` | 归属 run |
| `audit_type` | `TEXT` | `NOT NULL CHECK (audit_type IN ('low_margin_auto_assign','reassign_after_exclusion','new_anonymous_person'))` | 审计类型 |
| `face_observation_id` | `INTEGER` | `NOT NULL REFERENCES face_observation(id)` | observation |
| `person_id` | `INTEGER` | `REFERENCES person(id)` | 人物（可空） |
| `evidence_json` | `TEXT` | `NOT NULL` | 证据 |
| `created_at` | `TEXT` | `NOT NULL` | 创建时间 |

索引：

- `idx_scan_audit_session(scan_session_id, audit_type)`

## 5. `embedding.db` 结构

### 5.1 元信息

#### `embedding_meta`

| 字段 | 类型 | 约束 | 说明 |
| --- | --- | --- | --- |
| `key` | `TEXT` | `PRIMARY KEY` | 元数据键 |
| `value` | `TEXT` | `NOT NULL` | 值 |
| `updated_at` | `TEXT` | `NOT NULL` | 更新时间 |

首版必须包含：

- `schema_version` = `1`
- `vector_dim` = `512`
- `vector_dtype` = `float32`

### 5.2 向量表

#### `face_embedding`

| 字段 | 类型 | 约束 | 说明 |
| --- | --- | --- | --- |
| `id` | `INTEGER` | `PRIMARY KEY` | 主键 |
| `face_observation_id` | `INTEGER` | `NOT NULL` | observation ID（逻辑外键） |
| `feature_type` | `TEXT` | `NOT NULL CHECK (feature_type='face')` | 特征类型 |
| `model_key` | `TEXT` | `NOT NULL` | 模型标识 |
| `variant` | `TEXT` | `NOT NULL CHECK (variant IN ('main','flip'))` | 变体 |
| `dim` | `INTEGER` | `NOT NULL CHECK (dim=512)` | 向量维度 |
| `dtype` | `TEXT` | `NOT NULL CHECK (dtype='float32')` | 数据类型 |
| `vector_blob` | `BLOB` | `NOT NULL` | 向量二进制 |
| `created_at` | `TEXT` | `NOT NULL` | 创建时间 |

约束与索引：

- `UNIQUE(face_observation_id, feature_type, model_key, variant)`
- `idx_face_embedding_observation(face_observation_id)`

## 6. 跨库一致性规则

- 写入顺序：先写 `library.db` 的 `face_observation`，再写 `embedding.db.face_embedding`。
- 删除/失效 observation 时，应用层负责清理 `embedding.db` 对应向量。
- 扫描和归属阶段由主进程单写，避免双库并发写冲突。
- 不使用跨库外键；一致性靠事务编排和补偿任务保证。

## 7. 运行时锁定规则（与 schema 相关）

- 任意时刻仅允许一个 active scan（`running|aborting`）。
- 导出运行（`export_run.status='running'`）期间，禁止人物归属/合并写操作。
- 同一 observation 仅允许一个 active assignment（partial unique index）。
- 同一 `(person_id, face_observation_id)` 仅允许一个 active exclusion（partial unique index）。

## 8. 兼容性说明

- 本文档替代旧 `pipeline.db` 说明，不再描述旧表 `pipeline_sources/source_images/detected_faces/pipeline_meta`。
- 首版发布后，任何 schema 变更必须走 migration，并同步维护本文档。
