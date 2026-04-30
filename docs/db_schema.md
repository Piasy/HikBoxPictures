# 数据库 Schema 说明（当前已实现）

本文档只描述当前仓库已经落地并经自动化验证的 schema 契约。截止目前，已实现 Slice A「工作区与源目录」、Slice B「可恢复扫描与人脸产物」、Slice C「在线人物归属」、Slice D / Feature Slice 2「人物命名与重命名」、Slice E / Feature Slice 1「人物合并」与 Feature Slice 2「最近一次撤销」、Slice F / Feature Slice 1「人物详情页批量排除」、Slice G / Feature Slice 1「导出模板创建与保存」，以及 Feature Slice 2「导出计划持久化与同名冲突消解」的 schema。

## 0. DB Migration 机制

### 0.1 版本追踪

`library.db` 和 `embedding.db` 各自通过 `schema_meta` 表独立追踪当前 `schema_version`：

```sql
CREATE TABLE schema_meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
```

初始化时固定写入 `schema_meta('schema_version', '1')`。每次 migration 执行后，`schema_version` 递增。

两个 DB 各自独立维护版本，互不依赖。

### 0.2 Migration SQL 文件

Migration SQL 文件存放于 `hikbox_pictures/product/db/sql/`，命名规则为 `library_v{N}.sql` 和 `embedding_v{N}.sql`，其中 N 为迁移目标版本号。

示例：
- `library_v1.sql` — library.db 初始全量建表（schema_version = 1）
- `library_v2.sql` — 将 library.db 从 v1 升级到 v2 的增量 DDL
- `embedding_v1.sql` — embedding.db 初始全量建表（schema_version = 1）

### 0.3 自动执行时机

**`init` 命令**：
- 若 workspace 已存在（`.hikbox/` 目录或 `config.json`/`library.db`/`embedding.db` 任一存在），直接报错退出，不升级 DB
- 若 workspace 不存在，创建新 workspace 时先执行 v1 全量建表 SQL，再依次执行后续 migration SQL 升级至最新版本

**`init` 以外的所有命令**（`source add`、`source list`、`scan start`、`serve`）：
- 打开 DB 连接后、执行业务逻辑前，自动执行 migration
- 流程：读取当前 `schema_version` → 按版本序号递增查找后续 migration SQL 文件 → 在同一事务中执行 SQL 并更新 `schema_version` → 全部完成后进入业务逻辑
- 任一 migration 失败则命令启动失败（事务回滚，`schema_version` 不变），不进入业务逻辑
- 已是目标版本时零开销跳过

### 0.4 当前版本

| 数据库 | 文件 | 当前版本 |
|--------|------|----------|
| `library.db` | `library_v3.sql` | 3 |
| `embedding.db` | `embedding_v1.sql` | 1 |

### 0.5 新增 Migration 约定

当需要修改 DB schema 时：
1. 在 `hikbox_pictures/product/db/sql/` 下新增对应的 `library_v{N}.sql` 或 `embedding_v{N}.sql`
2. 更新本文档中对应表的 DDL 描述、当前版本表以及字段语义
3. migration 执行由自动机制完成，无需额外编写调用代码

## 1. 存储布局

