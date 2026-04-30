# 导出计划持久化、同名冲突消解与 DB Migration Spec

## Goal

通过 DB 持久化的导出计划（`export_plan`）替代当前纯内存的 preview + execute 流程，在 preview 阶段解决同名文件冲突，使 execute 阶段变成幂等的按计划搬运文件；同时建设 DB migration 基础设施，使 `library.db` 和 `embedding.db` 的 schema 变更可自动升级。

## Global Constraints

- 核心行为必须通过公共入口验证；mock/stub/no-op 路径不得满足验收。
- 任何数据库 schema 修改都必须同步更新 `docs/db_schema.md`。
- 新增 DB 表或字段时必须走 migration SQL 文件，不得直接修改 v1 全量建表脚本。
- `library_v1.sql` 和 `embedding_v1.sql` 为不可变基线，始终保持初始建表 DDL。
- `export_plan` 记录仅追加（upsert），永不删除；模板 invalid 后 plan 记录保留。
- 冲突消解不使用 source_label 以外的文件属性（如 EXIF、文件哈希）。
- 模板保存后不可编辑的约束不变。

## Feature Slice 1: DB Migration 基础设施

- [ ] Implementation status: Not done

### Behavior
- `library.db` 和 `embedding.db` 各自通过 `schema_meta.schema_version` 独立追踪当前版本，各自独立执行 migration。
- Migration SQL 文件存放于 `hikbox_pictures/product/db/sql/`，命名规则为 `library_v{N}.sql` 和 `embedding_v{N}.sql`（N 为迁移目标版本号）。
- **`init` 命令**：检查 workspace 是否存在，若已存在则报错退出、不升级 DB；若不存在则创建新 workspace，先执行 v1 全量建表 SQL，再依次执行后续 migration SQL 升级至最新版本。
- **`init` 以外的所有命令**（`source add`、`source list`、`scan start`、`serve`）：打开 DB 连接后、执行业务逻辑前，自动按版本序号递增查找并执行后续 migration SQL，同一事务中更新 `schema_version`。任一 migration 失败则命令启动失败，事务回滚，`schema_version` 不变。
- 已是目标版本时零开销跳过，不执行任何 SQL。
- 新增 migration 时只需在 `hikbox_pictures/product/db/sql/` 下新增对应 SQL 文件，更新 `docs/db_schema.md`；migration 执行由自动机制完成，无需额外编写调用代码。

### Public Interface
- 公开入口：所有 CLI 命令的启动路径（对用户透明，无新增 CLI 参数或子命令）
- 新增文件：`hikbox_pictures/product/db/sql/library_v2.sql`（占位迁移，用于验证机制，不含实际 schema 变更；真实 DDL 由 Feature Slice 2 的 `library_v3.sql` 承载）
- DB：`schema_meta.schema_version` 递增
- 文档：`AGENTS.md` 和 `docs/db_schema.md` 包含 migration 机制说明
- 若当前无 embedding schema 变更，则无需创建 `embedding_v2.sql`；迁移机制仅在对应 SQL 文件存在时执行

### Error and Boundary Cases
- migration SQL 文件缺失或损坏 → 命令启动失败，报可读错误
- migration 执行中 DB 写入失败 → 事务回滚，`schema_version` 不变，命令启动失败
- workspace 不存在时 `init` 以外的命令 → 保持现有行为（报错退出），不尝试创建 workspace 也不执行 migration
- 已是目标版本（无需迁移）→ 零开销跳过

### Non-goals
- 不做回滚/downgrade migration
- 不做跨 DB 联动（两个 DB 各自独立）
- 不修改 `library_v1.sql` 和 `embedding_v1.sql`（v1 全量建表脚本保持不可变）

### Acceptance Criteria

#### Shared Verification Baseline
- 主路径：基于已有 workspace（`init -> source add -> scan start` 全套），通过 CLI 公开入口触发 automation
- 默认断言层级：CLI 退出码 + stderr + DB 断言
- 防 mock 逃逸禁令：不得直接 `UPDATE schema_meta.schema_version` 来模拟迁移结果；不得绕过 CLI 入口直接调用迁移函数模拟成功/失败

#### AC-1: 新建 workspace 获得最新 schema
- 触发：`init --workspace <new> --external-root <new>`
- 必须可观察：`library.db` 和 `embedding.db` 的 `schema_version` 均等于当前最新版本；`library.db` 中包含 v1 和后续 migration 版本（如 v2）的全部表和索引
- 验证手段：DB 断言 `schema_version` 值 + 表/索引存在性

#### AC-2: 已有旧版 workspace 执行任意非 init 命令自动 migration
- 触发：用 `schema_version=1` 的 workspace（保留旧版 fixture 或通过测试制造），分别执行 `source add`、`source list`、`scan start`、`serve` 四个命令
- 必须可观察：每个命令均正常启动（或正常完成）；`schema_version` 升级到最新；migration 新增表存在；原数据无损
- 验证手段：对四个命令分别做启动前后 DB 断言

