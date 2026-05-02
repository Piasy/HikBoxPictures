# 增量 Scan Spec

## Goal

再次 scan 时，已扫描干净的源零文件系统操作直接跳过，有失败重试的源从 DB 精准取出重试候选，只有新源才遍历目录；同时去掉 plan_fingerprint 机制、清理 migration 历史债务。

## Global Constraints

- 核心行为通过真实 CLI + SQLite + artifact 验证；mock/stub/no-op 路径不得满足验收
- `library_v1.sql` 作为完整初始建表 DDL
- `docs/db_schema.md` 同步更新
- 已有 fixture `tests/fixtures/people_gallery_scan/`（含 1 个损坏图片 pg_902_corrupt.jpg）和 `tests/fixtures/people_gallery_scan_2/`（15 张全部正常）作为验收图片基线
- 新增测试优先复用 `scanned_workspace` / `copy_scanned_workspace`

## Feature Slice 1: 清理 migration 历史债务并扩展 schema

- [ ] Implementation status: Not done

### Behavior

- `library_v1.sql` 重写为完整初始建表 DDL：
  - 原有 v1 所有表
  - `export_plan` 表 + `export_delivery.plan_id` 列（原 library_v3.sql 内容）
  - `library_sources` 新增 `scan_state TEXT NOT NULL DEFAULT 'pending'`
  - `assets` 新增 `scan_retry_count INTEGER NOT NULL DEFAULT 0`
  - `scan_sessions` 表去掉 `plan_fingerprint` 列
- 删除 `library_v2.sql`（空占位）和 `library_v3.sql`
- `docs/db_schema.md` 更新：去掉 `plan_fingerprint` 及其文档说明；增加 `scan_state`、`scan_retry_count` 列文档；删去 v2/v3 migration 相关描述
- [export-plan-db-migration-spec](./2026-04-30-export-plan-db-migration-spec.md) 更新：去掉 "v1 不可变基线"、"不修改 library_v1.sql" 等声明；v2/v3 文件名引用改为已在 v1 中包含

### Public Interface

- 无新增 CLI/API
- DB migration 文件变更（v1 重写，v2/v3 删除）

### Error and Boundary Cases

- `init` 新建工作区时 `schema_version = 1`，无需执行后续 migration
- 不保证已有 v3 工作区兼容

### Non-goals

- 不修改 `embedding_v1.sql`
- 不改变 migration 自动执行机制本身

### Acceptance Criteria

#### Shared Verification Baseline

- 主路径：`init` 新工作区 → 检查 DB schema
- 默认断言层级：DB 表/列/索引存在性 + `schema_version` 值
- 防 mock 逃逸禁令：不得绕过 `init` CLI 直接构造 DB 文件
- 调试产物保留到 `.tmp/incremental-scan/`

#### AC-1: init 新工作区获得完整 schema

- 触发：`init --workspace <new> --external-root <new>`
- 必须可观察：`library.db` 的 `schema_version = 1`；`export_plan` 表存在；`export_delivery` 有 `plan_id` 列；`library_sources` 有 `scan_state` 列；`assets` 有 `scan_retry_count` 列；`scan_sessions` 不含 `plan_fingerprint` 列
- 验证手段：DB 断言

#### AC-2: v2.sql 和 v3.sql 已删除

- 触发：检查文件系统 `hikbox_pictures/product/db/sql/`
- 必须可观察：`library_v2.sql` 和 `library_v3.sql` 不存在；`library_v1.sql` 和 `embedding_v1.sql` 存在
- 验证手段：文件系统断言

#### AC-3: 已有 backend 测试全部通过

- 触发：`./scripts/run_tests.sh --scope backend`
- 必须可观察：所有 backend 测试通过（含因 plan_fingerprint 移除和 schema 变更而更新的测试）
- 验证手段：CI 自动化

### Done When

- AC-1、AC-2、AC-3 通过
- `docs/db_schema.md` 已更新
- [export-plan-db-migration-spec](./2026-04-30-export-plan-db-migration-spec.md) 已更新
- `.tmp/golden-workspace/` 已删除（schema 变更后重建，确保 `copy_scanned_workspace` 拿到新 schema workspace）

## Feature Slice 2: 源级别 scan 状态与增量候选发现

- [ ] Implementation status: Not done

### Behavior

**源 scan 状态：**

- `library_sources.scan_state` 三态：`pending`（未扫描）、`scanned_clean`（无待重试）、`scanned_with_retries`（有待重试）
- `source add` 时默认 `scan_state = 'pending'`
- 每次 scan session 完成后，对各参与源重算 scan_state：该源下存在 `processing_status = 'failed' AND scan_retry_count < 3` → `scanned_with_retries`；否则 → `scanned_clean`

**重试计数：**

- `_upsert_asset()` 中 `processing_status = 'failed'` 时 `scan_retry_count` 在已有值上 +1
- 最大重试次数硬编码为 3

**去掉 plan_fingerprint：**

