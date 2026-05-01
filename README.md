# HikBox Pictures

HikBox Pictures 是一个面向本地照片库的人物图库实验项目。核心能力包括：工作区初始化、照片 source 管理、人脸检测与人物聚类扫描、在线人物归属、人物库 WebUI 浏览与管理、以及按人物组合导出照片。

## 环境准备

项目要求 Python `3.12`。优先使用仓库根目录的 `.venv`：

```bash
./scripts/install.sh
source .venv/bin/activate
```

`./scripts/install.sh` 会安装项目开发依赖和 Playwright Chromium 浏览器。脚本会优先复用本地可用的 `uv`，如果没有则安装到仓库内的 `.tools/uv/bin/`。

## CLI 命令

### 初始化工作区

```bash
hikbox-pictures init --workspace /path/to/workspace --external-root /path/to/external-root
```

创建工作区目录、`library.db` 和 `embedding.db`，执行全量建表并升级到最新 schema 版本。如果工作区已存在则报错退出。

### 管理照片 source

添加照片 source：

```bash
hikbox-pictures source add --workspace /path/to/workspace /path/to/source
```

命令会自动将源目录的目录名写入 `label`。

查看 source 列表：

```bash
hikbox-pictures source list --workspace /path/to/workspace
```

### 执行扫描

```bash
hikbox-pictures scan start --workspace /path/to/workspace
```

扫描流程分两阶段：批处理阶段（人脸检测与特征提取）和在线归属阶段（人物聚类与分配）。默认会把扫描进度打印到 `stderr`，按 10 秒周期输出当前阶段和进度，例如：

```
scan 进度: 阶段=批处理，批次 2/6，照片 17/52
```

可通过 `--batch-size` 调整每批处理的照片数量，默认 200。

同一 workspace 上，`scan start` 与 `serve` 完全互斥：扫描运行中不能启动 WebUI，WebUI 运行中也不能启动新的扫描。需要做二次扫描时，先结束 `serve`，扫描完成后再重新启动 `serve`。

### 启动 WebUI

```bash
hikbox-pictures serve --workspace /path/to/workspace [--port <port>] [--person-detail-page-size <n>]
```

- `--port`：监听端口，默认 `8000`。
- `--person-detail-page-size`：人物详情页分页大小，默认 `204`。

WebUI 以 FastAPI + uvicorn 提供服务，默认绑定 `127.0.0.1`。

## WebUI 功能

### 人物库浏览

- 首页展示已命名人物和匿名人物，按样本数排序。
- 点击人物卡片进入详情页。

### 人物详情

- 按 `--person-detail-page-size` 配置分页浏览人物照片。
- 详情页标记 Live Photo。
- 显示人脸样本上下文裁剪图。

### 人物命名与重命名

- 在人物详情页执行首次命名和后续重命名。
- 重命名会写入 `person_name_events` 审计记录。

### 人物合并

- 在人物首页选择多个同名或不同名人物进行合并。
- 支持撤销最近一次合并操作。
- 导出进行中时合并操作会被锁定。

### 样本排除

- 在人物详情页勾选误归属的人脸样本并排除。
- 排除后若人物已无剩余样本，该人物自动清空并返回首页。

### 导出模板

- **创建模板**：选择 2 个及以上已命名人物，指定输出目录，创建导出模板。相同配置的模板不允许重复创建。
- **预览**：按月份分桶展示命中照片，区分"仅目标人物"（only）和"含其他人"（group）两个分桶。预览结果自动持久化为导出计划（export plan），支持同名文件冲突消解（自动追加 source label 后缀）。
- **执行导出**：异步后台复制文件到输出目录，按 `{bucket}/{month}/` 目录结构组织。Live Photo 的 MOV 配对文件会同步复制。已存在的文件自动跳过。
- **导出历史**：查看每次导出的执行状态、复制/跳过计数和逐文件交付详情。
- **运行锁**：同一时刻只能有一个导出在运行；导出进行中，人物命名、合并和排除操作会被锁定。服务重启时自动清理残留的 running 状态记录。

### REST API

WebUI 同时提供 JSON API 端点：

| 端点 | 方法 | 说明 |
|------|------|------|
| `/api/export-templates` | GET | 列出所有导出模板 |
| `/api/export-templates` | POST | 创建导出模板 |
| `/api/export-templates/{id}/preview` | GET | 获取导出预览 |
| `/api/export-templates/{id}/execute` | POST | 执行导出（同步） |
| `/api/export-templates/{id}/runs` | GET | 获取导出历史 |
| `/api/export-runs/{run_id}` | GET | 获取导出详情 |

## DB Migration

数据库 schema 变更通过 migration 机制自动执行：

- SQL 文件存放于 `hikbox_pictures/product/db/sql/`，命名规则为 `library_v{N}.sql` 和 `embedding_v{N}.sql`。
- `library.db` 和 `embedding.db` 各自独立维护版本号，各自独立执行 migration。
- `init` 命令先执行 v1 全量建表，再依次升级到最新版本。
- 其他命令打开 DB 连接后自动执行待升级的 migration SQL。
- 涉及 schema 修改时，需同步更新 `docs/db_schema.md`。

## 运行测试

推荐统一通过脚本运行：

```bash
./scripts/run_tests.sh
```

运行单个测试文件或测试用例：

```bash
./scripts/run_tests.sh tests/test_hikbox_init_cli.py
./scripts/run_tests.sh tests/people_gallery/test_people_gallery_online_assignment.py::test_scan_start_creates_expected_online_assignments_and_is_idempotent
```

如果 `.venv` 不存在，先执行 `./scripts/install.sh`，不要直接使用系统 Python 跑仓库测试。

## Playwright 与前端验收

WebUI 自动化验收以 Python Playwright + pytest 为主，测试路径约定为：

```text
tests/people_gallery/test_webui_*_playwright.py
```

验收目标是验证真实链路：真实 `hikbox-pictures` CLI、真实 HTTP 服务、真实浏览器页面交互、真实 SQLite 和真实图片 artifact。截图不作为默认验收依据；只有在视觉或布局不确定、需要人工复核、用户明确要求，或正在排查视觉回归时才保存截图。

本地调试或一次性排查也复用 Python Playwright + pytest 入口。需要额外调试能力时，优先在对应测试内补 helper、fixture、pytest 参数或环境变量，不新增独立的 Playwright 脚本入口。

仅在需要截图或视觉排查中文渲染时，才需要额外准备 Playwright 中文字体：

```bash
./scripts/setup_playwright_zh_fonts.sh
```

调试产物、服务日志、JSON 报告和按需截图统一放到 `.tmp/<task-name>/`。

## 临时文件

临时测试文件、调试产物、截图、JSON 报告、临时 runner 和临时日志统一放到：

```text
.tmp/<task-name>/
```

不要在仓库根目录新增 `.tmp-*`、`tmp-*` 等零散临时目录。
