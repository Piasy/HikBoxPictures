# HikBoxPictures 当前数据库 Schema

## 文档定位

- 本文记录当前仓库 `src/hikbox_pictures/db/migrations/` 已落地 migration 链对应的最新数据库 schema 快照。
- 当前最新 migration 为 `0004_identity_rebuild_v3_schema.sql`。
- 数据库类型为 SQLite。
- 本文只描述已经落地的表、字段、索引和约束，不包含设计稿或讨论中的未来变更。

## Schema 概览

- 当前共有 22 张表。
- 布尔语义统一使用 `INTEGER`，取值为 `0` 或 `1`。
- 大部分时间字段使用 `TEXT` 存储，并以 `CURRENT_TIMESTAMP` 作为默认值。
- `cursor_json`、`payload_json`、`detail_json` 等字段在库中以 JSON 字符串形式存储。

| 领域 | 表 |
| --- | --- |
| 来源与扫描 | `library_source`、`scan_session`、`scan_session_source`、`scan_checkpoint`、`photo_asset` |
| 人脸观测与聚类 | `face_observation`、`face_embedding`、`auto_cluster_batch`、`auto_cluster`、`auto_cluster_member` |
| 人物与复核 | `identity_threshold_profile`、`person`、`person_face_assignment`、`person_face_exclusion`、`person_trusted_sample`、`person_prototype`、`review_item` |
| 导出与运维 | `export_template`、`export_template_person`、`export_run`、`export_delivery`、`ops_event` |

## 当前表结构

### `library_source`

用途：已接入图库来源的注册表。

| 字段 | 类型 | 约束 / 默认值 | 含义 |
| --- | --- | --- | --- |
| `id` | `INTEGER` | 主键，自增 | 来源主键。 |
| `name` | `TEXT` | 非空 | 来源显示名称。 |
| `root_path` | `TEXT` | 非空 | 来源根目录绝对路径。 |
| `root_fingerprint` | `TEXT` | 可空 | 根目录指纹，用于识别来源是否变化。 |
| `active` | `INTEGER` | 非空，默认 `1`，`0/1` | 来源是否仍然启用。 |
| `created_at` | `TEXT` | 非空，默认 `CURRENT_TIMESTAMP` | 创建时间。 |
| `updated_at` | `TEXT` | 非空，默认 `CURRENT_TIMESTAMP` | 最近更新时间。 |

关键索引与约束：

- `uq_library_source_root_path_active`：`active = 1` 的记录中，`root_path` 必须唯一。

### `scan_session`

用途：一次全量、增量或恢复扫描任务的总记录。

| 字段 | 类型 | 约束 / 默认值 | 含义 |
| --- | --- | --- | --- |
| `id` | `INTEGER` | 主键，自增 | 扫描会话主键。 |
| `mode` | `TEXT` | 非空，枚举 `initial` / `incremental` / `resume` | 扫描模式。 |
| `status` | `TEXT` | 非空，枚举 `pending` / `running` / `paused` / `interrupted` / `completed` / `failed` / `abandoned` | 会话生命周期状态。 |
| `resume_from_session_id` | `INTEGER` | 可空，外键到 `scan_session.id` | 恢复模式时的来源会话。 |
| `created_at` | `TEXT` | 非空，默认 `CURRENT_TIMESTAMP` | 记录创建时间。 |
| `started_at` | `TEXT` | 可空 | 实际开始时间。 |
| `stopped_at` | `TEXT` | 可空 | 中断或暂停时间。 |
| `finished_at` | `TEXT` | 可空 | 正常结束时间。 |

关键索引与约束：

- `uq_scan_session_running_singleton`：库内同一时刻只允许一个 `status = 'running'` 的扫描会话。

### `scan_session_source`

用途：扫描会话下针对某个来源目录的执行状态与阶段计数。

| 字段 | 类型 | 约束 / 默认值 | 含义 |
| --- | --- | --- | --- |
| `id` | `INTEGER` | 主键，自增 | 会话来源记录主键。 |
| `scan_session_id` | `INTEGER` | 非空，外键到 `scan_session.id` | 所属扫描会话。 |
| `library_source_id` | `INTEGER` | 非空，外键到 `library_source.id` | 对应的图库来源。 |
| `status` | `TEXT` | 非空，默认 `pending`，枚举同 `scan_session.status` | 当前来源的执行状态。 |
| `cursor_json` | `TEXT` | 可空 | 来源级扫描 cursor。 |
| `discovered_count` | `INTEGER` | 非空，默认 `0` | 已发现资产数量。 |
| `metadata_done_count` | `INTEGER` | 非空，默认 `0` | 已完成元数据阶段的资产数量。 |
| `faces_done_count` | `INTEGER` | 非空，默认 `0` | 已完成人脸检测阶段的资产数量。 |
| `embeddings_done_count` | `INTEGER` | 非空，默认 `0` | 已完成人脸向量阶段的资产数量。 |
| `assignment_done_count` | `INTEGER` | 非空，默认 `0` | 已完成归属阶段的资产数量。 |
| `last_checkpoint_at` | `TEXT` | 可空 | 最近一次写 checkpoint 的时间。 |
| `created_at` | `TEXT` | 非空，默认 `CURRENT_TIMESTAMP` | 创建时间。 |
| `updated_at` | `TEXT` | 非空，默认 `CURRENT_TIMESTAMP` | 最近更新时间。 |

