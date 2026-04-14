# HikBox Pictures 本地 Workspace 与外部运行目录设计文档

## 目标

重定义 HikBox Pictures 的工作区布局，解决 `SQLite` 数据库放在网络挂载目录时容易出现锁冲突、无法稳定启用 `WAL` 的问题，同时保留大体积运行文件继续写入网络盘的能力。

本次设计的目标是：

- `workspace` 固定为本地目录，只保存数据库和配置。
- `artifacts`、`logs`、`exports` 统一写入一个显式配置的 `external_root`。
- 初始化后，后续所有命令仍然只需要传 `--workspace`。
- 不兼容旧布局，不做历史 `workspace` 迁移；改完后由用户重新初始化新的 `workspace`。
- `workspace` 与 `external_root` 允许相同，便于单机本地模式继续工作。

## 背景与问题

当前实现默认把所有状态都放进 `<workspace>/.hikbox/`。当 `workspace` 本身位于网络挂载目录，例如 AFP 挂载时，会出现两个问题：

- `library.db` 实际无法稳定切换到 `WAL`，即使代码尝试设置 `PRAGMA journal_mode=WAL`，运行时仍可能落回 `delete`。
- `scan` 过程中存在状态轮询和主扫描写事务并发访问数据库的场景；在 `delete` 模式下，读写冲突会触发 `database is locked`。

用户同时要求以下约束：

- 数据库必须落本地。
- 缩略图、人脸裁剪、ANN、日志、导出结果仍然要写网络盘。
- 不希望依赖机器本地的全局配置目录。
- 初始化后，后续日常命令仍然只传 `--workspace`。

因此，系统需要把“本地控制面状态”和“外部运行文件目录”显式拆开建模。

## 已选方案

本轮设计确定以下决策：

- `init` 必须显式传入 `--external-root`。
- `workspace` 语义调整为“本地控制面目录”，只保存 `library.db` 和配置文件。
- `external_root` 语义调整为“运行期文件根目录”，统一保存 `artifacts`、`logs`、`exports`。
- 初始化后，除 `init` 外，其他命令均只接收 `--workspace`，并从 `workspace/.hikbox/config.json` 读取 `external_root`。
- 不保留旧布局兼容逻辑；没有 `config.json` 的旧 `workspace` 直接视为非法。
- `workspace` 和 `external_root` 允许相同；当两者相同时，系统行为等价于使用单一本地目录承载数据库、配置和全部运行文件。

## 范围

范围内：

- 工作区路径模型重构。
- `init`、`serve`、`scan`、`logs`、`export` 等命令的路径解析调整。
- Web/API 启动时的路径注入方式调整。
- 运行期产物目录、日志目录、导出目录的统一重定向。
- 测试与 README 更新。

范围外：

- 旧 `workspace` 自动迁移。
- 历史数据库文件搬迁。
- 旧 CLI 参数兼容保留。
- 多机共享同一数据库。

## 路径模型

### 本地 workspace

`workspace` 必须指向本地目录。初始化后，本地目录布局固定为：

```text
<workspace>/
  .hikbox/
    library.db
    config.json
```

其中：

- `library.db` 是唯一的业务真相数据库。
- `config.json` 是工作区配置文件，至少记录配置版本和 `external_root`。

### 外部运行目录

`external_root` 是显式配置的统一外部根目录，运行期文件布局固定为：

```text
<external_root>/
  artifacts/
    thumbs/
    face-crops/
    ann/
    context/
  logs/
    app.log
    runs/
  exports/
```

约束如下：

- 所有可重建或面向运行的文件统一归入 `external_root`。
- 不再允许部分功能偷偷回退到 `workspace/.hikbox/` 下找 `artifacts`、`logs` 或 `exports`。
- `library_source.root_path` 继续独立存在，可以指向任意照片源目录，与 `external_root` 无强绑定关系。

## 配置文件

配置文件位于 `<workspace>/.hikbox/config.json`，首版结构定义为：

```json
{
  "version": 1,
  "external_root": "/absolute/path/to/external-root"
}
```

配置要求：

- `external_root` 必须写入绝对路径。
- 运行时统一先读取 `config.json`，再解析完整 `WorkspacePaths`。
- 若 `config.json` 缺失、损坏、字段缺失或 `external_root` 不可访问，命令直接失败，并提示用户重新执行 `init`。

## CLI 行为

### init

`init` 调整为：

```bash
PYTHONPATH=src python3 -m hikbox_pictures.cli init \
  --workspace <local-workspace> \
  --external-root <external-root>
```

执行行为：

- 创建本地 `workspace/.hikbox/library.db`
- 创建本地 `workspace/.hikbox/config.json`
- 创建 `external_root/artifacts`
- 创建 `external_root/logs`
- 创建 `external_root/exports`
- 对本地数据库执行迁移

### 其他命令

除 `init` 外，所有命令继续只接收 `--workspace`：

- `source add|list|remove`
- `serve`
- `scan`
- `scan status`
- `scan abort`
- `scan new`
- `rebuild-artifacts`
- `export run`
- `logs tail`
- `logs prune`

