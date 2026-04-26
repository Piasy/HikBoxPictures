# 数据库 Schema 说明（当前已实现）

本文档只描述当前仓库已经落地并经自动化验证的 schema 契约。截止目前，已实现 Slice A「工作区与源目录」、Slice B「可恢复扫描与人脸产物」、Slice C「在线人物归属」以及 Slice D / Feature Slice 2「人物命名与重命名」。本文不承诺后续 merge / exclusion / export 等 future slice 的 schema。

## 1. 存储布局

```text
<workspace>/
  .hikbox/
    config.json
    library.db
    embedding.db
    models/
      insightface/
        models/
          buffalo_l/
            det_10g.onnx
            w600k_r50.onnx

<external_root>/
  artifacts/
    crops/
    context/
  logs/
```

约定：

- `hikbox-pictures init --workspace <path> --external-root <path>` 创建 `config.json`、`library.db`、`embedding.db` 和外部目录骨架。
- `hikbox-pictures scan start --workspace <path> [--batch-size <n>]` 只读取已初始化工作区，不会隐式补建工作区或 source。
- 扫描运行时显式把 `workspace/.hikbox/models/insightface` 作为 InsightFace 模型根目录传入；不依赖 `~/.insightface` 默认目录。
- `external_root/artifacts/crops/` 存放 face crop，`external_root/artifacts/context/` 存放整图缩放加框 context 图。

## 2. `config.json`

初始化成功后，`workspace/.hikbox/config.json` 的最小结构如下：

```json
{
  "config_version": 1,
  "external_root": "/absolute/path/to/external-root"
}
```

说明：

- `config_version` 固定为数字 `1`。
- `external_root` 持久化为初始化命令解析后的绝对路径。

## 3. `library.db`

### 3.1 `schema_meta`

```sql
CREATE TABLE schema_meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
```

初始化时固定写入：

- `schema_meta('schema_version', '1')`

### 3.2 `library_sources`

```sql
CREATE TABLE library_sources (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  path TEXT NOT NULL UNIQUE,
  label TEXT NOT NULL,
  active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
  created_at TEXT NOT NULL
);
```

字段语义：

- `path`：照片源目录绝对路径。
- `label`：用户可读标签。
- `active`：`1` 表示 active，`0` 表示 inactive。
- `created_at`：带 `Z` 后缀的 ISO-8601 UTC 时间字符串。

### 3.3 `assets`

```sql
CREATE TABLE assets (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  source_id INTEGER NOT NULL REFERENCES library_sources(id),
  absolute_path TEXT NOT NULL UNIQUE,
  file_name TEXT NOT NULL,
  file_extension TEXT NOT NULL,
  capture_month TEXT NOT NULL,
  file_fingerprint TEXT NOT NULL,
  live_photo_mov_path TEXT,
  processing_status TEXT NOT NULL CHECK (processing_status IN ('pending', 'succeeded', 'failed')),
  failure_reason TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
```

索引：

```sql
CREATE INDEX idx_assets_source_id ON assets(source_id);
CREATE INDEX idx_assets_processing_status ON assets(processing_status);
```

字段语义：

- `source_id`：来源于 `library_sources.id`。
- `absolute_path`：照片绝对路径，作为单工作区内该照片的稳定唯一键。
- `file_name`：文件名，不含目录。
- `file_extension`：小写后缀，不带点，例如 `jpg`、`heif`。
- `capture_month`：`YYYY-MM`。优先来自 EXIF 日期，缺失时回退到文件修改时间月份。
- `file_fingerprint`：当前实现优先使用文件内容 SHA-256；若源文件在 metadata 阶段不可读，则回退为基于绝对路径、文件大小、修改时间和 inode 的等价幂等键。
- `live_photo_mov_path`：仅对 `heic`/`heif` 尝试配对隐藏 MOV；匹配成功时写入 MOV 绝对路径，否则为 `NULL`。
- `processing_status`：当前值域为：
  - `pending`：schema 保留值，当前扫描实现最终会落为 `succeeded` 或 `failed`
  - `succeeded`：该 asset 已成功完成读取、检测和人脸结果入库
  - `failed`：该 asset 读取失败或解码失败
- `failure_reason`：`processing_status='failed'` 时保存可读失败原因。
- `created_at` / `updated_at`：带 `Z` 后缀的 ISO-8601 UTC 时间字符串。

运行时语义：

