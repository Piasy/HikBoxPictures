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
- `scan --workspace <dir>`：执行或恢复扫描会话，并完成 source 发现、阶段推进与会话收口；若存在失败 source，会返回非零退出码。
- `scan status --workspace <dir>`：查看最近一次会话与 source 进度，完成态与失败态也会保留可见。
- `scan abort --workspace <dir>`：中断最近一个未完成扫描会话。
- `scan new --workspace <dir> --abandon-resumable`：放弃旧会话并启动新扫描。
- `rebuild-artifacts --workspace <dir>`：按当前 `face_embedding.model_key` 重建人物原型与 ANN 索引。
- `export run --workspace <dir> --template-id <id>`：执行导出模板并输出统计摘要。
- `logs tail --workspace <dir> [--run-kind ... --run-id ... --limit ...]`：查看结构化运行日志。
- `logs prune --workspace <dir> [--days <n>]`：按天数清理 `ops_event` 历史索引。

## API 路由

- `GET /api/health`
- `GET /api/scan/status`
- `POST /api/scan/start_or_resume`
- `POST /api/scan/abort`
- `POST /api/scan/start_new`
- `GET /api/people`
- `POST /api/people/{id}/actions/rename`
- `POST /api/people/{id}/actions/merge`
- `POST /api/people/{id}/actions/split`
- `POST /api/people/{id}/actions/lock-assignment`
- `GET /api/reviews`
- `POST /api/reviews/{id}/actions/dismiss`
- `POST /api/reviews/{id}/actions/resolve`
- `POST /api/reviews/{id}/actions/ignore`
- `GET /api/export/templates`
- `GET /api/export/templates/{template_id}/preview`
- `POST /api/export/templates/{template_id}/actions/run`
- `GET /api/export/templates/{template_id}/runs`
- `GET /api/logs/events`
- `GET /api/photos/{photo_id}/original`
- `GET /api/photos/{photo_id}/preview`
- `GET /api/observations/{observation_id}/crop`
- `GET /api/observations/{observation_id}/context`

其中 `GET /api/scan/status` 会返回最近一次扫描会话，不会在 completed/failed 后回退成 `idle`。`POST /api/scan/start_or_resume` 会同步执行 discover 与四阶段流水线，响应中的 `status` 即最终会话状态。

`GET /api/observations/{observation_id}/context` 不再直出原图，而是返回 workspace 下生成的局部上下文 artifact：包含 bbox 周边区域与高亮框，文件写入 `.hikbox/artifacts/context/`。

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

如需验证真实 DeepFace 主链路，推荐至少运行以下组合：

```bash
source .venv/bin/activate
PYTHONPATH=src python3 -m pytest \
  tests/people_gallery/test_real_face_pipeline.py \
  tests/people_gallery/test_assignment_with_ann_thresholds.py \
  tests/people_gallery/test_e2e_real_source_pipeline.py -q
```

主流程验收已要求包含无 seed/mock 注入路径，固定数据集位于 `tests/data/e2e-face-input`。