```text
<workspace>/
  .hikbox/
    config.json
    library.db
    embedding.db
    operation.lock
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
- `.hikbox/operation.lock` 是 `scan start` 与 `serve` 共享的工作区运行锁文件；同一 workspace 上两者完全互斥。锁的 OS 级持有态是运行时真相，文件内容只用于诊断当前持锁操作。
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
- `label`：用户可读标签；当前由 `hikbox-pictures source add` 自动取源目录的目录名。
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
- CLI 执行 `hikbox-pictures scan start` 时默认每 10 秒向 `stderr` 打印一次进度，固定格式为“阶段、已完成批次数/总批次数、已完成照片数/总照片数”；批处理阶段的照片进度来自 worker stdout 的 `batch_progress` 事件，在线归属阶段由主进程在 assignment 阶段切换时输出。

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
  write_revision INTEGER NOT NULL DEFAULT 0,
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
- `status`：`active` 表示仍可见且可继续接收 active assignment 的人物；`inactive` 表示该人物已经退出 active 生命周期，不再出现在首页、详情页或后续可操作人物集合中。当前会进入 `inactive` 的路径包括：真实 merge 中作为 loser 失效，以及人物详情页批量排除把该人物全部样本排空。
- `write_revision`：人物相关真实写入版本号。只有会改变该人物真相的持久化写入才会递增，例如命名/重命名、merge 导致的 winner/loser 状态变化、新增 active assignment 写入，以及人物详情页批量排除导致的 active assignment 失效与 exclusion 真相写入；no-op 名称提交不会递增。
- `updated_at`：人物记录最近一次真正发生状态变化的时间；命名/重命名会更新，no-op 不会更新。

运行时语义：

- `hikbox-pictures scan start` 的 assignment 阶段只会在“自己 + 至少 2 个近邻”达到 `min_faces=3` 且无法复用已有人物时创建匿名 `person`。
- 重复执行同一 `scan start` 时不会重复创建已存在的匿名人物。
- WebUI 命名写入前会先做首尾空白裁剪；裁剪后为空会拒绝写入。
- active 且 `is_named=1` 的人物之间，`display_name` 必须按裁剪后的完整字符串精确唯一；当前不做大小写折叠或别名归并。
- 当前 Slice E 的真实 merge 会把 loser 的全部 active assignment 迁移到 winner，并把 loser 标记为 `inactive`；winner 的 `id`、`display_name`、`is_named` 保持不变。
- Slice E / Feature Slice 2 的 undo eligibility 不依赖 `updated_at` 模糊推断，而是依赖 `write_revision` 与 merge 账本中的版本快照精确判断“merge 之后是否发生了新的真实人物相关写入”。

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

- 在线归属新增的 active assignment 仍然只写一条 active 记录；当前 Slice E 的 merge 不会新建第二条 assignment，而是直接把 loser 现有 active assignment 的 `person_id` 迁移到 winner，并更新 `updated_at`。
- 已有 active assignment 的 face 不会再次作为待归属候选，重复 `scan start` 也不会重复写入 assignment。
- 因为在线归属读取“已有 active assignment 的 face 当前所属 `person_id`”作为历史锚点，merge 后 loser 旧样本上的 active assignment 已经改指向 winner，所以后续新增的 loser-like 样本会继续归到 winner，而不会 resurrect loser。
- 人物详情页批量排除不会删除 assignment 历史行，而是把被选中的行改成 `active = 0`，同时保留原 `id` / `face_observation_id` 供 exclusion 真相和后续审计引用。
- 如果一次批量排除让某个人物不再拥有任何 active assignment，该人物会被置为 `inactive`；若它之前是已命名人物，其 `display_name` 会保留作历史记录，但不会再参与 active 名称唯一性约束，因此该名称可被其它 active 人物复用。
- 当同一路径图片在后续真实 `scan start` 中发生重检测失配、处理失败或等价 invalidation，系统会先删除旧的 active assignment / face rows，再按新结果重建；这类删除或失效同样属于真实人物相关写入，会把受影响 `person.write_revision` 递增。

### 3.12 `person_face_exclusions`

```sql
CREATE TABLE person_face_exclusions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  face_observation_id INTEGER NOT NULL REFERENCES face_observations(id),
  excluded_person_id TEXT NOT NULL REFERENCES person(id),
  source_assignment_id INTEGER REFERENCES person_face_assignments(id),
  created_at TEXT NOT NULL,
  UNIQUE(face_observation_id, excluded_person_id)
);
```

索引：

```sql
CREATE INDEX idx_person_face_exclusions_face_id
  ON person_face_exclusions(face_observation_id, excluded_person_id, id);
CREATE INDEX idx_person_face_exclusions_person_id
  ON person_face_exclusions(excluded_person_id, face_observation_id, id);