- 扫描只会把支持后缀 `jpg`/`jpeg`/`png`/`heic`/`heif` 的文件 discover 为 candidate asset。
- 非支持后缀不会写入 `assets`。
- 损坏图片如果后缀受支持，仍会写入 `assets`，但 `processing_status='failed'`，且不会生成 face、embedding 或产物。
- 重复扫描相同 `absolute_path` 时会复用同一行并更新状态，不会重复插入第二条 asset。

### 3.4 `scan_sessions`

```sql
CREATE TABLE scan_sessions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  plan_fingerprint TEXT NOT NULL UNIQUE,
  batch_size INTEGER NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('running', 'completed', 'failed')),
  command TEXT NOT NULL,
  total_batches INTEGER NOT NULL DEFAULT 0,
  completed_batches INTEGER NOT NULL DEFAULT 0,
  failed_assets INTEGER NOT NULL DEFAULT 0,
  success_faces INTEGER NOT NULL DEFAULT 0,
  artifact_files INTEGER NOT NULL DEFAULT 0,
  started_at TEXT NOT NULL,
  completed_at TEXT
);
```

字段语义：

- `plan_fingerprint`：由本次 discover 结果和 `batch_size` 计算的稳定指纹；同一文件集合和同一批次大小会命中同一 session。
- `batch_size`：本次命令使用的批次大小。
- `status`：
  - `running`：存在未完成批次
  - `completed`：全部批次已完成，且同一 `scan start` 触发的 assignment 阶段已成功完成
  - `failed`：worker 异常退出、批次提交失败，或后续 assignment 阶段失败
- `command`：完整命令文本，例如 `hikbox-pictures scan start --workspace ... --batch-size 10`。
- `total_batches`：该 session 的总批次数。
- `completed_batches`：当前已完成批次数。
- `failed_assets`：当前 session 内失败 asset 数。
- `success_faces`：当前 session 内成功写入的 `face_observations` 数。
- `artifact_files`：当前 session 内产物文件数；当前等于 `success_faces * 2`，即每个 face 一张 crop 和一张 context。
- `started_at` / `completed_at`：带 `Z` 后缀的 ISO-8601 UTC 时间字符串。

运行时语义：

- 成功恢复时复用已有 `scan_sessions` 记录，并基于 `scan_batches.status` 判断哪些批次可跳过。
- 同一 `plan_fingerprint` 下，`completed_batches == total_batches` 时再次执行会直接跳过，不会新建第二个 session。
- 当前实现即使 discover/批次阶段没有新增待处理批次，也会在同一个 `scan start` 里继续执行 assignment 阶段；只有 assignment 也成功完成后，session 才会保持 `completed`。

### 3.5 `scan_batches`

```sql
CREATE TABLE scan_batches (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id INTEGER NOT NULL REFERENCES scan_sessions(id),
  batch_index INTEGER NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('pending', 'running', 'completed', 'failed')),
  item_count INTEGER NOT NULL,
  started_at TEXT,
  completed_at TEXT,
  failure_message TEXT,
  worker_pid INTEGER,
  UNIQUE(session_id, batch_index)
);
```

索引：

```sql
CREATE INDEX idx_scan_batches_session_status ON scan_batches(session_id, status, batch_index);
```

字段语义：

- `session_id`：所属扫描会话。
- `batch_index`：从 `1` 开始的稳定批次序号。
- `status`：
  - `pending`：未处理
  - `running`：已启动子进程处理
  - `completed`：整批结果已提交
  - `failed`：worker 异常退出或整批提交失败
- `item_count`：该批包含的 asset candidate 数。
- `failure_message`：批次失败时的可读错误。
- `worker_pid`：当前实现记录主扫描进程在标记 running 时的 PID，用于排查；不保证等于子进程 PID。

运行时语义：

- 已 `completed` 的批次再次扫描会被跳过。
- 若进程在某批提交完成前被 kill，该批不会标记 `completed`；重跑时整批重跑。

### 3.6 `scan_batch_items`

```sql
CREATE TABLE scan_batch_items (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  batch_id INTEGER NOT NULL REFERENCES scan_batches(id),
  item_index INTEGER NOT NULL,
  source_id INTEGER NOT NULL REFERENCES library_sources(id),
  absolute_path TEXT NOT NULL,
  asset_id INTEGER REFERENCES assets(id),
  status TEXT NOT NULL CHECK (status IN ('pending', 'succeeded', 'failed')),
  failure_reason TEXT,
  face_count INTEGER NOT NULL DEFAULT 0,
  UNIQUE(batch_id, item_index),
  UNIQUE(batch_id, absolute_path)
);
```

索引：

