# Immich v6 人物图库产品化 Slice A：工作区与源目录 Spec

## Goal

为 Immich v6 人物图库产品化提供从零建库的本地工作区：用户可以通过 CLI 初始化 `workspace/.hikbox`，登记一个或多个照片源目录，并用公共命令确认已登记 source；失败场景必须可读、幂等且不破坏已有数据。

## Global Constraints

- 本 spec 是产品化 Slice A，可独立实现和验收；后续扫描、人物归属、WebUI 和导出不属于本 spec。
- 所有行为必须通过 CLI 公共入口触发；测试不得直接调用内部 helper 来代替 CLI。
- 核心行为必须观察真实文件系统和真实 SQLite；mock/stub/no-op 路径不得满足验收。
- 所有 CLI 命令都必须显式传入 `--workspace <path>`。
- 初始化只支持全新工作区；发现目标工作区已有 `workspace/.hikbox/`、`workspace/.hikbox/library.db`、`workspace/.hikbox/embedding.db` 或 `workspace/.hikbox/config.json` 时直接失败，不复用、不迁移。
- `config.json` 的 `external_root` 必须保存为绝对路径。
- 本 spec 固定最小持久化契约；后续 slice 可以新增字段或表，但不得破坏本 spec 定义的字段名、类型和语义。
- 任何数据库 schema 修改都必须同步更新 `docs/db_schema.md`。

### 持久化契约

`workspace/.hikbox/config.json` 必须是 UTF-8 JSON，最小结构如下：

```json
{
  "config_version": 1,
  "external_root": "/absolute/path/to/external-root"
}
```

`library.db` 和 `embedding.db` 都必须包含 `schema_meta` 表：

```sql
CREATE TABLE schema_meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
```

两个 DB 都必须写入 `schema_meta('schema_version', '1')`。`library.db` 必须包含 `library_sources` 表：

```sql
CREATE TABLE library_sources (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  path TEXT NOT NULL UNIQUE,
  label TEXT NOT NULL,
  active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
  created_at TEXT NOT NULL
);
```

`library_sources.path` 保存绝对路径；`library_sources.active` 使用 `1` 表示 active，`0` 表示 inactive；`created_at` 使用带 `Z` 后缀的 ISO-8601 UTC 字符串，例如 `2026-04-24T00:00:00Z`。

`hikbox source list --workspace <path>` 默认按 `library_sources.id ASC` 输出 JSON 到 stdout，格式如下：

```json
{
  "sources": [
    {
      "id": 1,
      "label": "family",
      "path": "/absolute/path/to/source",
      "active": true,
      "created_at": "2026-04-24T00:00:00Z"
    }
  ]
}
```

没有 source 时输出 `{"sources": []}` 并以 0 退出。

## Feature Slice 1: 初始化工作区

- [x] Implementation status: Done

### Behavior

- `hikbox init --workspace <path> --external-root <path>` 创建新的 `workspace/.hikbox/`。
- 初始化创建 `workspace/.hikbox/config.json`、`workspace/.hikbox/library.db`、`workspace/.hikbox/embedding.db`。
- `config.json` 按“持久化契约”保存 `config_version=1` 和绝对路径形式的 `external_root`。
- `library.db` 与 `embedding.db` 按“持久化契约”写入 `schema_meta('schema_version', '1')`。
- `library.db` 创建 `library_sources` 表和 `path` 唯一约束。
- 初始化创建 `external_root/artifacts/crops`、`external_root/artifacts/context`、`external_root/logs`。
- 初始化成功后写入一条可追踪日志，记录命令、workspace、external_root、结果和时间。

### Public Interface

- CLI：`hikbox init --workspace <path> --external-root <path>`。
- 文件：`workspace/.hikbox/config.json`。
- DB：`workspace/.hikbox/library.db`、`workspace/.hikbox/embedding.db`。
- 目录：`external_root/artifacts/crops`、`external_root/artifacts/context`、`external_root/logs`。
- 日志：`external_root/logs` 下的初始化日志文件或等价追加日志。

### Error and Boundary Cases

- `--workspace` 缺失时返回非 0 退出码和可读参数错误。
- `--external-root` 缺失时返回非 0 退出码和可读参数错误。
- `external_root` 无法创建、不可写或初始化中途失败时返回非 0 退出码，只通过 stderr 报告失败；不得留下 `workspace/.hikbox/`、`config.json`、`library.db`、`embedding.db`、`external_root/artifacts`、`external_root/artifacts/crops`、`external_root/artifacts/context`、`external_root/logs` 或其他本次 init 创建的半成品初始化产物。
- 目标工作区已有 `workspace/.hikbox/`、`workspace/.hikbox/library.db`、`workspace/.hikbox/embedding.db` 或 `workspace/.hikbox/config.json` 时返回非 0 退出码，不覆盖、不删除、不迁移已有文件。
- `workspace` 或 `external_root` 使用相对路径传入时，持久化配置和 DB 中只保存解析后的绝对路径。