关键索引与约束：

- `UNIQUE (scan_session_id, library_source_id)`：同一会话内同一来源只能出现一次。

### `scan_checkpoint`

用途：扫描阶段级 checkpoint，用于暂停、恢复和进度观测。

| 字段 | 类型 | 约束 / 默认值 | 含义 |
| --- | --- | --- | --- |
| `id` | `INTEGER` | 主键，自增 | Checkpoint 主键。 |
| `scan_session_source_id` | `INTEGER` | 非空，外键到 `scan_session_source.id` | 所属来源级扫描任务。 |
| `phase` | `TEXT` | 非空，枚举 `discover` / `metadata` / `faces` / `embeddings` / `assignment` | Checkpoint 对应阶段。 |
| `cursor_json` | `TEXT` | 可空 | 阶段级处理 cursor。 |
| `pending_asset_count` | `INTEGER` | 非空，默认 `0` | 当时尚未处理完成的资产数量。 |
| `created_at` | `TEXT` | 非空，默认 `CURRENT_TIMESTAMP` | Checkpoint 写入时间。 |

### `photo_asset`

用途：单个图片或 Live Photo 主文件的资产主表。

| 字段 | 类型 | 约束 / 默认值 | 含义 |
| --- | --- | --- | --- |
| `id` | `INTEGER` | 主键，自增 | 资产主键。 |
| `library_source_id` | `INTEGER` | 非空，外键到 `library_source.id` | 资产所属来源。 |
| `primary_path` | `TEXT` | 非空 | 主图路径。 |
| `primary_fingerprint` | `TEXT` | 可空 | 主图文件指纹。 |
| `file_size` | `INTEGER` | 可空 | 主图文件大小。 |
| `mtime` | `REAL` | 可空 | 主图修改时间。 |
| `capture_datetime` | `TEXT` | 可空 | 拍摄时间。 |
| `capture_month` | `TEXT` | 可空 | 拍摄月份，供按月聚合。 |
| `width` | `INTEGER` | 可空 | 主图宽度。 |
| `height` | `INTEGER` | 可空 | 主图高度。 |
| `is_heic` | `INTEGER` | 非空，默认 `0`，`0/1` | 主图是否为 HEIC。 |
| `live_mov_path` | `TEXT` | 可空 | Live Photo 关联 MOV 路径。 |
| `live_mov_fingerprint` | `TEXT` | 可空 | Live Photo 关联 MOV 指纹。 |
| `processing_status` | `TEXT` | 非空，默认 `discovered`，枚举 `discovered` / `metadata_done` / `faces_done` / `embeddings_done` / `assignment_done` / `failed` | 当前处理阶段。 |
| `last_processed_session_id` | `INTEGER` | 可空，外键到 `scan_session.id` | 最近一次处理该资产的扫描会话。 |
| `last_error` | `TEXT` | 可空 | 最近一次处理失败信息。 |
| `indexed_at` | `TEXT` | 非空，默认 `CURRENT_TIMESTAMP` | 初次入库时间。 |
| `created_at` | `TEXT` | 非空，默认 `CURRENT_TIMESTAMP` | 记录创建时间。 |
| `updated_at` | `TEXT` | 非空，默认 `CURRENT_TIMESTAMP` | 最近更新时间。 |

关键索引与约束：

- `UNIQUE (library_source_id, primary_path)`：同一来源下主图路径唯一。
- `idx_photo_asset_source_status`：按来源与处理状态查询资产时使用。

### `face_observation`

用途：单张图片里一次人脸检测结果。

| 字段 | 类型 | 约束 / 默认值 | 含义 |
| --- | --- | --- | --- |
| `id` | `INTEGER` | 主键，自增 | 人脸观测主键。 |
| `photo_asset_id` | `INTEGER` | 非空，外键到 `photo_asset.id` | 该人脸所属图片。 |
| `bbox_top` | `REAL` | 非空 | 检测框上边界坐标。 |
| `bbox_right` | `REAL` | 非空 | 检测框右边界坐标。 |
| `bbox_bottom` | `REAL` | 非空 | 检测框下边界坐标。 |
| `bbox_left` | `REAL` | 非空 | 检测框左边界坐标。 |
| `face_area_ratio` | `REAL` | 可空 | 人脸面积占整张图像的比例。 |
| `sharpness_score` | `REAL` | 可空 | 清晰度分数。 |
| `pose_score` | `REAL` | 可空 | 姿态分数。 |
| `quality_score` | `REAL` | 可空 | 综合质量分数。 |
| `crop_path` | `TEXT` | 可空 | 人脸裁剪图路径。 |
| `detector_key` | `TEXT` | 可空 | 检测器标识。 |
| `detector_version` | `TEXT` | 可空 | 检测器版本。 |
| `observed_at` | `TEXT` | 非空，默认 `CURRENT_TIMESTAMP` | 观测写入时间。 |
| `active` | `INTEGER` | 非空，默认 `1`，`0/1` | 该观测是否仍参与当前流程。 |
| `created_at` | `TEXT` | 非空，默认 `CURRENT_TIMESTAMP` | 记录创建时间。 |