```sql
CREATE INDEX idx_scan_batch_items_batch_id ON scan_batch_items(batch_id, item_index);
```

字段语义：

- `batch_id`：所属批次。
- `item_index`：批内稳定顺序，从 `1` 开始。
- `source_id`：来源 source。
- `absolute_path`：该批次处理的照片绝对路径。
- `asset_id`：整批提交成功后，回填到 `assets.id`。
- `status`：
  - `pending`：未提交
  - `succeeded`：该图片成功提交人脸结果
  - `failed`：该图片读取/解码失败
- `failure_reason`：`status='failed'` 时保存失败原因。
- `face_count`：成功提交时保存该图片写入的人脸数量；失败时为 `0`。

### 3.7 `face_observations`

```sql
CREATE TABLE face_observations (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  asset_id INTEGER NOT NULL REFERENCES assets(id),
  face_index INTEGER NOT NULL,
  bbox_x1 REAL NOT NULL,
  bbox_y1 REAL NOT NULL,
  bbox_x2 REAL NOT NULL,
  bbox_y2 REAL NOT NULL,
  image_width INTEGER NOT NULL,
  image_height INTEGER NOT NULL,
  score REAL NOT NULL,
  crop_path TEXT NOT NULL,
  context_path TEXT NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE(asset_id, face_index)
);
```

索引：

```sql
CREATE INDEX idx_face_observations_asset_id ON face_observations(asset_id, face_index);
```

字段语义：

- `asset_id`：所属照片。
- `face_index`：同一 asset 内从 `0` 开始的检测序号。
- `bbox_x1..bbox_y2`：基于原图坐标系的检测框。
- `image_width` / `image_height`：EXIF 方向纠正后的原图尺寸。
- `score`：检测分数。
- `crop_path`：face crop 产物绝对路径。
- `context_path`：context 产物绝对路径。
- `created_at`：带 `Z` 后缀的 ISO-8601 UTC 时间字符串。

运行时语义：

- 每个成功检测到的人脸恰好对应一条 `face_observations`。
- `crop_path` 指向单人脸裁剪图。
- `context_path` 指向整图缩放到最长边不超过 480 后绘制红色人脸框的 JPEG。
- 当前实现的 artifact 文件名带 session/batch 作用域前缀，并包含 batch item 级唯一标识，不会在同批重复内容照片之间冲突，也不会在重扫时原地覆盖旧文件；只有当新结果成功提交后，旧路径对应文件才会被清理。
- 当前实现对同一 asset 的重检采用 IoU 复用语义：归一化 bbox IoU `> 0.5` 的新检测框会复用旧 `face_observations.id`，并保留旧 `main` embedding 与既有 assignment；未被新检测框匹配的旧 face 会被删除并清理其 embedding / assignment；只有新增检测框才会写入新的 `face_observations` 与 `face_embeddings`。

### 3.8 `person`

```sql
CREATE TABLE person (
  id TEXT PRIMARY KEY,
  display_name TEXT,
  is_named INTEGER NOT NULL DEFAULT 0 CHECK (is_named IN (0, 1)),
  status TEXT NOT NULL CHECK (status IN ('active', 'inactive')),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
```

索引：

```sql
CREATE INDEX idx_person_status ON person(status, is_named, created_at);
CREATE UNIQUE INDEX idx_person_unique_active_display_name
  ON person(display_name)
  WHERE status = 'active' AND is_named = 1;
```

字段语义：

- `id`：匿名人物 UUID。
- `display_name`：首版匿名人物为空，后续 WebUI 命名后才会有值。
- `is_named`：`0` 表示匿名人物，`1` 表示已命名人物。
- `status`：当前 Slice C 只写 `active`；后续 merge/撤销等功能再引入失效语义。
- `updated_at`：人物记录最近一次真正发生状态变化的时间；命名/重命名会更新，no-op 不会更新。

运行时语义：

- `hikbox-pictures scan start` 的 assignment 阶段只会在“自己 + 至少 2 个近邻”达到 `min_faces=3` 且无法复用已有人物时创建匿名 `person`。
- 重复执行同一 `scan start` 时不会重复创建已存在的匿名人物。
- WebUI 命名写入前会先做首尾空白裁剪；裁剪后为空会拒绝写入。
- active 且 `is_named=1` 的人物之间，`display_name` 必须按裁剪后的完整字符串精确唯一；当前不做大小写折叠或别名归并。

### 3.9 `person_name_events`

