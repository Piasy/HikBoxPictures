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

## 工作区与运行时文档

- 数据库 schema 和 migration 机制详见 `docs/db_schema.md`。
- 工作区 `config.json`、日志输出和扫描/归属运行语义详见 `docs/runtime_semantics.md`。

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

数据库 schema 和 migration 机制详见 `docs/db_schema.md`。涉及 schema 修改时，必须新增对应 migration SQL，并同步更新该文档。工作区配置、日志与运行语义见 `docs/runtime_semantics.md`。

## 运行测试

自动化验收统一通过 `./scripts/run_tests.sh` 执行。脚本会按测试文件逐个运行，并在每个文件结束后打印结果摘要（总运行用例数、失败用例数、总耗时）。如果 `.venv` 不存在，先执行 `./scripts/install.sh`，不要直接使用系统 Python 跑仓库测试。

```bash
./scripts/run_tests.sh
```

常用运行方式：

```bash
./scripts/run_tests.sh tests/test_hikbox_init_cli.py
./scripts/run_tests.sh tests/people_gallery/test_people_gallery_online_assignment.py::test_scan_start_creates_expected_online_assignments_and_is_idempotent
./scripts/run_tests.sh --scope backend
./scripts/run_tests.sh --scope frontend
```

`--scope backend` 会排除 `test_webui_*_playwright.py`，`--scope frontend` 只运行 `test_webui_*_playwright.py`；scope 和文件路径可以组合，也可以传入多个文件路径。

最新一轮逐文件全量运行总耗时约 15 分钟，其中 `tests/test_hikbox_scan_cli.py` 约 6 分钟、`tests/people_gallery/test_webui_people_gallery_playwright.py` 约 4 分钟、`tests/test_hikbox_serve_cli.py` 约 2 分钟，是主要耗时来源；其余多数文件在 30 秒内完成。日常修改优先运行受影响文件，收尾或跨模块改动再运行 `--scope backend`、`--scope frontend` 或全量。

已扫描图库基线已经抽到 `tests/conftest.py`：全量 fixture 的 `init -> source add -> scan start` 以 session 级金色工作区懒加载执行一次；新增测试只要需要“已完成主基线扫描的图库”，必须复用 `scanned_workspace` 或 `copy_scanned_workspace(tmp_path)`，不要重新内联完整 init/scan helper。

使用预扫描工作区时，测试只应修改自己的副本；`copy_scanned_workspace` 会修复副本中的 `config.json`、`face_observations.crop_path` 和 `face_observations.context_path`。`library_sources.path`、`assets.absolute_path`、`assets.live_photo_mov_path` 保持指向固定 fixture 目录，不要在测试里额外改写。

验证 `init` 自身、`source add/list` 行为、首次扫描或重扫语义、扫描中的拒绝服务、迁移启动路径、自定义图片子集，以及真实增量 `source add -> scan start` 的用例，继续使用真实 CLI、真实 SQLite 和真实图片 artifact。

WebUI 自动化验收以 Python Playwright + pytest 为主，文件必须命名为：

```text
tests/people_gallery/test_webui_*_playwright.py
```

其余测试文件属于 backend 用例。WebUI 主路径验收必须走真实公共入口：真实 `hikbox-pictures` CLI、真实 HTTP 服务、真实浏览器页面交互、真实 SQLite 和真实图片 artifact；只是在“已扫描基线”准备阶段允许复用预扫描工作区副本。CLI 启动失败、端口占用、schema 缺失、扫描运行中拒绝服务等边界，不必强行用浏览器覆盖，优先用服务级集成测试验证退出码、stderr 和端口状态。

当前前端验收范围以 Chromium 桌面浏览器为准；移动端兼容性不作为本阶段要求，也不作为阻塞项。Playwright 交互优先使用 role/name 等语义定位，并结合页面暴露的稳定 `data-*` 标识与 DB/artifact 对齐；不要把截图识别或脆弱 CSS selector 作为主断言。

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
