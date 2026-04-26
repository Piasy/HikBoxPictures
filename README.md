# HikBox Pictures

HikBox Pictures 是一个面向本地照片库的人物图库实验项目。当前代码主要覆盖工作区初始化、source 管理、扫描、人脸检测产物、在线人物归属，以及人物库 WebUI 的浏览、详情分页、命名和重命名能力。

## 环境准备

项目要求 Python `3.12`。优先使用仓库根目录的 `.venv`：

```bash
./scripts/install.sh
source .venv/bin/activate
```

`./scripts/install.sh` 会安装项目开发依赖、Playwright Chromium 浏览器和中文字体。脚本会优先复用本地可用的 `uv`，如果没有则安装到仓库内的 `.tools/uv/bin/`。

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

## 常用 CLI

初始化工作区：

```bash
hikbox-pictures init --workspace /path/to/workspace --external-root /path/to/external-root
```

添加照片 source：

```bash
hikbox-pictures source add --workspace /path/to/workspace /path/to/source --label fixture
```

查看 source：

```bash
hikbox-pictures source list --workspace /path/to/workspace
```

执行扫描：

```bash
hikbox-pictures scan start --workspace /path/to/workspace
```

WebUI 的 `hikbox-pictures serve` 入口已经实现，可用于在本机启动人物库 WebUI；当前支持 `hikbox-pictures serve --workspace <path> [--port <port>] [--person-detail-page-size <n>]`。

当前 WebUI 能力包括：

- 浏览已命名人物与匿名人物的人物库首页。
- 打开人物详情页，并按 `--person-detail-page-size` 配置分页浏览照片；详情页会标记 Live 照片。
- 在人物详情页执行首次命名和后续重命名；重命名会写入 `person_name_events` 审计记录。

## Playwright 与前端验收

WebUI 自动化验收以 Python Playwright + pytest 为主，测试路径约定为：

```text
tests/people_gallery/test_webui_*_playwright.py
```

验收目标是验证真实链路：真实 `hikbox-pictures` CLI、真实 HTTP 服务、真实浏览器页面交互、真实 SQLite 和真实图片 artifact。截图不作为默认验收依据；只有在视觉或布局不确定、需要人工复核、用户明确要求，或正在排查视觉回归时才保存截图。

本地调试或一次性排查也复用 Python Playwright + pytest 入口。需要额外调试能力时，优先在对应测试内补 helper、fixture、pytest 参数或环境变量，不新增独立的 Playwright 脚本入口。

调试产物、服务日志、JSON 报告和按需截图统一放到 `.tmp/<task-name>/`。

## 数据库文档

涉及数据库 schema 的修改必须同步更新：

```text
docs/db_schema.md
```

包括 migration、建表、字段、索引、约束和运行时语义变更。

## 临时文件

临时测试文件、调试产物、截图、JSON 报告、临时 runner 和临时日志统一放到：

```text
.tmp/<task-name>/
```

不要在仓库根目录新增 `.tmp-*`、`tmp-*` 等零散临时目录。