### `face_embedding`

用途：人脸观测对应的向量特征。

| 字段 | 类型 | 约束 / 默认值 | 含义 |
| --- | --- | --- | --- |
| `id` | `INTEGER` | 主键，自增 | 向量记录主键。 |
| `face_observation_id` | `INTEGER` | 非空，外键到 `face_observation.id` | 所属人脸观测。 |
| `feature_type` | `TEXT` | 非空，默认 `face`，当前仅允许 `face` | 特征类型。 |
| `model_key` | `TEXT` | 可空 | 向量模型标识。 |
| `dimension` | `INTEGER` | 可空 | 向量维度。 |
| `vector_blob` | `BLOB` | 可空 | 序列化后的向量数据。 |
| `normalized` | `INTEGER` | 非空，默认 `1`，`0/1` | 向量是否已归一化。 |
| `generated_at` | `TEXT` | 非空，默认 `CURRENT_TIMESTAMP` | 向量生成时间。 |

关键索引与约束：

- `UNIQUE (face_observation_id, feature_type)`：同一观测同一种特征只保留一条记录。

### `auto_cluster_batch`

用途：一次自动聚类运行的批次元数据。

| 字段 | 类型 | 约束 / 默认值 | 含义 |
| --- | --- | --- | --- |
| `id` | `INTEGER` | 主键，自增 | 聚类批次主键。 |
| `model_key` | `TEXT` | 非空 | 聚类所基于的向量模型。 |
| `algorithm_version` | `TEXT` | 非空 | 聚类算法版本。 |
| `batch_type` | `TEXT` | 非空，枚举 `bootstrap` / `incremental` | 批次类型。 |
| `threshold_profile_id` | `INTEGER` | 可空，外键到 `identity_threshold_profile.id` | 该批聚类使用的阈值配置。 |
| `scan_session_id` | `INTEGER` | 可空，外键到 `scan_session.id` | 触发该批聚类的扫描会话。 |
| `created_at` | `TEXT` | 非空，默认 `CURRENT_TIMESTAMP` | 批次创建时间。 |

### `auto_cluster`

用途：自动聚类批次中的单个 cluster。

| 字段 | 类型 | 约束 / 默认值 | 含义 |
| --- | --- | --- | --- |
| `id` | `INTEGER` | 主键，自增 | Cluster 主键。 |
| `batch_id` | `INTEGER` | 非空，外键到 `auto_cluster_batch.id` | 所属聚类批次。 |
| `representative_observation_id` | `INTEGER` | 可空，外键到 `face_observation.id` | 代表该 cluster 的观测。 |
| `cluster_status` | `TEXT` | 非空，枚举 `materialized` / `review_pending` / `review_resolved` / `ignored` / `discarded` | 当前 cluster 的处置状态。 |
| `resolved_person_id` | `INTEGER` | 可空，外键到 `person.id` | cluster 最终落到的人物。 |
| `diagnostic_json` | `TEXT` | 非空，默认 `'{}'` | cluster 决策诊断信息。 |
| `created_at` | `TEXT` | 非空，默认 `CURRENT_TIMESTAMP` | 创建时间。 |

### `auto_cluster_member`

用途：自动聚类结果与人脸观测之间的成员关系。

| 字段 | 类型 | 约束 / 默认值 | 含义 |
| --- | --- | --- | --- |
| `id` | `INTEGER` | 主键，自增 | 成员关系主键。 |
| `cluster_id` | `INTEGER` | 非空，外键到 `auto_cluster.id` | 所属 cluster。 |
| `face_observation_id` | `INTEGER` | 非空，外键到 `face_observation.id` | 参与该 cluster 的人脸观测。 |
| `membership_score` | `REAL` | 可空 | 该观测落入 cluster 的成员分数。 |
| `quality_score_snapshot` | `REAL` | 可空 | 写入时刻的质量分快照。 |
| `is_seed_candidate` | `INTEGER` | 非空，默认 `0`，`0/1` | 是否入选 bootstrap seed 候选。 |
| `created_at` | `TEXT` | 非空，默认 `CURRENT_TIMESTAMP` | 创建时间。 |

关键索引与约束：