### Non-goals

- 不做旧数据导入、自动迁移或 schema 升级。
- 不登记 source，不扫描照片，不生成 asset/face/embedding。
- 不做磁盘空间预检测、配额管理或自动清理。

### Acceptance Criteria

- AC-1：在真实临时目录执行 `hikbox init --workspace <ws> --external-root <ext>` 后，`.hikbox/config.json`、`library.db`、`embedding.db`、`artifacts/crops`、`artifacts/context`、`logs` 都存在。
- AC-2：使用相对路径形式传入 `--workspace` 和 `--external-root` 执行 init 后，读取 `config.json` 可见 `config_version` 为数字 `1`，`external_root` 是解析后的绝对路径，且指向本次命令指定的目录。
- AC-3：读取 `library.db` 和 `embedding.db` 可见 `schema_meta('schema_version', '1')`；读取 `library.db` 可见 `library_sources` 表、`path` 唯一约束和 `active` 取值约束。
- AC-4：重复执行同一 `init` 返回非 0 退出码，已有 `config.json`、两个 DB 和日志内容不被覆盖或删除。
- AC-5：缺少 `--workspace`、缺少 `--external-root`、不可写 `external_root` 都返回非 0 退出码和可读错误；不可写或中途失败后不得留下 `workspace/.hikbox/`、任何 DB/config 半成品、`external_root/artifacts` 或 `external_root/logs`。
- AC-6：初始化成功必须在 `external_root/logs` 落盘可追踪日志；初始化失败必须在 stderr 输出可追踪错误，且不得为了记录失败而创建 `external_root/logs` 半成品。

### Automated Verification

- 新增 CLI 集成测试，在 pytest 临时目录中通过 subprocess 执行 `hikbox init`，断言退出码、stdout/stderr、文件系统、JSON 内容和 SQLite 元数据。
- AC-1、AC-2、AC-3 由成功初始化测试覆盖；AC-2 的测试必须从临时工作目录用相对 `--workspace` 和相对 `--external-root` 调用 CLI，并断言持久化路径为绝对路径；AC-3 直接查询 `schema_meta` 和 `library_sources` 的公开 schema 契约。
- AC-4 由重复初始化测试覆盖，测试先记录文件内容或 mtime，再断言重复 init 后未被覆盖。
- AC-5 由参数缺失和不可写目录测试覆盖；不可写目录可在非 Windows 环境用权限位构造，若 CI 环境不支持权限失败，则使用已存在普通文件作为 `external_root` 父级冲突来触发真实文件系统错误，并断言失败后不存在 `.hikbox`、DB、config、`external_root/artifacts`、`external_root/artifacts/crops`、`external_root/artifacts/context` 或 `external_root/logs` 半成品。
- AC-6 由成功日志落盘断言和失败 stderr 断言覆盖；失败场景同时断言不会为了记录失败创建 `external_root/logs`。
- 测试必须只通过 CLI 入口执行，不能直接调用初始化函数、直接写 DB 或手工创建目标文件来满足验收。

### Done When

- 所有验收标准都通过自动化验证。
- `docs/db_schema.md` 已同步描述本 slice 引入或确认的 schema。
- 没有核心需求通过直接状态修改、硬编码数据、占位行为或 fake integration 满足。

## Feature Slice 2: 登记和列出源目录

- [x] Implementation status: Done

### Behavior

- `hikbox source add --workspace <path> <source-path> --label <label>` 将一个照片源目录登记到 `library.db`。
- source 持久化到 `library_sources`，保存绝对路径、用户给定 label、带 `Z` 后缀的 ISO-8601 UTC 创建时间和 active 状态。
- `hikbox source list --workspace <path>` 按“持久化契约”输出 JSON，包含 label、绝对路径、active 布尔值和创建时间，并按 `id ASC` 排序。
- 重复登记同一个源目录绝对路径必须失败，不产生第二条 source 记录。
- source add/list 必须读取现有 `workspace/.hikbox/config.json` 和 `library.db`；未初始化工作区不能隐式 init。
- source add 成功后必须落盘日志；source add/list 失败必须在 stderr 输出可读错误；source list 成功至少可通过 stdout JSON 追踪结果。