```sql
CREATE TABLE person_name_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  person_id TEXT NOT NULL REFERENCES person(id),
  event_type TEXT NOT NULL CHECK (event_type IN ('person_named', 'person_renamed')),
  old_display_name TEXT,
  new_display_name TEXT NOT NULL,
  created_at TEXT NOT NULL
);
```

索引：

```sql
CREATE INDEX idx_person_name_events_person_id
  ON person_name_events(person_id, id);
```

字段语义：

- `person_id`：被命名或重命名的人物。
- `event_type`：`person_named` 表示首次命名，`person_renamed` 表示已命名人物改名。
- `old_display_name`：首次命名时为 `NULL`；重命名时保存旧名称。
- `new_display_name`：本次提交后生效的裁剪后名称。
- `created_at`：事件写入时间，带 `Z` 后缀的 ISO-8601 UTC 时间字符串。

运行时语义：

- 每次真正成功的首次命名或重命名都会新增一条事件。
- 重复提交相同名称，或只在前后空白不同但裁剪后相同的名称，会走 no-op 成功路径，不会新增事件。

### 3.10 `assignment_runs`

```sql
CREATE TABLE assignment_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  scan_session_id INTEGER NOT NULL REFERENCES scan_sessions(id),
  algorithm_version TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('running', 'completed', 'failed')),
  param_snapshot_json TEXT NOT NULL,
  candidate_count INTEGER NOT NULL DEFAULT 0,
  assigned_count INTEGER NOT NULL DEFAULT 0,
  new_person_count INTEGER NOT NULL DEFAULT 0,
  deferred_count INTEGER NOT NULL DEFAULT 0,
  skipped_count INTEGER NOT NULL DEFAULT 0,
  failed_count INTEGER NOT NULL DEFAULT 0,
  orphan_embedding_count INTEGER NOT NULL DEFAULT 0,
  orphan_embedding_keys_json TEXT NOT NULL DEFAULT '[]',
  failure_reason TEXT,
  started_at TEXT NOT NULL,
  completed_at TEXT,
  updated_at TEXT NOT NULL
);
```

索引：

```sql
CREATE INDEX idx_assignment_runs_session_id ON assignment_runs(scan_session_id, id);
```

字段语义：

- `scan_session_id`：归属运行所属的 `scan_sessions.id`。
- `algorithm_version`：当前固定为 `immich_v6_online_v1`。
- `param_snapshot_json`：当前固定记录 `max_distance=0.5`、`min_faces=3`、`num_results=3`、`embedding_variant='main'`、`distance_metric='cosine_distance'`、`self_match_included=true`、`two_pass_deferred=true`。
- `candidate_count` / `assigned_count` / `new_person_count` / `deferred_count` / `skipped_count` / `failed_count`：本次 assignment 摘要。
- `orphan_embedding_count` / `orphan_embedding_keys_json`：记录未能关联到任何 `face_observations.id` 的孤儿 `main` embedding；这类数据只记 warning，不参与归属。
- `failure_reason`：assignment 失败时的可读错误。

运行时语义：

- 每次执行公开入口 `hikbox-pictures scan start` 都会在进入 assignment 阶段时创建一条 `assignment_runs`。
- 候选 active face 缺少 `main` embedding、embedding 维度不是 `512` 或 `vector_blob` 不可解码时，当前 run 记为 `failed`，对应 `scan_sessions.status` 也会记为 `failed`。
- 无新增归属或所有候选都保持未归属时，run 仍可 `completed`，但日志会记 `assignment_skipped`。

### 3.11 `person_face_assignments`

```sql
CREATE TABLE person_face_assignments (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  person_id TEXT NOT NULL REFERENCES person(id),
  face_observation_id INTEGER NOT NULL REFERENCES face_observations(id),
  assignment_run_id INTEGER NOT NULL REFERENCES assignment_runs(id),
  assignment_source TEXT NOT NULL CHECK (assignment_source IN ('online_v6')),
  active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
  evidence_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
```

索引与约束：

```sql
CREATE INDEX idx_person_face_assignments_person_id
  ON person_face_assignments(person_id, active, face_observation_id);
CREATE INDEX idx_person_face_assignments_face_id
  ON person_face_assignments(face_observation_id, active, assignment_run_id);
CREATE UNIQUE INDEX idx_person_face_assignments_unique_active_face
  ON person_face_assignments(face_observation_id)
  WHERE active = 1;
```

字段语义：

- `person_id`：归属到的匿名/已命名人物。
- `face_observation_id`：被归属的人脸 observation。
- `assignment_run_id`：本次归属来自哪一次 `assignment_runs`。
- `assignment_source`：当前固定为 `online_v6`。
- `active`：同一张脸同时最多只允许一条 active assignment。
- `evidence_json`：当前记录匹配到的近邻 face id 列表与距离摘要。