```

字段语义：

- `face_observation_id`：被禁止“自动归回某个人物”的那张 face。
- `excluded_person_id`：这张 face 不能再自动归回的 `person.id`。
- `source_assignment_id`：触发这次排除时失效的 `person_face_assignments.id`；当前实现用于把详情页选择动作追溯回当时的 active assignment。
- `created_at`：排除真相写入时间，带 `Z` 后缀的 ISO-8601 UTC 时间字符串。

运行时语义：

- 这张表表达的稳定真相是“`face_observation_id` 不能再自动归到 `excluded_person_id`”，而不是“某次按钮点击发生过”。
- 同一对 `face_observation_id + excluded_person_id` 最多只能存在一条记录；重复排除同一人物会被真实 HTTP 入口拒绝并保持 DB 不变。
- 同一个 `face_observation_id` 可以随着时间累积多条 exclusion，只要它们的 `excluded_person_id` 不同。
- 当某张 face 没有 active assignment 但存在一条或多条 exclusion 时，后续 `scan start` 仍会把它当作候选；区别是在线归属选择已有 active person 时，必须排除本表中的全部 `excluded_person_id`。
- 仅使用同一套固定图库重扫时，被排除的旧 face 不能仅凭彼此之间的相似度自发重组成新的匿名人物；如果没有其它未被排除的人物可挂靠，它们会继续保持未归属。
- 当后续新增 source 带来新的同类 face 且这些新 face 本身没有对应 exclusion 时，它们仍然可以先形成新的匿名人物；之前被排除的旧 face 会在同一次或后续 assignment 中挂到这个新人物，而不会回到旧的 `excluded_person_id`。

### 3.13 `person_merge_operations`

```sql
CREATE TABLE person_merge_operations (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  winner_person_id TEXT NOT NULL REFERENCES person(id),
  loser_person_id TEXT NOT NULL REFERENCES person(id),
  winner_display_name_before TEXT,
  winner_is_named_before INTEGER NOT NULL CHECK (winner_is_named_before IN (0, 1)),
  winner_status_before TEXT NOT NULL CHECK (winner_status_before IN ('active', 'inactive')),
  loser_display_name_before TEXT,
  loser_is_named_before INTEGER NOT NULL CHECK (loser_is_named_before IN (0, 1)),
  loser_status_before TEXT NOT NULL CHECK (loser_status_before IN ('active', 'inactive')),
  winner_write_revision_after_merge INTEGER NOT NULL,
  loser_write_revision_after_merge INTEGER NOT NULL,
  merged_at TEXT NOT NULL,
  undone_at TEXT
);
```

索引：

```sql
CREATE INDEX idx_person_merge_operations_merged_at
  ON person_merge_operations(id DESC, merged_at DESC);
```

字段语义：

- `winner_person_id`：本次 two-person merge 的 canonical winner。
- `loser_person_id`：本次 two-person merge 中被失效的人物。
- `winner_display_name_before` / `winner_is_named_before` / `winner_status_before`：winner 在 merge 前的可观察状态快照。
- `loser_display_name_before` / `loser_is_named_before` / `loser_status_before`：loser 在 merge 前的可观察状态快照。
- `winner_write_revision_after_merge` / `loser_write_revision_after_merge`：这次 merge 成功提交后 winner 与 loser 的 `person.write_revision` 版本快照。undo 会用它判断 merge 之后这两个人物是否又发生了新的真实人物相关写入。
- `merged_at`：本次 merge 成功提交时间，带 `Z` 后缀的 ISO-8601 UTC 时间字符串。
- `undone_at`：最近一次 merge 成功撤销时写入撤销完成时间；未撤销时为 `NULL`。

运行时语义：

- 当前 Slice E 只支持 exactly-two merge，因此每次成功 merge 恰好写入一条 `person_merge_operations`。
- winner 规则固定为：一个已命名 + 一个匿名时已命名人物赢；两个匿名时样本数更多者赢；样本数相同时 `person_id` 更小者赢；两个已命名人物直接拒绝。
- merge 事务只有在 assignment 迁移、loser 置 `inactive`、真相记录与 assignment 快照全部成功落库后才会提交；任一步失败都会整体回滚。
- undo 只针对 `id` 最新且 `undone_at IS NULL` 的最近一次成功 merge；如果 winner 或 loser 当前 `write_revision` 与本表记录的 merge 后版本快照不一致，则表示 merge 后已经发生新的真实人物相关写入，undo 必须被拒绝。
- 除版本快照外，undo 还会验证当前 winner 的 active assignment 集合必须精确等于本次 merge 快照里的 `winner + loser` 并集，且当前 loser 不能残留 active assignment；如果不一致，则视为最近一次 merge 的 snapshot/关联账本不完整，undo 必须失败且 DB 保持不变。
- undo 成功时会恢复 winner/loser 的 `display_name`、`is_named`、`status` 与 assignment owner，并把本行 `undone_at` 置为撤销完成时间；同一行不能被第二次撤销。

### 3.14 `person_merge_operation_assignments`

```sql
CREATE TABLE person_merge_operation_assignments (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  merge_operation_id INTEGER NOT NULL REFERENCES person_merge_operations(id),
  assignment_id INTEGER NOT NULL REFERENCES person_face_assignments(id),
  person_role TEXT NOT NULL CHECK (person_role IN ('winner', 'loser'))
);
```

索引：

```sql
CREATE INDEX idx_person_merge_operation_assignments_merge_id
  ON person_merge_operation_assignments(merge_operation_id, person_role, assignment_id);
