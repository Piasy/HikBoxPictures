# 数据库 Schema 说明

本文档记录当前仓库里会持久化到 SQLite 的 schema。  
目前人物归类调研链路使用的是 `face_review_pipeline` 生成的 `pipeline.db` 缓存库。

## face_review_pipeline / pipeline.db

默认路径示例：`.tmp/<task>/cache/pipeline.db`

### `detected_faces`

用于保存每张已检测人脸的检测结果、embedding、聚类结果与错误状态。

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `face_id` | `TEXT` | 人脸主键，格式为 `<photo_key>_<idx>` |
| `photo_relpath` | `TEXT` | 原图相对路径 |
| `crop_relpath` | `TEXT` | 局部人脸裁剪图相对路径 |
| `context_relpath` | `TEXT` | 带框上下文图相对路径 |
| `preview_relpath` | `TEXT` | 预览图相对路径；当前链路保留字段，通常为空字符串 |
| `aligned_relpath` | `TEXT` | 对齐脸图相对路径 |
| `bbox_json` | `TEXT` | 检测框 `[x1, y1, x2, y2]` 的 JSON |
| `detector_confidence` | `REAL` | 检测置信度 |
| `face_area_ratio` | `REAL` | 人脸面积占原图面积比例 |
| `embedding_json` | `TEXT` | 归一化 embedding 的 JSON 数组 |
| `magface_quality` | `REAL` | MagFace 向量范数 |
| `quality_score` | `REAL` | 综合质量分 |
| `cluster_label` | `INTEGER` | 第一阶段微簇标签；`-1` 表示噪声 |
| `cluster_probability` | `REAL` | HDBSCAN 原生簇成员概率；`person_consensus` 回挂样本为 `NULL` |
| `cluster_assignment_source` | `TEXT` | 显式记录聚类归属来源：`hdbscan` / `person_consensus` / `noise` |
| `face_error` | `TEXT` | 单张人脸处理错误信息 |
| `updated_at` | `TEXT` | 最近更新时间，`CURRENT_TIMESTAMP` |

#### `cluster_assignment_source` 取值约定

| 值 | 含义 |
| --- | --- |
| `hdbscan` | 第一阶段 HDBSCAN 直接成簇的样本 |
| `person_consensus` | 第一阶段先落到噪声，随后被 person-consensus 回挂到某个微簇的样本 |
| `noise` | 当前仍然留在噪声中的样本 |
| `NULL` | 尚未跑到聚类阶段，或 detect 重新入库后等待重算的样本 |

### `failed_images`

用于记录整张图片在 detect 阶段处理失败的情况。

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `photo_relpath` | `TEXT` | 原图相对路径，主键 |
| `error` | `TEXT` | 错误信息 |
| `updated_at` | `TEXT` | 最近更新时间 |

### `pipeline_meta`

用于保存 pipeline 级别元数据。

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `key` | `TEXT` | 元数据键，主键 |
| `value` | `TEXT` | JSON 编码后的值 |
| `updated_at` | `TEXT` | 最近更新时间 |

## 兼容性说明

- 旧版 `pipeline.db` 若缺少 `detected_faces.cluster_assignment_source` 列，当前代码会在打开数据库时自动 `ALTER TABLE` 补列。
- 自动补列后，会按现有 `cluster_label` / `cluster_probability` 回填：
  - `cluster_label = -1` -> `noise`
  - `cluster_label != -1 AND cluster_probability IS NOT NULL` -> `hdbscan`
  - `cluster_label != -1 AND cluster_probability IS NULL` -> `person_consensus`