- `UNIQUE (cluster_id, face_observation_id)`：同一观测不能重复加入同一 cluster。

### `identity_threshold_profile`

用途：身份重建与自动归属流程的阈值配置表。

字段契约（按职责分组）：

| 字段组 | 字段 | 类型 | 约束 / 默认值 | 含义 |
| --- | --- | --- | --- | --- |
| 基础标识 | `id` | `INTEGER` | 主键，自增 | Profile 主键。 |
| 基础标识 | `profile_name` | `TEXT` | 非空 | Profile 名称。 |
| 基础标识 | `profile_version` | `TEXT` | 非空 | Profile 版本号。 |
| 向量绑定 | `embedding_feature_type` | `TEXT` | 非空 | 向量特征类型（当前为人脸特征）。 |
| 向量绑定 | `embedding_model_key` | `TEXT` | 非空 | 向量模型标识。 |
| 向量绑定 | `embedding_distance_metric` | `TEXT` | 非空 | 距离度量（当前流程按 cosine 语义使用）。 |
| 向量绑定 | `embedding_schema_version` | `TEXT` | 非空 | 向量 schema 版本。 |
| 质量分参数 | `quality_formula_version` | `TEXT` | 非空 | 质量分公式版本。 |
| 质量分参数 | `quality_area_weight` | `REAL` | 非空 | 质量分中面积权重。 |
| 质量分参数 | `quality_sharpness_weight` | `REAL` | 非空 | 质量分中清晰度权重。 |
| 质量分参数 | `quality_pose_weight` | `REAL` | 非空 | 质量分中姿态权重。 |
| 质量分参数 | `area_log_p10` / `area_log_p90` | `REAL` | 非空 | 面积对数分布分位点。 |
| 质量分参数 | `sharpness_log_p10` / `sharpness_log_p90` | `REAL` | 非空 | 清晰度对数分布分位点。 |
| 质量分参数 | `pose_score_p10` / `pose_score_p90` | `REAL` | 可空 | 姿态分布分位点（可按数据情况缺省）。 |
| 质量阈值 | `low_quality_threshold` | `REAL` | 非空 | 低质量阈值。 |
| 质量阈值 | `high_quality_threshold` | `REAL` | 非空 | 高质量阈值。 |
| 质量阈值 | `trusted_seed_quality_threshold` | `REAL` | 非空 | trusted seed 最低质量阈值。 |
| Bootstrap 聚类 | `bootstrap_edge_accept_threshold` | `REAL` | 非空 | 边直接接受阈值。 |
| Bootstrap 聚类 | `bootstrap_edge_candidate_threshold` | `REAL` | 非空 | 边候选阈值。 |
| Bootstrap 聚类 | `bootstrap_margin_threshold` | `REAL` | 非空 | 候选边距阈值。 |
| Bootstrap 聚类 | `bootstrap_min_cluster_size` | `INTEGER` | 非空 | 最小 cluster 大小。 |
| Bootstrap 聚类 | `bootstrap_min_distinct_photo_count` | `INTEGER` | 非空 | 最小不同照片数。 |
| Bootstrap 聚类 | `bootstrap_min_high_quality_count` | `INTEGER` | 非空 | 最小高质量样本数。 |
| Bootstrap 聚类 | `bootstrap_seed_min_count` / `bootstrap_seed_max_count` | `INTEGER` | 非空 | seed 数量上下限。 |
| 自动归属 | `assignment_auto_min_quality` | `REAL` | 非空 | 自动归属最低质量门槛。 |
| 自动归属 | `assignment_auto_distance_threshold` | `REAL` | 非空 | 自动归属距离阈值。 |
| 自动归属 | `assignment_auto_margin_threshold` | `REAL` | 非空 | 自动归属边距阈值。 |
| 自动归属 | `assignment_review_distance_threshold` | `REAL` | 非空 | 进入人工复核的距离阈值。 |
| 自动归属 | `assignment_require_photo_conflict_free` | `INTEGER` | 非空，`0/1` | 是否要求同图冲突消解后才可自动归属。 |
| Trusted Sample | `trusted_min_quality` | `REAL` | 非空 | trusted 样本最低质量。 |
| Trusted Sample | `trusted_centroid_distance_threshold` | `REAL` | 非空 | trusted 样本到质心距离阈值。 |
| Trusted Sample | `trusted_margin_threshold` | `REAL` | 非空 | trusted 判定边距阈值。 |
| Trusted Sample | `trusted_block_exact_duplicate` | `INTEGER` | 非空，`0/1` | 是否阻断完全重复样本。 |
| Trusted Sample | `trusted_block_burst_duplicate` | `INTEGER` | 非空，`0/1` | 是否阻断 burst 重复样本。 |
| Trusted Sample | `burst_time_window_seconds` | `INTEGER` | 非空 | burst 判定时间窗口（秒）。 |
| 合并建议 | `possible_merge_distance_threshold` | `REAL` | 可空 | 人物合并建议距离阈值。 |
| 合并建议 | `possible_merge_margin_threshold` | `REAL` | 可空 | 人物合并建议边距阈值。 |
| 激活状态 | `active` | `INTEGER` | 非空，默认 `0`，`0/1` | 当前 profile 是否激活。 |
| 激活状态 | `activated_at` | `TEXT` | 可空 | 最近一次激活时间。 |
| 审计字段 | `created_at` | `TEXT` | 非空，默认 `CURRENT_TIMESTAMP` | 创建时间。 |
| 审计字段 | `updated_at` | `TEXT` | 非空，默认 `CURRENT_TIMESTAMP` | 最近更新时间。 |