```

字段语义：

- `merge_operation_id`：所属 `person_merge_operations.id`。
- `assignment_id`：参与本次 merge 的 active assignment，在 merge 前归属于 winner 或 loser。
- `person_role`：该 assignment 在 merge 前属于 `winner` 还是 `loser`。

运行时语义：

- 每次成功 merge 会把 merge 前 winner 与 loser 的 active assignment id 集合完整快照到该表。
- undo 会依赖这张表把 merge 前属于 loser 的 assignment 精确恢复给 loser；如果最近一次 merge 在这张表里的快照不完整，则 undo 必须失败并保持 DB 不变。

### 3.15 `export_template`

```sql
CREATE TABLE export_template (
  template_id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  output_root TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('active', 'invalid')),
  created_at TEXT NOT NULL,
  dedup_key TEXT NOT NULL UNIQUE
);
```

索引：

```sql
CREATE INDEX idx_export_template_status ON export_template(status, created_at);
```

字段语义：

- `template_id`：模板唯一标识，UUIDv4。
- `name`：用户填写的模板名称，trim 后保存。
- `output_root`：绝对路径，创建模板时系统会尝试创建该目录。
- `status`：`active` 表示模板有效；`invalid` 表示模板关联的某个人物已失效（inactive 或 display_name 被清空）。
- `created_at`：创建时间，带 `Z` 后缀的 ISO-8601 UTC 时间字符串。

运行时语义：

- 模板保存后不可编辑；`name` 不参与去重键。
- 去重键为 `person_ids` 排序后 + `output_root` 组合。
- 当关联的 `person_id` 变为 `inactive` 或 `display_name` 变为 `NULL` 时，级联更新为 `invalid`。

### 3.16 `export_template_person`

```sql
CREATE TABLE export_template_person (
  template_id TEXT NOT NULL REFERENCES export_template(template_id),
  person_id TEXT NOT NULL REFERENCES person(id),
  PRIMARY KEY (template_id, person_id)
);
```

索引：

```sql
CREATE INDEX idx_export_template_person_person_id ON export_template_person(person_id, template_id);
```

字段语义：

- `template_id`：所属导出模板。
- `person_id`：快照保存时关联的人物 ID；不保存 `display_name`，防止重命名后语义漂移。

运行时语义：

- 创建模板时记录所选人物的 `person_id` 快照。
- 模板至少关联 2 个 active 且已命名的人物。

### 3.17 `export_run`

```sql
CREATE TABLE export_run (
  run_id INTEGER PRIMARY KEY AUTOINCREMENT,
  template_id TEXT NOT NULL REFERENCES export_template(template_id),
  status TEXT NOT NULL CHECK (status IN ('running', 'completed', 'failed')),
  started_at TEXT NOT NULL,
  completed_at TEXT,
  copied_count INTEGER NOT NULL DEFAULT 0,
  skipped_count INTEGER NOT NULL DEFAULT 0
);
```

索引：

```sql
CREATE INDEX idx_export_run_template_id ON export_run(template_id, run_id);
CREATE INDEX idx_export_run_status ON export_run(status);
```

字段语义：

- `run_id`：运行记录自增 ID。
- `template_id`：关联的导出模板。
- `status`：`running` / `completed` / `failed`。
- `started_at`：启动时间。
- `completed_at`：完成时间；失败时也写入。
- `copied_count` / `skipped_count`：实际复制/跳过的静态图数量（MOV 不计入）。

运行时语义：

- 任何 `export_run` 处于 `running` 状态时，全局锁定人物写操作（命名、合并、撤销合并、排除）。
- 服务启动时若存在残留 `running` 记录，自动标记为 `failed` 以解除锁定。
- 不允许并发执行多个导出运行。

### 3.18 `export_plan`

```sql
CREATE TABLE export_plan (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  template_id TEXT NOT NULL REFERENCES export_template(template_id),
  asset_id INTEGER NOT NULL REFERENCES assets(id),
  bucket TEXT NOT NULL,
  month TEXT NOT NULL,
  file_name TEXT NOT NULL,
  mov_file_name TEXT,
  source_label TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  UNIQUE(template_id, asset_id)
);
```

索引：

```sql
CREATE INDEX idx_export_plan_template_bucket_month
  ON export_plan(template_id, bucket, month, file_name);