统一行为：

- 先从本地 `workspace` 解析 `db_path` 和 `config_path`
- 读取 `config.json`
- 生成完整 `WorkspacePaths`
- 再继续后续数据库连接、日志目录、导出目录和 artifact 目录操作

## 内部数据结构

`WorkspacePaths` 需要扩展为显式描述完整路径集合，至少包括：

- `root`
- `db_path`
- `config_path`
- `external_root`
- `artifacts_dir`
- `logs_dir`
- `exports_dir`

设计原则：

- 所有服务层和 API 层都应从 `WorkspacePaths` 获取路径。
- 禁止继续通过 `db_path.parent.parent`、`PRAGMA database_list` 等方式反推 `workspace` 或 artifact 目录。
- 路径解析必须集中在 workspace/runtime 层，避免路径逻辑分散到业务服务内部。

## 服务与代码边界调整

### workspace/runtime 层

需要把当前“创建目录并顺便得到所有路径”的实现拆成两个明确入口：

- 初始化入口：显式接收 `workspace` 和 `external_root`，写入配置并创建目录。
- 加载入口：只接收 `workspace`，读取 `config.json` 并返回 `WorkspacePaths`。

这样可以把“首次建 workspace”和“日常使用 workspace”两条路径区分开，避免在运行时隐式补齐目录导致行为不清晰。

### CLI 与 API

CLI 和 `create_app()` 必须统一依赖加载后的 `WorkspacePaths`，而不是只保存一个 `db_path`：

- CLI 中所有命令通过 `WorkspacePaths` 连接 DB、读取日志、定位导出目录。
- API `app.state` 不再只保存 `db_path`，而是保存足够的路径信息，确保路由、预览服务、日志服务、导出服务都使用同一份路径配置。

### 扫描与产物生成

扫描和产物构建相关服务必须停止从数据库路径反推运行目录。首版至少要覆盖以下场景：

- 人脸裁剪输出目录
- 上下文图输出目录
- ANN 索引文件目录
- 缩略图目录
- 导出结果目录
- 结构化运行日志目录

实现要求：

- 这些服务要么直接接收 `WorkspacePaths`，要么接收其派生出的明确目录参数。
- 不允许在服务内部自行假设 `artifacts` 与 `library.db` 位于同一父目录树。

## 错误处理

### 配置错误

以下情况直接失败，不做隐式兼容：

- `workspace/.hikbox/config.json` 不存在
- `config.json` 不是合法 JSON
- `version` 不受支持
- `external_root` 缺失或为空
- `external_root` 无法创建或不可访问

错误信息应明确指出当前 `workspace` 非新布局，并提示重新运行 `init --external-root ...`。

### 运行期目录不可用

当 `external_root` 存在但运行期不可访问时：

- `scan`、`rebuild-artifacts`、`export run`、`serve` 均应快速失败
- 错误信息必须指明具体目录和失败动作，例如 `无法写入 external_root/logs`

### workspace 与外部目录同路径

当 `workspace` 与 `external_root` 恰好相同：

- 视为合法配置
- 系统不做额外告警
- 行为等价于“数据库和配置位于 `<workspace>/.hikbox/`，运行文件位于 `<workspace>/` 下”

## 测试策略

至少新增或调整以下测试：

- `init` 未传 `--external-root` 时失败
- `init` 生成本地 `library.db` 与 `config.json`
- `config.json` 正确写入绝对路径 `external_root`
- 日常命令能仅凭 `--workspace` 解析出完整路径
- `scan` 产物写入 `external_root/artifacts`
- `logs` 写入 `external_root/logs`
- `export` 写入 `external_root/exports`
- `external_root == workspace` 时仍然全部通过
- 缺失或损坏 `config.json` 时明确失败

## README 与用户文档

README 需要同步更新以下内容：

- `init` 新参数 `--external-root`
- `workspace` 语义变更为本地控制面目录
- `external_root` 语义变更为运行文件根目录
- 一个典型示例：
  - `workspace` 在本地 SSD
  - `external_root` 在网络盘
  - `library_source` 继续指向网络照片源目录

## 实施建议

实施顺序建议为：

1. 重构 `workspace` 和 `runtime` 的路径解析模型。
2. 调整 `init` 与 CLI 参数。
3. 调整 API 启动与 `app.state` 注入。
4. 替换扫描、预览、日志、导出中所有从 `db_path` 反推目录的逻辑。
5. 补齐测试。
6. 更新 README。

## 验收标准

当以下条件同时满足时，本设计视为完成：

- 用户可以执行一次 `init --workspace <local> --external-root <dir>` 初始化新工作区。
- 初始化后，后续所有命令只需 `--workspace <local>` 即可运行。
- 数据库稳定写入本地 `workspace/.hikbox/library.db`。
- `artifacts`、`logs`、`exports` 全部写入 `external_root`。
- 不存在任何功能继续隐式依赖 `db_path` 与 `artifacts/logs/exports` 同目录的假设。
- `external_root` 与 `workspace` 相同时，系统仍能正常工作。