#### AC-3: init 对已存在 workspace 报错不修改
- 触发：对已有 workspace 再次执行 `init`
- 必须可观察：CLI 退出码非 0，stderr 含可读错误；DB `schema_version` 不变，表结构不变
- 验证手段：退出码 + stderr + DB 断言

#### AC-4: migration 失败时命令失败且 DB 回滚
- 触发：放入语法错误的 migration SQL 文件，执行 `serve`
- 必须可观察：命令启动失败，`schema_version` 不变，无部分执行痕迹
- 验证手段：退出码 + DB 断言

#### AC-5: 已是目标版本时零开销
- 触发：在已是最新版本的 workspace 执行 `serve`
- 必须可观察：服务正常启动，DB 无变化
- 验证手段：服务响应正常 + DB 断言 `schema_version` 不变

### Done When
- 所有验收标准通过自动化验证。
- 没有核心需求是通过直接状态修改、硬编码数据、占位行为或 fake integration 满足的。

## Feature Slice 2: 导出计划持久化与同名冲突消解

- [ ] Implementation status: Not done

### Behavior

**Preview 阶段（写入 export_plan）：**
- `compute_export_preview` 计算命中照片后，在同一事务中写入 `export_plan` 表（全部成功或全部回滚）；自此 `GET /api/export-templates/{template_id}/preview` 成为读写端点（幂等：重复调用不改变已有 plan 记录）
- `export_plan` 以 `(template_id, asset_id)` 为唯一键，upsert 语义：已有记录不动，新命中 insert
- 每条 plan 记录包含：`template_id`、`asset_id`、`bucket`（`only`/`group`）、`month`、`file_name`（可能已被重命名）、`mov_file_name`（配对 MOV 的文件名，可能已被重命名）、`source_label`（记录来源 source label，用于审计）
- 按 `asset_id` 升序遍历新命中列表，逐条写入前对已持久化记录及当前批次已写入记录进行同名冲突检测：
  - 同模板、同 bucket、同 month、同 `file_name` 的不同 `asset_id` → 判定为冲突
  - 冲突解决：后续文件在 stem 后追加 `__<source_label>` 后缀（如 `IMG_0001__iPhone.jpg`）
  - 若追加后仍有冲突（两个源目录恰好同名），追加数字序号 `__<source_label>-2`；依此类推，按冲突数量递增序号（`-3`、`-4`…）
  - 首个写入的文件保持原名不变
- 冲突检测范围：已存在于 `export_plan` 中的记录 + 当前批次已按 `asset_id` 升序写入的记录（确保排序稳定、跨运行可重复）
- paired MOV 文件名与静态图同步重命名（如 `IMG_0001__iPhone.mov`）
- `source_label` 取自 `library_sources.label`，不进行字符转义

**Execute 阶段（从 plan 执行）：**
- `execute_export` 不再调用 `compute_export_preview`，改为读取 `export_plan` 中的记录
- 对每条 plan 记录：检查目标文件是否存在 → 存在则 skip（`skipped_exists`），不存在则 copy
- 写入 `export_delivery` 时新增 `plan_id` 字段关联到 `export_plan.id`
- 文件系统产物与现有行为一致（`output_root/<bucket>/<month>/<file_name>`）
- 配对 MOV 的复制逻辑不变，MOV 文件检测与 plan 中的 `mov_file_name` 一致

**WebUI 变更：**
- `/exports` 列表页移除每个模板 item 的"执行"操作入口
- 执行统一走预览页入口（`/exports/{template_id}/preview`）

### Public Interface
- API 不变：现有 `GET /api/export-templates/{template_id}/preview`、`POST /api/export-templates/{template_id}/execute` 等端点行为契约保持不变，内部实现切换为 plan 驱动
- 新增 DB 表：`export_plan`（字段含 `id`、`template_id`、`asset_id`、`bucket`、`month`、`file_name`、`mov_file_name`、`source_label`、`created_at`；UNIQUE(`template_id`, `asset_id`)）
- 修改 DB 表：`export_delivery` 新增 `plan_id` 列
- Schema migration：`library_v3.sql` 包含 `export_plan` 建表 DDL 和 `export_delivery` 新增列 DDL
- Web 页面：`/exports` 模板列表页移除"执行"入口按钮

### Error and Boundary Cases
- 模板 invalid 时预览/执行仍被拒绝（现有行为不变）
- 同一源目录产生同名文件 — 不可能出现（DB 中 `absolute_path` UNIQUE 约束），若出现则按序号后缀兜底
- `export_plan` 中记录的 asset 对应的源文件在执行时已被删除 → 沿用现有 `_copy_asset` 错误处理
- 目标文件已存在 → `skipped_exists`，不覆盖
- 已有导出运行处于 `running` 状态时，不允许执行（锁定机制不变）