运行时语义：

- 当前 Slice C 只新增 active assignment，不实现 merge / undo / exclusion，因此不会自动失效旧 assignment。
- 已有 active assignment 的 face 不会再次作为待归属候选，重复 `scan start` 也不会重复写入 assignment。

## 4. `embedding.db`

### 4.1 `schema_meta`

```sql
CREATE TABLE schema_meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
```

初始化时固定写入：

- `schema_meta('schema_version', '1')`

### 4.2 `face_embeddings`

```sql
CREATE TABLE face_embeddings (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  face_observation_id INTEGER NOT NULL,
  variant TEXT NOT NULL CHECK (variant IN ('main')),
  dimension INTEGER NOT NULL,
  l2_norm REAL NOT NULL,
  vector_blob BLOB NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE(face_observation_id, variant)
);
```

索引：

```sql
CREATE INDEX idx_face_embeddings_face_id ON face_embeddings(face_observation_id, variant);
```

字段语义：

- `face_observation_id`：关联 `library.db.face_observations.id`。
- `variant`：当前只允许 `main`。
- `dimension`：当前实现要求为 `512`。
- `l2_norm`：写入前归一化后的 L2 范数；正常情况下接近 `1.0`。
- `vector_blob`：`float32` 原始字节序列，长度应对应 `512` 维向量。
- `created_at`：带 `Z` 后缀的 ISO-8601 UTC 时间字符串。

运行时语义：

- 每个成功 face 恰好写入一条 `main` embedding。
- 当前不写 `flip` embedding，也不承诺其它 variant。
- 写入前若维度不是 `512`，扫描会失败，不会把该批标记为 `completed`。

## 5. 日志与运行时可观测性

当前实现会在 `external_root/logs/` 下写 JSON Lines 日志：

- `init.log.jsonl`：`hikbox-pictures init` 成功日志。
- `source.log.jsonl`：`hikbox-pictures source add` 成功日志。
- `scan.log.jsonl`：扫描相关日志，当前至少包括：
  - `scan_started`
  - `batch_started`
  - `asset_failed`
  - `batch_completed`
  - `scan_completed`
  - `scan_skipped`
  - `assignment_started`
  - `assignment_completed`
  - `assignment_skipped`
  - `assignment_failed`
  - `assignment_warning`

`scan.log.jsonl` 的单条记录会包含该事件最小必需字段，例如时间戳、`session_id`、`batch_id`、`batch_index`、统计摘要和模型根目录。

## 6. 已实现运行语义摘要

- discover 只扫描 active source 根目录下一层文件，按文件绝对路径稳定排序。
- 支持后缀：`jpg`、`jpeg`、`png`、`heic`、`heif`，大小写不敏感。
- 只有 `heic`/`heif` 尝试匹配同目录隐藏 `.MOV/.mov`，并把命中的绝对路径写入 `assets.live_photo_mov_path`。
- `jpg`/`jpeg`/`png` 不做 live MOV 配对。
- 当前扫描 worker 使用 `det_thresh=0.7` 调用 InsightFace `buffalo_l`。
- 每批调用一个独立 worker 子进程处理真实 InsightFace 检测、embedding 与产物生成。
- 主进程只在整批 worker 成功返回后，才统一提交 `assets`、`face_observations`、`face_embeddings` 和批次 `completed` 状态。
- 单图失败不会阻断同批其它图片提交，但该图不会产生 `face_observations`、`face_embeddings` 或产物。
- 当 discover/批次阶段收敛后，同一个 `hikbox-pictures scan start` 会继续执行在线 assignment 阶段。
- assignment 只读取 `library.db` 中已存在的 active `face_observations` 和 `embedding.db` 中的 `main` embedding；不会重新读照片，也不会重新调用 InsightFace 做归属。
- assignment 使用 HNSW 余弦索引执行两轮在线归属，算法版本固定为 `immich_v6_online_v1`，assignment 来源固定为 `online_v6`。
- orphan `main` embedding 只记录 warning，不进入索引或候选；损坏候选 embedding 会使 assignment 与 `scan_sessions` 一起失败。

## 7. 未在本文承诺的内容

以下内容尚未在当前实现中落地，因此不属于本文档承诺范围：

- 命名、合并、排除、导出相关表
- WebUI、服务端 API、导出账本
- 任何 `schema_version > 1` 的 migration 规则