关键索引与约束：

- `uq_identity_threshold_profile_active`：`active = 1` 的记录在全库内最多一条。

### `person`

用途：人物实体主表。

| 字段 | 类型 | 约束 / 默认值 | 含义 |
| --- | --- | --- | --- |
| `id` | `INTEGER` | 主键，自增 | 人物主键。 |
| `display_name` | `TEXT` | 非空 | 人物显示名。 |
| `cover_observation_id` | `INTEGER` | 可空，外键到 `face_observation.id` | 人物封面所用的人脸观测。 |
| `origin_cluster_id` | `INTEGER` | 可空，外键到 `auto_cluster.id` | 人物最初来源 cluster。 |
| `status` | `TEXT` | 非空，默认 `active`，枚举 `active` / `merged` / `ignored` | 人物状态。 |
| `notes` | `TEXT` | 可空 | 人物备注。 |
| `confirmed` | `INTEGER` | 非空，默认 `0`，`0/1` | 人物是否被人工确认。 |
| `ignored` | `INTEGER` | 非空，默认 `0`，`0/1` | 人物是否被标记为忽略。 |
| `merged_into_person_id` | `INTEGER` | 可空，外键到 `person.id` | 如果已合并，指向目标人物。 |
| `created_at` | `TEXT` | 非空，默认 `CURRENT_TIMESTAMP` | 创建时间。 |
| `updated_at` | `TEXT` | 非空，默认 `CURRENT_TIMESTAMP` | 最近更新时间。 |

### `person_face_assignment`

用途：当前人物归属关系，以及历史归属记录。

| 字段 | 类型 | 约束 / 默认值 | 含义 |
| --- | --- | --- | --- |
| `id` | `INTEGER` | 主键，自增 | 归属记录主键。 |
| `person_id` | `INTEGER` | 非空，外键到 `person.id` | 归属到的人物。 |
| `face_observation_id` | `INTEGER` | 非空，外键到 `face_observation.id` | 被归属的人脸观测。 |
| `assignment_source` | `TEXT` | 非空，枚举 `bootstrap` / `auto` / `manual` / `merge` | 归属来源。 |
| `diagnostic_json` | `TEXT` | 非空，默认 `'{}'` | 归属决策诊断信息。 |
| `threshold_profile_id` | `INTEGER` | 可空，外键到 `identity_threshold_profile.id` | 归属时使用的阈值配置。 |
| `locked` | `INTEGER` | 非空，默认 `0`，`0/1` | 归属是否锁定，不允许自动覆盖。 |
| `confirmed_at` | `TEXT` | 可空 | 人工确认时间。 |
| `active` | `INTEGER` | 非空，默认 `1`，`0/1` | 该归属是否为当前有效归属。 |
| `created_at` | `TEXT` | 非空，默认 `CURRENT_TIMESTAMP` | 创建时间。 |
| `updated_at` | `TEXT` | 非空，默认 `CURRENT_TIMESTAMP` | 最近更新时间。 |

关键索引与约束：

- `uq_person_face_assignment_active_observation`：`active = 1` 的记录中，同一 `face_observation_id` 只能有一个有效归属。

### `person_trusted_sample`

用途：人物可信样本池，用于后续原型与 ANN 构建。

| 字段 | 类型 | 约束 / 默认值 | 含义 |
| --- | --- | --- | --- |
| `id` | `INTEGER` | 主键，自增 | 可信样本主键。 |
| `person_id` | `INTEGER` | 非空，外键到 `person.id` | 所属人物。 |
| `face_observation_id` | `INTEGER` | 非空，外键到 `face_observation.id` | 样本观测。 |
| `trust_source` | `TEXT` | 非空，枚举 `bootstrap_seed` / `manual_confirm` | 样本来源。 |
| `trust_score` | `REAL` | 非空，`0.0~1.0` | 可信分。 |
| `quality_score_snapshot` | `REAL` | 非空 | 样本入池时质量分。 |
| `threshold_profile_id` | `INTEGER` | 非空，外键到 `identity_threshold_profile.id` | 样本判定使用的阈值配置。 |
| `source_review_id` | `INTEGER` | 可空，外键到 `review_item.id` | 来源复核项。 |
| `source_auto_cluster_id` | `INTEGER` | 可空，外键到 `auto_cluster.id` | 来源 cluster。 |
| `active` | `INTEGER` | 非空，默认 `1`，`0/1` | 样本是否仍有效。 |
| `created_at` | `TEXT` | 非空，默认 `CURRENT_TIMESTAMP` | 创建时间。 |
| `updated_at` | `TEXT` | 非空，默认 `CURRENT_TIMESTAMP` | 最近更新时间。 |

