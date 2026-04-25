# 数据库 Schema 说明（当前已实现）

本文档只描述当前仓库已经落地并经自动化验证的最小 schema 契约。截止目前，仅实现了 Slice A / Feature Slice 1「初始化工作区」。

## 1. 存储布局

```text
<workspace>/
  .hikbox/
    config.json
    library.db
    embedding.db

<external_root>/
  artifacts/
    crops/
    context/
  logs/
```

约定：

- 所有路径都由 `hikbox init --workspace <path> --external-root <path>` 创建。
- `config.json` 以 UTF-8 JSON 保存，`external_root` 持久化为绝对路径。
- `library.db` 和 `embedding.db` 都使用 SQLite。
- 当前还没有 migration；`schema_version=1` 仅支持空工作区初始化。

## 2. `config.json`

初始化成功后，`workspace/.hikbox/config.json` 的最小结构如下：

```json
{
  "config_version": 1,
  "external_root": "/absolute/path/to/external-root"
}
```

说明：

- `config_version` 是数字 `1`。
- `external_root` 必须是本次命令解析后的绝对路径。

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
- `created_at`：带 `Z` 后缀的 ISO-8601 UTC 字符串，例如 `2026-04-24T00:00:00Z`。

说明：

- 当前 slice 只负责在初始化时创建该表和约束，不负责写入 source 数据。
- `path TEXT NOT NULL UNIQUE` 是后续 `source add` 的全局唯一约束基础。

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

## 5. 初始化日志

初始化成功后，`external_root/logs/` 下会写入初始化日志文件。当前实现使用 JSON Lines，单条记录至少包含：

- `timestamp`
- `command`
- `workspace`
- `external_root`
- `result`

初始化失败时不会为了记录失败额外创建 `external_root/logs` 半成品。

## 6. 未在本文承诺的内容

以下内容尚未在当前实现中落地，因此不属于本文档承诺范围：

- `source add/list` 的运行时写入行为
- 扫描、产物生成、人物归属、WebUI、导出
- 任何 `schema_version > 1` 的 migration 规则
