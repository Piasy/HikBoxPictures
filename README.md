# HikBox Pictures

HikBox Pictures 是一个本地运行的图库控制面项目，围绕 workspace 提供扫描、人物维护、审核、导出与日志能力。当前主入口为 CLI + 本地 API/WebUI。

## 依赖要求

- macOS
- Python 3.13+
- Xcode Command Line Tools
- `deepface`
- `tf-keras`
- `Pillow`
- `pillow-heif`

完整依赖与版本约束以 `pyproject.toml` 为准。

## 安装

```bash
./scripts/install.sh
```

安装脚本会创建 `.venv`、升级 `pip`，并安装项目及开发依赖。

如需显式指定 Python，可在执行前设置 `PYTHON_BIN`：

```bash
PYTHON_BIN=python3.13 ./scripts/install.sh
```

## 快速开始

先激活虚拟环境：

```bash
source .venv/bin/activate
```

初始化 workspace（自动创建目录并执行数据库迁移）：

```bash
PYTHONPATH=src python3 -m hikbox_pictures.cli init --workspace /path/to/workspace
```

启动本地 API/WebUI：

```bash
PYTHONPATH=src python3 -m hikbox_pictures.cli serve --workspace /path/to/workspace --host 127.0.0.1 --port 7860
```

启动后访问：`http://127.0.0.1:7860/`

## CLI 命令

- `init --workspace <dir>`：初始化工作区与数据库。
- `source add|list|remove --workspace <dir> ...`：源目录管理（添加、查看、移除）。
- `serve --workspace <dir> [--host ... --port ...]`：启动 API/WebUI。
- `scan --workspace <dir>`：执行或恢复扫描会话。
- `scan status --workspace <dir>`：查看会话与 source 进度。
- `rebuild-artifacts --workspace <dir>`：重建人物原型与 ANN 索引。
- `export run --workspace <dir> --template-id <id>`：执行导出模板并输出统计摘要。
- `logs tail --workspace <dir> [--run-kind ... --run-id ... --limit ...]`：查看结构化运行日志。
- `logs prune --workspace <dir> [--days <n>]`：按天数清理 `ops_event` 历史索引。

## API 路由

- `GET /api/health`
- `GET /api/scan/status`
- `POST /api/scan/start_or_resume`
- `GET /api/people`
- `POST /api/people/{id}/actions/rename`
- `POST /api/people/{id}/actions/merge`
- `POST /api/people/{id}/actions/split`
- `POST /api/people/{id}/actions/lock-assignment`
- `GET /api/reviews`
- `POST /api/reviews/{id}/actions/dismiss`
- `GET /api/export/templates`
- `GET /api/export/templates/{template_id}/preview`
- `GET /api/logs/events`
- `GET /api/photos/{photo_id}/original`
- `GET /api/photos/{photo_id}/preview`
- `GET /api/observations/{observation_id}/crop`
- `GET /api/observations/{observation_id}/context`

## WebUI 路由

- `GET /`：人物库首页
- `GET /people/{person_id}`：人物详情维护页
- `GET /reviews`：待审核队列
- `GET /sources`：源目录与扫描进度
- `GET /exports`：导出模板列表
- `GET /logs`：运行日志列表
- `GET /static/style.css`、`GET /static/app.js`：静态资源

## 测试

运行全部测试：

```bash
source .venv/bin/activate
PYTHONPATH=src python3 -m pytest -q
```

只运行 people_gallery：

```bash
source .venv/bin/activate
PYTHONPATH=src python3 -m pytest tests/people_gallery -q
```