### Non-goals
- 不做文件哈希去重（不依赖文件内容判断两份不同源的同名文件是否真的相同）
- 不支持用户自定义重命名规则
- 不提供 plan 记录的人工删除/编辑入口
- 不清理无效模板的 plan 记录
- 不改变现有导出分桶算法和月份目录逻辑
- 不修复 `export_delivery.mov_result` CHECK constraint 缺失 `skipped_exists` 的问题（已有代码质量 review 记录该风险，不在本 spec 范围；若后续单独修复需新增独立 migration）

### Acceptance Criteria

#### Shared Verification Baseline
- 主路径：在 Feature Slice 1 已通过的 workspace 上，基于 `tests/fixtures/people_gallery_scan/` 完成 `init -> source add -> scan start -> serve`，通过命名路径命名目标人物后创建模板；构造同名冲突场景需要额外一个包含同名文件的不同源目录
- 默认断言层级：HTTP/DB/文件系统/Playwright DOM
- 调试产物保留到 `.tmp/people-gallery-export-plan/`
- 防 mock 逃逸禁令：不得直接 `INSERT` 到 `export_plan`；不得伪造冲突条件；不得绕过真实 `POST /api/export-templates/{template_id}/execute` 入口；不得 mock 文件系统层

#### AC-1: Preview 写入 export_plan
- 触发：创建模板后访问 `GET /api/export-templates/{template_id}/preview`
- 必须可观察：`export_plan` 表有记录，`(template_id, asset_id)` 唯一；每条记录含 `template_id`、`asset_id`、`bucket`、`month`、`file_name`、`source_label`
- 验证手段：DB 断言行数与字段值

#### AC-2: 同名冲突自动加 source_label 后缀
- 触发：两个不同源有同名文件都命中同一模板同一 bucket/month，访问预览页
- 必须可观察：`export_plan` 中先写入的保持原名，后写入的 `file_name` 在扩展名前追加 `__<source_label>`（如 `IMG_0001__iPhone.jpg`）
- 验证手段：DB `export_plan.file_name` 断言不同 asset_id 对应不同 file_name

#### AC-3: source_label 相同时追加序号
- 触发：两个不同源但恰好同 label（通过测试 fixture 构造），产生同名冲突
- 必须可观察：后续文件追加 `__<source_label>-2`
- 验证手段：DB `export_plan.file_name` 断言
- 降级理由：产品场景中同 source label 概率极低，通过测试 fixture 构造触发条件

#### AC-4: MOV 文件名同步重命名
- 触发：HEIC 同名冲突导致静态图重命名
- 必须可观察：`export_plan.mov_file_name` 与静态图 stem 一致（如 `IMG_0001__iPhone.mov`）；执行后目标文件系统上 MOV 也使用重命名后的文件名
- 验证手段：DB + 文件系统断言

#### AC-5: Execute 从 plan 读取并关联 plan_id
- 触发：preview 后执行导出
- 必须可观察：`export_delivery` 每条记录 `plan_id` 指向对应 `export_plan.id`；目标文件系统产物与 plan 一致
- 验证手段：DB `export_delivery.plan_id` 断言 + 文件系统断言

#### AC-6: 目标文件已存在时跳过
- 触发：预置目标文件后执行导出
- 必须可观察：`result='skipped_exists'`，原文件内容/mtime 未变；其他文件正常导出
- 验证手段：文件 hash 对比 + DB `export_delivery.result` 断言

#### AC-7: 再次 preview 只追加新记录
- 触发：以 `tests/fixtures/people_gallery_scan/` 为基础完成 `init -> source add -> scan start -> serve`，创建模板并首次 preview；然后停止 `serve`，执行 `source add tests/fixtures/people_gallery_scan_2/ -> scan start`，再重启 `serve`，同模板再次 preview
- 必须可观察：原有 plan 记录不变（行数、字段值）；新命中从 `people_gallery_scan_2/` 产生并追加；总行数 = 旧命中 + 新命中；新增记录的 `asset_id` 归属 `people_gallery_scan_2/` 对应 source
- 验证手段：DB 行数前后对比 + source 归属断言

#### AC-8: 再次 execute 只导出目标文件不存在的
- 触发：第一次 execute 后手动删除其中几个目标文件，再次 execute
- 必须可观察：只复制被删文件；已存在的跳过
- 验证手段：文件系统 + DB 断言

#### AC-9: /exports 列表页移除执行入口
- 触发：访问 `/exports`
- 必须可观察：每个模板 item 的操作区不含"执行"按钮或链接；执行入口仅在预览页（`/exports/{template_id}/preview`）
- 验证手段：Playwright DOM 断言

#### AC-10: 执行仍可走预览页完成
- 触发：从 `/exports` → 预览页 → 点击执行
- 必须可观察：导出正确执行，303 重定向到历史页，历史页展示运行结果
- 验证手段：Playwright 端到端

### Done When
- 所有验收标准通过自动化验证。
- 没有核心需求是通过直接状态修改、硬编码数据、占位行为或 fake integration 满足的。
