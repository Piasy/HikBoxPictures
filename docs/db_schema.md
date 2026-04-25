# 数据库 Schema 说明（当前已实现）

本文档只描述当前仓库已经落地并经自动化验证的最小 schema 契约。截止目前，已实现 Slice A / Feature Slice 1「初始化工作区」和 Feature Slice 2「登记和列出源目录」。

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

运行时行为：

- `hikbox source add --workspace <path> <source-path> --label <label>` 会向该表插入一条记录。
- `path` 写入解析后的源目录绝对路径，并受 `UNIQUE` 约束保护；重复登记同一路径必须失败，且不会覆盖原 `label`。
- `label` 写入用户传入的标签；空白标签不会入库。
- `active` 当前固定写入 `1`，由 `hikbox source list` 映射为 JSON 布尔值 `true`。
- `created_at` 写入带 `Z` 后缀的 ISO-8601 UTC 时间字符串。
- `hikbox source list --workspace <path>` 按 `id ASC` 读取该表，并输出如下 JSON 结构：

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

没有任何 source 时，stdout 精确输出 `{"sources": []}`。

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

## 6. source 操作日志

`hikbox source add` 成功后，`external_root/logs/source.log.jsonl` 会追加一条 JSON Lines 日志。当前最小字段包括：

- `timestamp`
- `command`
- `workspace`
- `source_path`
- `label`
- `result`

当前实现只要求 `source add` 成功落盘日志；`source list` 成功通过 stdout JSON 暴露结果，失败则通过 stderr 输出可读错误。

## 7. 未在本文承诺的内容

以下内容尚未在当前实现中落地，因此不属于本文档承诺范围：

- 扫描、产物生成、人物归属、WebUI、导出
- 任何 `schema_version > 1` 的 migration 规则