### Public Interface

- CLI：`hikbox source add --workspace <path> <source-path> --label <label>`。
- CLI：`hikbox source list --workspace <path>`。
- DB：`library.db` 中的 `library_sources` 表。
- 日志：`external_root/logs` 下的 source 操作日志文件或等价追加日志。

### Error and Boundary Cases

- `--workspace` 缺失时返回非 0 退出码和可读参数错误。
- 工作区未初始化、缺少 `config.json` 或缺少 `library.db` 时返回非 0 退出码，不创建新工作区。
- `source-path` 不存在、不可读或不是目录时返回非 0 退出码，不新增记录。
- `--label` 缺失或为空白时返回非 0 退出码，不新增记录。
- 重复 source 返回非 0 退出码，不新增记录，不改变原记录 label。
- `source list` 在没有 source 时输出 `{"sources": []}`，以 0 退出。

### Non-goals

- 不递归发现照片，不计算文件 fingerprint，不读取图片 metadata。
- 不做 source 删除、停用、重命名或跨 source 去重。
- 不支持远程 source、云盘鉴权或网络同步。

### Acceptance Criteria

- AC-1：在已初始化工作区执行 `hikbox source add --workspace <ws> <source> --label family` 后，`library_sources` 中存在一条记录：`path` 为 source 绝对路径，`label='family'`，`active=1`，`created_at` 是带 `Z` 后缀的 ISO-8601 UTC 字符串。
- AC-2：执行 `hikbox source list --workspace <ws>` 后，stdout 是合法 JSON，`sources[0]` 包含 `label='family'`、source 绝对路径、`active=true` 和同一条 `created_at`。
- AC-3：初始化后尚未登记 source 时执行 `hikbox source list --workspace <ws>`，stdout 是合法 JSON 且精确等于 `{"sources": []}`，退出码为 0。
- AC-4：连续登记两个不同 source 后，`library_sources` 中存在两条 active 记录；`source list` 返回两条记录，并按 `id ASC` 排序。
- AC-5：未初始化工作区执行 source add/list 都返回非 0 退出码和可读错误，且不会创建 `.hikbox`。
- AC-6：不存在路径、普通文件路径、不可读目录、空白 label 都返回非 0 退出码，不新增 source 记录。
- AC-7：重复登记同一 source 绝对路径返回非 0 退出码，`library_sources` 中仍只有一条记录，原 label 不变。
- AC-8：source add 成功必须落盘日志；source add/list 失败必须在 stderr 输出可读错误；source list 成功必须输出可解析 JSON。

### Automated Verification

- CLI 集成测试先执行本 spec 的 init 成功路径，再通过 subprocess 执行 source add/list，并读取 SQLite 断言 source 记录。
- AC-1、AC-2 由 source add/list happy path 测试覆盖，测试解析 stdout JSON 并查询 `library_sources`。
- AC-3 由空 source list 测试覆盖，测试在 init 后、source add 前执行 `source list`，断言 stdout JSON 精确为 `{"sources": []}` 且退出码为 0。
- AC-4 由多 source 测试覆盖，测试连续添加两个不同真实目录，断言 DB 记录数、stdout JSON 数量和 `id ASC` 顺序。
- AC-5 由未初始化工作区测试覆盖，并断言没有隐式创建 `.hikbox`。
- AC-6 由非法路径、普通文件、不可读目录和空白 label 测试覆盖；不可读目录如受 CI 权限限制，可用普通文件路径作为稳定替代，但必须仍通过真实 CLI 失败路径触发。
- AC-7 由重复 source 测试覆盖，断言记录数和原 label。
- AC-8 由 source add 成功日志落盘断言、source list 成功 JSON 解析断言和失败 stderr 断言覆盖。
- 测试不得直接插入 source 记录或直接调用内部存储函数来满足验收。

### Done When

- 所有验收标准都通过自动化验证。
- `docs/db_schema.md` 已同步描述 source 相关 schema、唯一约束和字段语义。
- 没有核心需求通过直接状态修改、硬编码数据、占位行为或 fake integration 满足。

## Cross-Slice Verification

- 本 spec 完成后，必须存在一个端到端 CLI 集成测试链路：`init -> source add -> source list`。
- 该链路必须在真实临时目录中运行，读取真实 `config.json` 和 SQLite 验证结果。
- 该链路是后续扫描 slice 的前置验收证据；后续 slice 不得重新定义 workspace/source 的产品语义。