关键索引与约束：

- `uq_person_trusted_sample_active_observation`：`active = 1` 的记录中，同一 `face_observation_id` 只能出现一次。

### `person_face_exclusion`

用途：显式记录“某个人物不应再关联某张脸”的排除关系。

| 字段 | 类型 | 约束 / 默认值 | 含义 |
| --- | --- | --- | --- |
| `id` | `INTEGER` | 主键，自增 | 排除记录主键。 |
| `person_id` | `INTEGER` | 非空，外键到 `person.id` | 被排除的人物。 |
| `face_observation_id` | `INTEGER` | 非空，外键到 `face_observation.id` | 被排除的人脸观测。 |
| `assignment_id` | `INTEGER` | 可空，外键到 `person_face_assignment.id` | 如果该排除由既有归属产生，这里记录原归属。 |
| `reason` | `TEXT` | 非空，默认 `manual_exclude` | 排除原因。 |
| `active` | `INTEGER` | 非空，默认 `1`，`0/1` | 该排除是否仍生效。 |
| `created_at` | `TEXT` | 非空，默认 `CURRENT_TIMESTAMP` | 创建时间。 |
| `updated_at` | `TEXT` | 非空，默认 `CURRENT_TIMESTAMP` | 最近更新时间。 |

关键索引与约束：

- `UNIQUE (person_id, face_observation_id)`：同一人物与同一观测只能保留一条排除关系。
- `idx_person_face_exclusion_observation_active`：按观测查询生效排除时使用。
- `idx_person_face_exclusion_person_active`：按人物查询生效排除时使用。

### `person_prototype`

用途：人物原型向量表，用于相似度检索与归属判断。

| 字段 | 类型 | 约束 / 默认值 | 含义 |
| --- | --- | --- | --- |
| `id` | `INTEGER` | 主键，自增 | 原型主键。 |
| `person_id` | `INTEGER` | 非空，外键到 `person.id` | 原型所属人物。 |
| `prototype_type` | `TEXT` | 非空，枚举 `centroid` / `medoid` / `exemplar` | 原型类型。 |
| `source_observation_id` | `INTEGER` | 可空，外键到 `face_observation.id` | 若是样本型原型，来源观测。 |
| `model_key` | `TEXT` | 可空 | 原型所用向量模型。 |
| `vector_blob` | `BLOB` | 可空 | 序列化后的原型向量。 |
| `quality_score` | `REAL` | 可空 | 原型质量分。 |
| `active` | `INTEGER` | 非空，默认 `1`，`0/1` | 该原型是否仍生效。 |
| `updated_at` | `TEXT` | 非空，默认 `CURRENT_TIMESTAMP` | 最近更新时间。 |

### `review_item`

用途：需要人工处理的复核队列表。

| 字段 | 类型 | 约束 / 默认值 | 含义 |
| --- | --- | --- | --- |
| `id` | `INTEGER` | 主键，自增 | 复核项主键。 |
| `review_type` | `TEXT` | 非空，枚举 `new_person` / `possible_merge` / `possible_split` / `low_confidence_assignment` | 复核类型。当前 schema 仍包含 `possible_split`。 |
| `primary_person_id` | `INTEGER` | 可空，外键到 `person.id` | 主关联人物。 |
| `secondary_person_id` | `INTEGER` | 可空，外键到 `person.id` | 次关联人物。 |
| `face_observation_id` | `INTEGER` | 可空，外键到 `face_observation.id` | 关联的人脸观测。 |
| `payload_json` | `TEXT` | 非空 | 复核上下文数据。 |
| `priority` | `INTEGER` | 非空，默认 `0` | 复核优先级。 |
| `status` | `TEXT` | 非空，默认 `open`，枚举 `open` / `resolved` / `dismissed` | 复核状态。 |
| `created_at` | `TEXT` | 非空，默认 `CURRENT_TIMESTAMP` | 创建时间。 |
| `resolved_at` | `TEXT` | 可空 | 复核处理完成时间。 |

### `export_template`

用途：导出规则模板主表。

