# 数据库 Schema 说明

本文档记录当前仓库里 `face_review_pipeline` 生成的 `pipeline.db` schema 与运行语义。  
默认路径示例：`.tmp/<task>/cache/pipeline.db`

## Source 模型

- 图库根目录下每个一级子目录视为一个 `source`。
- 图库根目录若直接放图片，这批图片统一归到保留 `source_key="__root__"`。
- `discover` 默认只会处理 `pipeline_sources.discover_status != 'done'` 的 source。
- 需要重新扫描文件系统时，显式使用 `--refresh`；它只会把已有 source 重新置为 discover pending，并补录新增的一级 source / `__root__`，不会重置已完成的 detect 队列。

## face_review_pipeline / pipeline.db

### `pipeline_sources`

source 级阶段真相表。每行代表一个 source 在 `discover -> detect -> embed -> cluster` 四阶段上的状态。

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `source_key` | `TEXT` | source 主键；一级子目录用目录名，根目录图片固定为 `__root__` |
| `source_relpath` | `TEXT` | source 相对图库根目录的路径；根目录图片固定为 `.` |
| `discover_status` | `TEXT` | `discover` 阶段状态：`pending` / `running` / `done` / `error` |
| `detect_status` | `TEXT` | `detect` 阶段状态：`pending` / `running` / `done` / `error` |
| `embed_status` | `TEXT` | `embed` 阶段状态：`pending` / `running` / `done` |
| `cluster_status` | `TEXT` | `cluster` 阶段状态：`pending` / `running` / `done` |
| `discover_completed_at` | `TEXT` | 最近一次 discover 完成时间 |
| `detect_completed_at` | `TEXT` | 最近一次 detect 完成时间 |
| `embed_completed_at` | `TEXT` | 最近一次 embed 完成时间 |
| `cluster_completed_at` | `TEXT` | 最近一次 cluster 完成时间 |
| `last_error` | `TEXT` | 最近一次 source 级错误信息 |
| `updated_at` | `TEXT` | 最近更新时间 |

说明：

- `discover_status='done'` 表示该 source 已完成文件系统扫描，后续默认不会再扫描。
- `cluster` 仍然是全局聚类，但是否需要重跑由 DB 中是否存在“已 embedding 但尚未回写聚类结果”的 face 决定；只要存在 cluster pending，所有 `embed_status='done'` 的 source 都会被视作 `cluster_status='pending'`，聚类完成后统一回写为 `done`。

### `source_images`

图片级 detect 真相表。每行代表一张图片归属到哪个 source，以及该图片在 detect 阶段上的状态。

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `photo_relpath` | `TEXT` | 原图相对路径，主键 |
| `source_key` | `TEXT` | 所属 source |
| `detect_status` | `TEXT` | detect 状态：`pending` / `done` / `error` |
| `detect_error` | `TEXT` | detect 失败信息；成功时为 `NULL` |
| `updated_at` | `TEXT` | 最近更新时间 |

说明：

- detect 阶段只消费 `source_images.detect_status='pending'` 的记录。
- 新 schema 不再创建 `failed_images` / `processed_images` 表；图片级失败信息统一以 `source_images.detect_status='error'` 与 `detect_error` 为准。

### `detected_faces`

已检测到的人脸结果表，承载 detect / embed / cluster 阶段的人脸级产物。

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `face_id` | `TEXT` | 人脸主键，格式为 `<photo_key>_<idx>` |
| `photo_relpath` | `TEXT` | 原图相对路径 |
| `crop_relpath` | `TEXT` | 局部人脸裁剪图相对路径 |
| `context_relpath` | `TEXT` | 带框上下文图相对路径 |
| `preview_relpath` | `TEXT` | 预览图相对路径；当前链路保留字段，默认空字符串 |
| `aligned_relpath` | `TEXT` | 对齐脸图相对路径 |
| `bbox_json` | `TEXT` | 检测框 `[x1, y1, x2, y2]` 的 JSON |
| `detector_confidence` | `REAL` | 检测置信度 |
| `face_area_ratio` | `REAL` | 人脸面积占原图面积比例 |
| `embedding_json` | `TEXT` | embedding JSON 兼容字段；当前新链路默认不写入 |
| `embedding_blob` | `BLOB` | 归一化 embedding 的二进制向量（当前默认 `float32`） |
| `embedding_dtype` | `TEXT` | `embedding_blob` 的 dtype 标识，当前支持 `float16` / `float32` |
| `magface_quality` | `REAL` | MagFace 向量范数 |
| `quality_score` | `REAL` | 综合质量分 |
| `cluster_label` | `INTEGER` | 第一阶段微簇标签；`-1` 表示噪声 |
| `cluster_probability` | `REAL` | HDBSCAN 原生簇成员概率；`person_consensus` 回挂样本为 `NULL` |
| `cluster_assignment_source` | `TEXT` | 显式记录聚类归属来源 |
| `face_error` | `TEXT` | 单张人脸在 embed 阶段等后续处理中的错误信息 |
| `updated_at` | `TEXT` | 最近更新时间 |

#### `cluster_assignment_source` 取值约定

| 值 | 含义 |
| --- | --- |
| `hdbscan` | 第一阶段 HDBSCAN 直接成簇的样本 |
| `person_consensus` | 第一阶段先落到噪声，随后被 person-consensus 回挂到某个微簇的样本 |
| `noise` | 当前仍留在噪声中的样本 |
| `low_quality_ignored` | 因 face 级质量门控被直接排除到噪声的样本 |
| `NULL` | 尚未完成 cluster 回写，属于 cluster pending 数据 |

说明：

- `embed` 阶段待处理样本判定条件：`embedding_blob IS NULL AND embedding_json IS NULL AND face_error IS NULL`。
- `cluster` 阶段待处理样本判定条件：存在 `(embedding_blob IS NOT NULL OR embedding_json IS NOT NULL) AND face_error IS NULL AND cluster_assignment_source IS NULL` 的 face。

### `pipeline_meta`

pipeline 级元数据表。

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `key` | `TEXT` | 元数据键，主键 |
| `value` | `TEXT` | JSON 编码后的值 |
| `updated_at` | `TEXT` | 最近更新时间 |

常见键：

- `source`
- `source_count`
- `source_scan_mode`
- `source_refresh_requested`
- `discovered_image_count`
- `detector_model_name`
- `det_size`
- `preview_max_side`
- `last_detection_at`
- `magface_checkpoint`
- `embedding_flip_enabled`
- `embedding_flip_weight`
- `last_embedding_at`

## 运行语义

- 主流程固定为 `detect -> embed -> cluster`。
- 每次启动先读 DB，再只处理各阶段的 pending 数据：
  - `discover` 只扫 `pipeline_sources.discover_status != 'done'` 的 source；
  - `detect` 只处理 `source_images.detect_status='pending'` 的图片；
  - `embed` 只处理待 embedding 的 face；
  - `cluster` 只在存在 cluster pending face 时才重跑。
- 连续两次启动时，如果第一次已经跑完，第二次默认不会再次 discover / detect / embed / cluster。
- 新 schema 不包含运行时 `ALTER TABLE` 兜底，也不包含旧表数据回填路径；从空库创建完整 schema 是唯一受支持路径。