- `_compute_plan_fingerprint()` 函数删除
- `_ensure_scan_session()` 不再做指纹去重，每次 scan 都创建新 session
- `_load_resumable_session()` 仅靠 `status = 'running'` + 存在未完成批次来恢复，不依赖指纹
- 日志事件中不再输出 `plan_fingerprint` 字段

**候选发现（`_discover_candidates()` 重构）：**

- `scanned_clean` 源 → 跳过，零文件系统操作
- `scanned_with_retries` 源 → 从 assets 表 `WHERE source_id = ? AND processing_status = 'failed' AND scan_retry_count < 3` 直接构造候选，不遍历目录、不重算指纹/EXIF/mov；候选需验证文件仍存在于磁盘
- `pending` 源 → 遍历目录，计算指纹/EXIF/mov（与现有逻辑一致）
- 各源候选合并后统一排序返回

### Public Interface

- CLI：`hikbox-pictures scan start` — 候选发现逻辑改变，进度中的总数只包含真正需要处理的文件
- CLI：`hikbox-pictures source add` — 新增行为（设置 `scan_state = 'pending'`），对外接口不变
- WebUI、serve、export 等不受影响

### Error and Boundary Cases

- 所有源均为 `scanned_clean` → candidates 为空，报 "没有可扫描照片"
- 损坏文件连续失败 3 次 → `scan_retry_count = 3`，不再被后续 scan 选中；源内若无其他待重试文件则状态变为 `scanned_clean`
- `scanned_with_retries` 源从 DB 取出的候选文件已被外部删除 → worker 处理失败，正常递增 retry_count

### Non-goals

- 不检测已扫描源中新增/删除/修改的文件（用户承诺源目录内容不变）
- 不提供手动重置源 scan 状态的 CLI
- 不改变 batch 处理、worker、online assignment 的内部逻辑

### Acceptance Criteria

#### Shared Verification Baseline

- 主路径：基于 `copy_scanned_workspace`（`people_gallery_scan/` 全量扫描基线，含 1 个 failed asset：pg_902_corrupt.jpg），通过真实 CLI 触发 scan
- 默认断言层级：CLI 退出码 + stderr + DB 行/列/状态断言
- 防 mock 逃逸禁令：不得直接 `UPDATE library_sources SET scan_state` 或 `UPDATE assets SET scan_retry_count` 模拟状态变更
- 调试产物保留到 `.tmp/incremental-scan/`

#### AC-1: 已扫描基线再次 scan — 只从 DB 取重试候选，零目录遍历

- 触发：对 `copy_scanned_workspace`（1 个源，51 个候选文件：50 个成功 + 1 个损坏 pg_902_corrupt.jpg）执行 `scan start`
- 必须可观察：不遍历源目录（零文件系统调用如 `iterdir`、零指纹计算）；直接从 assets 表查出 1 个 `failed AND retry_count < 3` 的候选；新 session 只有 1 个 batch_item（pg_902_corrupt.jpg）；处理失败后 retry_count 从 1 递增为 2（golden workspace 首次扫描已将 retry_count 从 0 递增为 1）；face_observations、face_embeddings、artifacts 行数/文件数不变
- 验证手段：改进已有测试 `test_scan_start_is_idempotent_after_completed_scan`，增加 `os.scandir`/`pathlib.Path.iterdir` 调用计数断言（零调用），以及 DB session 数/batch_item 数/scan_state/retry_count 断言

#### AC-2: 新增 pending 源全量扫描、已有源不受影响

- 触发：`copy_scanned_workspace` → `source add people_gallery_scan_2/`（15 张正常照片，全部会成功）→ `scan start`
- 必须可观察：新源全量遍历目录（15 个文件），旧源只从 DB 取 1 个重试候选；总候选 = 16；scan 后新源 `scan_state = 'scanned_clean'`，旧源 `scan_state = 'scanned_with_retries'`（损坏文件又失败一次）；旧源已有 48 个成功 asset 的 face_observations、embeddings、artifacts 不受影响
- 验证手段：服务级集成测试，DB 断言 batch_items 来源分布、scan_state、已有 assets 行数不变

#### AC-3: 重试上限生效

- 触发：同一损坏文件连续失败 3 次后（`scan_retry_count = 3`），再次 `scan start`
- 必须可观察：该文件不再出现在 batch_items 中；所属源 `scan_state` 变为 `scanned_clean`（无其他待重试文件）
- 验证手段：DB 断言 batch_items 不含该文件、scan_state 变更

#### AC-4: scanned_clean 源零开销

- 触发：以 `people_gallery_scan_2/` 为源完成 `init → source add → scan start`（15 张全部成功 → `scanned_clean`）→ 再次 `scan start`
- 必须可观察：零文件系统操作（`os.scandir`/`pathlib.Path.iterdir` 零调用、零指纹计算）；candidates 为空；stderr 含 "没有可扫描照片"
- 验证手段：服务级集成测试，文件系统调用计数断言（零）+ DB 断言无新 session 的 batch_items 写入

### Done When

- 所有 AC 通过自动化验证
- 没有通过直接修改 DB 或 mock 满足的验收条件