| 字段 | 类型 | 约束 / 默认值 | 含义 |
| --- | --- | --- | --- |
| `id` | `INTEGER` | 主键，自增 | 模板主键。 |
| `name` | `TEXT` | 非空 | 模板名称。 |
| `output_root` | `TEXT` | 非空 | 导出根目录。 |
| `include_group` | `INTEGER` | 非空，默认 `1`，`0/1` | 是否导出多人合照桶。 |
| `export_live_mov` | `INTEGER` | 非空，默认 `0`，`0/1` | 是否导出 Live Photo 的 MOV。 |
| `start_datetime` | `TEXT` | 可空 | 导出时间窗口起点。 |
| `end_datetime` | `TEXT` | 可空 | 导出时间窗口终点。 |
| `enabled` | `INTEGER` | 非空，默认 `1`，`0/1` | 模板是否启用。 |
| `created_at` | `TEXT` | 非空，默认 `CURRENT_TIMESTAMP` | 创建时间。 |
| `updated_at` | `TEXT` | 非空，默认 `CURRENT_TIMESTAMP` | 最近更新时间。 |

### `export_template_person`

用途：导出模板与人物的绑定关系。

| 字段 | 类型 | 约束 / 默认值 | 含义 |
| --- | --- | --- | --- |
| `id` | `INTEGER` | 主键，自增 | 绑定记录主键。 |
| `template_id` | `INTEGER` | 非空，外键到 `export_template.id` | 所属导出模板。 |
| `person_id` | `INTEGER` | 非空，外键到 `person.id` | 被模板选中的人物。 |
| `position` | `INTEGER` | 非空，默认 `0` | 模板内排序位置。 |
| `created_at` | `TEXT` | 非空，默认 `CURRENT_TIMESTAMP` | 创建时间。 |

关键索引与约束：

- `UNIQUE (template_id, person_id)`：同一模板内人物不能重复。
- `UNIQUE (template_id, position)`：同一模板内位置不能重复。

### `export_run`

用途：一次模板导出执行的汇总记录。

| 字段 | 类型 | 约束 / 默认值 | 含义 |
| --- | --- | --- | --- |
| `id` | `INTEGER` | 主键，自增 | 导出运行主键。 |
| `template_id` | `INTEGER` | 非空，外键到 `export_template.id` | 触发导出的模板。 |
| `spec_hash` | `TEXT` | 非空 | 模板展开后规格的哈希。 |
| `status` | `TEXT` | 非空，默认 `pending`，枚举 `pending` / `running` / `completed` / `failed` | 导出运行状态。 |
| `matched_only_count` | `INTEGER` | 非空，默认 `0` | 命中单人桶的资产数量。 |
| `matched_group_count` | `INTEGER` | 非空，默认 `0` | 命中多人桶的资产数量。 |
| `exported_count` | `INTEGER` | 非空，默认 `0` | 实际导出成功数量。 |
| `skipped_count` | `INTEGER` | 非空，默认 `0` | 跳过数量。 |
| `failed_count` | `INTEGER` | 非空，默认 `0` | 导出失败数量。 |
| `started_at` | `TEXT` | 可空 | 开始导出时间。 |
| `finished_at` | `TEXT` | 可空 | 结束导出时间。 |
| `created_at` | `TEXT` | 非空，默认 `CURRENT_TIMESTAMP` | 记录创建时间。 |

### `export_delivery`

用途：导出结果的落盘记录与校验状态。

| 字段 | 类型 | 约束 / 默认值 | 含义 |
| --- | --- | --- | --- |
| `id` | `INTEGER` | 主键，自增 | 导出交付记录主键。 |
| `template_id` | `INTEGER` | 非空，外键到 `export_template.id` | 所属模板。 |
| `spec_hash` | `TEXT` | 非空 | 导出规格哈希。 |
| `photo_asset_id` | `INTEGER` | 非空，外键到 `photo_asset.id` | 被导出的资产。 |
| `asset_variant` | `TEXT` | 非空，枚举 `primary` / `live_mov` | 导出的是主图还是 Live Photo MOV。 |
| `bucket` | `TEXT` | 非空，枚举 `only` / `group` | 该文件归入单人桶还是多人桶。 |
| `target_path` | `TEXT` | 非空 | 导出目标路径。 |
| `source_fingerprint` | `TEXT` | 可空 | 导出时源文件指纹。 |
| `status` | `TEXT` | 非空，默认 `ok`，枚举 `ok` / `pending` / `skipped` / `failed` / `stale` | 导出交付状态。 |
| `last_exported_at` | `TEXT` | 可空 | 最近一次成功写出时间。 |
| `last_verified_at` | `TEXT` | 可空 | 最近一次校验时间。 |
| `created_at` | `TEXT` | 非空，默认 `CURRENT_TIMESTAMP` | 创建时间。 |
| `updated_at` | `TEXT` | 非空，默认 `CURRENT_TIMESTAMP` | 最近更新时间。 |

关键索引与约束：

- `UNIQUE (template_id, spec_hash, photo_asset_id, asset_variant)`：同一规格下同一资产变体只保留一条交付记录。

### `ops_event`

用途：统一的运行事件、错误和告警日志表。

