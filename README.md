# HikBox Pictures

HikBox Pictures 是一个本地运行的图库控制面项目，围绕 workspace 提供扫描、人物维护、审核、导出与日志能力。当前主入口为 CLI + 本地 API/WebUI。

## 依赖要求

- macOS
- Python 3.12
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

安装脚本会自动完成以下事情：

- 检测 `uv`；如果机器上没有，会自动安装到仓库内 `.tools/uv/bin/uv`
- 下载并固定使用 uv 管理的 Python 3.12，安装位置默认在 `.tools/python`
- 创建/修复 `.venv`
- 安装项目及开发依赖
- 安装 Playwright Chromium 浏览器
- 准备 Playwright 中文字体

这样创建出来的 `.venv` 绑定的是仓库内 uv 管理的 Python 3.12，不依赖系统 Python。

如需显式指定已有的 `uv`，可在执行前设置 `UV_BIN`。

## 快速开始

先激活虚拟环境：

```bash
source .venv/bin/activate
```

初始化 workspace（本地保存数据库与配置，并在 external root 下创建运行文件目录）：

```bash
PYTHONPATH=src python3 -m hikbox_pictures.cli init \
  --workspace /path/to/local-workspace \
  --external-root /path/to/external-root
```

其中：

- `workspace` 必须是本地目录，只保存 `.hikbox/library.db` 和 `.hikbox/config.json`
- `external-root` 用于保存 `artifacts/`、`logs/`、`exports/`
- 若希望所有文件都放在同一本地目录，可把 `--external-root` 设为与 `--workspace` 相同

启动本地 API/WebUI：

```bash
PYTHONPATH=src python3 -m hikbox_pictures.cli serve --workspace /path/to/workspace --host 127.0.0.1 --port 7860
```

启动后访问：`http://127.0.0.1:7860/`

## CLI 命令

- `init --workspace <dir> --external-root <dir>`：初始化工作区、本地数据库与外部运行目录。
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
- `POST /api/people/{id}/actions/exclude-assignment`
- `GET /api/reviews`
- `POST /api/reviews/{id}/actions/dismiss`
- `POST /api/reviews/{id}/actions/resolve`
- `POST /api/reviews/{id}/actions/ignore`
- `GET /api/export/templates`
- `POST /api/export/templates`
- `PUT /api/export/templates/{template_id}`
- `DELETE /api/export/templates/{template_id}`
- `GET /api/export/templates/{template_id}/preview`
- `POST /api/export/templates/{template_id}/actions/run`
- `GET /api/export/templates/{template_id}/runs`
- `GET /api/logs/events`
- `GET /api/photos/{photo_id}/original`
- `GET /api/photos/{photo_id}/preview`
- `GET /api/observations/{observation_id}/crop`
- `GET /api/observations/{observation_id}/context`

其中 `GET /api/scan/status` 会返回最近一次扫描会话，不会在 completed/failed 后回退成 `idle`。`POST /api/scan/start_or_resume` 会同步执行 discover 与四阶段流水线，响应中的 `status` 即最终会话状态。

`GET /api/observations/{observation_id}/context` 不再直出原图，而是返回 external root 下生成的局部上下文 artifact：包含 bbox 周边区域与高亮框，文件写入 `artifacts/context/`。

## WebUI 路由

- `GET /`：人物库首页
- `GET /people/{person_id}`：人物详情维护页
- `GET /reviews`：待审核队列
- `GET /sources`：源目录与扫描进度
- `GET /exports`：导出模板创建、管理、预览与执行页
- `GET /logs`：运行日志列表
- `GET /static/style.css`、`GET /static/app.js`：静态资源

## v3 第一阶段：身份层重建与调参验收（phase1）

```bash
source .venv/bin/activate
python scripts/rebuild_identities_v3.py --workspace <workspace> --dry-run
python scripts/rebuild_identities_v3.py --workspace <workspace> --backup-db
python scripts/evaluate_identity_thresholds.py --workspace <workspace> --output-dir .tmp/identity-threshold-tuning/<timestamp>/
cp -R <workspace> <workspace-copy>
python scripts/rebuild_identities_v3.py --workspace <workspace-copy> --backup-db --threshold-profile <candidate-thresholds.json 文件路径>
python -m hikbox_pictures.cli serve --workspace <workspace> --host 0.0.0.0 --port 8000
```

- 调参验收入口：`/identity-tuning`（只读）。
- phase1 明确允许 scan/review/actions/export 旧功能暂时失效；不在本阶段做封禁或兼容兜底。
- 主链验收必须包含真实图片路径，不允许只跑 seed/mock 夹具。

## 测试

运行全部测试：

```bash
./scripts/install.sh
./scripts/run_tests.sh
```

该脚本默认带上 `RUN_PLAYWRIGHT_VISUAL=1` 运行全量 pytest；首次运行前先执行一次 `./scripts/install.sh` 准备环境。

只运行 people_gallery：

```bash
./scripts/run_tests.sh tests/people_gallery -q
```

如需验证真实 DeepFace 主链路，推荐至少运行以下组合：

```bash
source .venv/bin/activate
PYTHONPATH=src python -m pytest \
  tests/people_gallery/test_real_face_pipeline.py \
  tests/people_gallery/test_assignment_with_ann_thresholds.py \
  tests/people_gallery/test_e2e_real_source_pipeline.py -q
```

主流程验收已要求包含无 seed/mock 注入路径，固定数据集位于 `tests/data/e2e-face-input`。

人物库首页视觉检查（Playwright）：

```bash
./scripts/install.sh
source .venv/bin/activate
PYTHONPATH=src python -m pytest tests/people_gallery/test_webui_people_home_visual_playwright.py -q
```

说明：安装脚本已经会自动安装 Chromium 并准备中文字体。`setup_playwright_zh_fonts.sh` 会在仓库内 `.cache/playwright-fonts/` 下载 Noto Sans CJK SC 并生成局部 `fontconfig`，仅供 Playwright 浏览器进程使用，不影响应用运行时字体配置。

该用例仅覆盖 `GET /` 两种状态：空库态、seed 后人物卡片态；不会触碰 reviews/person detail/exports 页面语义。