```

字段语义：

- `template_id`：关联的导出模板。
- `asset_id`：被导出的资产。
- `bucket`：人物名称桶，用于目标目录结构。
- `month`：拍摄月份，`YYYY-MM` 格式。
- `file_name`：最终目标文件名，含冲突消解后缀。
- `mov_file_name`：配对 MOV 文件的最终目标文件名；无 MOV 配对时为 `NULL`。
- `source_label`：来源目录标签，用于同名文件冲突消解。
- `created_at`：记录创建时间。

运行时语义：

- `compute_export_preview` 预览时写入，`(template_id, asset_id)` 唯一约束保证幂等 upsert。
- 同一模板下不同源目录的同名文件通过 `__<source_label>` 后缀消解冲突；同标签时追加 `-N` 数字后缀。
- `execute_export` 从 `export_plan` 读取计划，不再重新计算预览。

### 3.19 `export_delivery`

```sql
CREATE TABLE export_delivery (
  delivery_id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id INTEGER NOT NULL REFERENCES export_run(run_id),
  asset_id INTEGER NOT NULL REFERENCES assets(id),
  target_path TEXT NOT NULL,
  result TEXT NOT NULL CHECK (result IN ('copied', 'skipped_exists')),
  mov_result TEXT NOT NULL CHECK (mov_result IN ('copied', 'skipped_missing', 'not_applicable')),
  plan_id INTEGER REFERENCES export_plan(id)
);
```

索引：

```sql
CREATE INDEX idx_export_delivery_run_id ON export_delivery(run_id, asset_id);
```

字段语义：

- `run_id`：所属导出运行。
- `asset_id`：被处理的资产。
- `target_path`：目标文件路径。
- `result`：静态图复制结果。
- `mov_result`：MOV 配对复制结果。
- `plan_id`：关联的 `export_plan` 记录；由 `execute_export` 写入。

运行时语义：

- 每个被处理的 asset 产生一条记录。
- 目标文件已存在时记 `skipped_exists`，不覆盖。
- MOV 缺失或不可读时记 `skipped_missing`。
- JPG/PNG 等无 MOV 配对时记 `not_applicable`。

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

- 导出运行期间的人物写操作全局锁定（Slice G / Feature Slice 3）的具体运行时语义与 API 契约

备注：Slice G / Feature Slice 2（导出模板预览、执行与导出历史）已在当前实现中落地，其运行时语义与 API 契约见上文 `export_run`、`export_delivery` 以及对应 Public Interface 描述。