| 字段 | 类型 | 约束 / 默认值 | 含义 |
| --- | --- | --- | --- |
| `id` | `INTEGER` | 主键，自增 | 事件主键。 |
| `occurred_at` | `TEXT` | 非空，默认 `CURRENT_TIMESTAMP` | 事件发生时间。 |
| `level` | `TEXT` | 非空，枚举 `debug` / `info` / `warning` / `error` | 日志级别。 |
| `component` | `TEXT` | 非空 | 事件来源组件。 |
| `event_type` | `TEXT` | 非空 | 事件类型。 |
| `run_kind` | `TEXT` | 可空 | 运行类型，例如扫描或导出。 |
| `run_id` | `TEXT` | 可空 | 运行实例标识。 |
| `scan_session_id` | `INTEGER` | 可空，外键到 `scan_session.id` | 关联扫描会话。 |
| `scan_session_source_id` | `INTEGER` | 可空，外键到 `scan_session_source.id` | 关联来源级扫描会话。 |
| `export_run_id` | `INTEGER` | 可空，外键到 `export_run.id` | 关联导出运行。 |
| `photo_asset_id` | `INTEGER` | 可空，外键到 `photo_asset.id` | 关联资产。 |
| `face_observation_id` | `INTEGER` | 可空，外键到 `face_observation.id` | 关联人脸观测。 |
| `template_id` | `INTEGER` | 可空，外键到 `export_template.id` | 关联导出模板。 |
| `message` | `TEXT` | 可空 | 简短消息。 |
| `detail_json` | `TEXT` | 可空 | 结构化细节。 |
| `traceback_text` | `TEXT` | 可空 | 异常堆栈文本。 |
| `dedupe_key` | `TEXT` | 可空 | 事件去重键。 |
| `repeat_count` | `INTEGER` | 非空，默认 `1` | 合并后的重复次数。 |

关键索引与约束：

- `idx_ops_event_run`：按 `run_kind`、`run_id`、`occurred_at` 查询运行日志。
- `idx_ops_event_filters`：按 `level`、`event_type`、`occurred_at` 查询过滤日志。

## 历史变更记录

### `0001_people_gallery.sql`

- 初始化整套图库、扫描、人脸、人物、导出和运维 schema。
- 新建 19 张表：
  - `library_source`
  - `scan_session`
  - `scan_session_source`
  - `scan_checkpoint`
  - `photo_asset`
  - `face_observation`
  - `face_embedding`
  - `auto_cluster_batch`
  - `auto_cluster`
  - `auto_cluster_member`
  - `person`
  - `person_face_assignment`
  - `person_prototype`
  - `review_item`
  - `export_template`
  - `export_template_person`
  - `export_run`
  - `export_delivery`
  - `ops_event`
- 新建索引：
  - `uq_library_source_root_path_active`
  - `uq_scan_session_running_singleton`
  - `uq_person_face_assignment_active_observation`
  - `idx_ops_event_run`
  - `idx_ops_event_filters`

### `0002_photo_asset_progress_index.sql`

- 为 `photo_asset` 新增索引 `idx_photo_asset_source_status`。
- 作用：加速按来源和处理状态检索待处理资产。

### `0003_person_face_exclusion.sql`

- 新增 `person_face_exclusion` 表，用于持久化人物与观测之间的排除关系。
- 新建索引：
  - `idx_person_face_exclusion_observation_active`
  - `idx_person_face_exclusion_person_active`

### `0004_identity_rebuild_v3_schema.sql`

- 新增 `identity_threshold_profile` 表与部分唯一索引 `uq_identity_threshold_profile_active`。
- 重建 `auto_cluster_batch` / `auto_cluster` / `auto_cluster_member` 三张表并迁移旧数据，补充 v3 字段：
  - `auto_cluster_batch.batch_type` / `threshold_profile_id` / `scan_session_id`
  - `auto_cluster.cluster_status` / `resolved_person_id` / `diagnostic_json`
  - `auto_cluster_member.quality_score_snapshot` / `is_seed_candidate`
- `person` 表新增 `origin_cluster_id` 外键列。
- 新增 `person_trusted_sample` 表与部分唯一索引 `uq_person_trusted_sample_active_observation`。
- 重建 `person_face_assignment`：
  - 移除 `confidence`
  - 新增 `diagnostic_json`、`threshold_profile_id`
  - `assignment_source` 收敛为 `bootstrap` / `auto` / `manual` / `merge`
  - 迁移时历史 `split` 写入为 `manual`
- 重建 `person_face_exclusion` 并重新绑定 `assignment_id -> person_face_assignment(id)` 外键。

## 维护要求

- 新增 migration 后，必须同步更新本文中的“Schema 概览”“当前表结构”“历史变更记录”。
- 如果只是调整既有 migration 且尚未发布，也必须同步修正文档，确保文档描述与 migration 文件完全一致。
- 本文以 migration 链为准；如果本地数据库实例与 migration 链不一致，应先修正 migration 或补迁移，再更新本文。
