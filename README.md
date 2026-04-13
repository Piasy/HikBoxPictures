# HikBox Pictures

HikBox Pictures 是一个本地 macOS CLI，用于递归扫描照片目录，找出同时包含两位目标人物的图片，并将命中的照片复制到 `only-two/YYYY-MM` 和 `group/YYYY-MM` 输出目录中。

## 依赖要求

- macOS
- Python 3.13+
- Xcode Command Line Tools
- `deepface`
- `tf-keras`
- `Pillow`
- `pillow-heif`

完整依赖列表与版本约束以 `pyproject.toml` 为准。

## 安装

```bash
./scripts/install.sh
```

脚本会自动创建 `.venv`、升级 `pip`，并安装项目及开发依赖（包含 `deepface`、`tf-keras` 与必要的图像处理依赖）。

如果需要显式指定 Python，可在执行前设置 `PYTHON_BIN`：

```bash
PYTHON_BIN=python3.13 ./scripts/install.sh
```

## 控制面命令（Task 5 最小可用）

先进入虚拟环境：

```bash
source .venv/bin/activate
```

初始化工作区（会自动创建目录并执行数据库迁移）：

```bash
PYTHONPATH=src python3 -m hikbox_pictures.cli init --workspace /path/to/workspace
```

启动本地 API（Task 4 已提供基础查询/动作路由）：

```bash
PYTHONPATH=src python3 -m hikbox_pictures.cli serve --workspace /path/to/workspace --host 127.0.0.1 --port 7860
```

当前可用 API（均连接 workspace 真实数据库，包含查询与动作接口）：

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
- `GET /api/logs/events`（支持 `run_kind`、`event_type`、`run_id`、`level`、`limit` 过滤）
- `GET /api/photos/{photo_id}/original`（支持 `Range: bytes=...`）
- `GET /api/photos/{photo_id}/preview`
- `GET /api/observations/{observation_id}/crop`
- `GET /api/observations/{observation_id}/context`

预览降级与重建行为：

- 当 `/api/observations/{observation_id}/crop` 对应裁剪文件缺失时，系统会按 observation bbox 自动重建 crop，写回数据库并记录事件 `preview.context.rebuild_requested`。
- 当原图缺失时，`/api/photos/{photo_id}/original` 返回结构化错误 `{"error_code":"preview.asset.missing","message":"..."}`，并记录事件 `preview.asset.missing`。
- 当预览解码失败时，`/api/photos/{photo_id}/preview` 返回结构化错误 `{"error_code":"preview.asset.decode_failed","message":"..."}`，并记录事件 `preview.asset.decode_failed`。

当前可用 WebUI 路由（同一进程托管，页面数据直接读取 workspace 数据库）：

- `GET /`：人物库首页
- `GET /people/{person_id}`：人物详情维护页
- `GET /reviews`：按类型分组的待审核队列
- `GET /sources`：源目录与扫描进度
- `GET /exports`：导出模板列表
- `GET /logs`：运行日志列表
- `GET /static/style.css`、`GET /static/app.js`：Web 静态资源

WebUI 运行方式：

```bash
source .venv/bin/activate
PYTHONPATH=src python3 -m hikbox_pictures.cli serve --workspace /path/to/workspace --host 127.0.0.1 --port 7860
```

启动后访问 `http://127.0.0.1:7860/`。

统一预览器快捷键：

- `ArrowLeft`：上一张
- `ArrowRight`：下一张
- `b` / `B`：切换脸框显示

## WebUI 看图验收（P0）

- 人物详情页需可见三层图层（`crop/context/original`）与预览器动作按钮（上一张、下一张、脸框开关）。
- 待审核页需可见同一套预览器动作语义；即使某一张图预览失败，也不能阻塞队列页面与待审核条目展示。
- 导出模板页需展示 `export-preview-sample` 样例卡片，不能只展示命中计数。
- 媒体 API 需满足边界约束：原图接口支持 `Range`，并具备路径越界防护与结构化错误码。
- 预览接口性能烟测门槛为 600ms（本地测试以 `tests/people_gallery/test_media_preview_performance_smoke.py` 为准）。

## E2E（Mock Embedding）说明

- e2e 集成测试支持使用“数字图片”作为输入，不依赖真实人脸检测结果。
- 通过测试夹具向 `face_observation`、`face_embedding`、`person_face_assignment` 注入 mock 数据，绕过检测与 embedding 提取耗时链路。
- 该路径用于验证“人物维护 -> 预览 -> 导出 -> 日志”后续流程稳定性，不替代真实模型链路测试。

当前可用扫描控制命令：

- `scan --workspace <dir>`：默认恢复最近可恢复会话（`pending/running/paused/interrupted`），若不存在则创建新的增量会话。
- `scan status --workspace <dir>`：查看当前可恢复会话状态；若无可恢复会话则显示 `idle`。

当前可用导出控制命令：

- `export run --workspace <dir> --template-id <id>`：执行指定模板，输出 `matched_only/matched_group/exported/skipped/failed` 摘要。

当前可用日志控制命令：

- `logs tail --workspace <dir> [--run-kind <scan|export>] [--run-id <id>] [--limit <n>]`：查看结构化 run 日志（JSON Lines）。
- `logs prune --workspace <dir> [--days <n>]`：按天数清理 `ops_event` 历史索引记录。

Task 5 回归记录（先失败后通过）：

- 失败阶段：`source .venv/bin/activate && PYTHONPATH=src python3 -m pytest tests/people_gallery/test_scan_resume_semantics.py tests/people_gallery/test_scan_owner_reaper.py -v`
- 通过阶段：`source .venv/bin/activate && PYTHONPATH=src python3 -m pytest tests/people_gallery/test_scan_resume_semantics.py tests/people_gallery/test_scan_owner_reaper.py tests/people_gallery/test_cli_control_plane.py::test_scan_status_command -q`

Task 6 回归记录（先失败后通过）：

- 失败阶段：`source .venv/bin/activate && PYTHONPATH=src python3 -m pytest tests/people_gallery/test_asset_stage_idempotency.py tests/people_gallery/test_scan_session_source_progress.py -v`
- 通过阶段：`source .venv/bin/activate && PYTHONPATH=src python3 -m pytest tests/people_gallery/test_asset_stage_idempotency.py tests/people_gallery/test_scan_session_source_progress.py tests/people_gallery/test_api_contract.py::test_scan_status_reports_source_progress -q`

Task 7 回归记录（先失败后通过）：

- 失败阶段：`source .venv/bin/activate && PYTHONPATH=src python3 -m pytest tests/people_gallery/test_person_truth_actions.py tests/people_gallery/test_review_actions_contract.py -v`
- 通过阶段：`source .venv/bin/activate && PYTHONPATH=src python3 -m pytest tests/people_gallery/test_person_truth_actions.py tests/people_gallery/test_review_actions_contract.py tests/people_gallery/test_api_actions.py::test_people_rename_action_persists_to_db -q`

Task 8 回归记录（先失败后通过）：

- 失败阶段：`source .venv/bin/activate && PYTHONPATH=src python3 -m pytest tests/people_gallery/test_ann_recall.py tests/people_gallery/test_threshold_layers.py -v`
- 通过阶段：`source .venv/bin/activate && PYTHONPATH=src python3 -m pytest tests/people_gallery/test_ann_recall.py tests/people_gallery/test_threshold_layers.py tests/people_gallery/test_cli_control_plane.py::test_rebuild_artifacts_command -q`

Task 9 回归记录（先失败后通过）：

- 失败阶段：`source .venv/bin/activate && PYTHONPATH=src python3 -m pytest tests/people_gallery/test_export_matching_and_ledger.py tests/people_gallery/test_export_stale_cleanup.py tests/people_gallery/test_export_live_photo_delivery.py -v`
- 通过阶段：`source .venv/bin/activate && PYTHONPATH=src python3 -m pytest tests/people_gallery/test_export_matching_and_ledger.py tests/people_gallery/test_export_stale_cleanup.py tests/people_gallery/test_export_live_photo_delivery.py tests/people_gallery/test_api_contract.py::test_export_preview_contains_real_counts -q`

Task 10 回归记录（先失败后通过）：

- 失败阶段：`source .venv/bin/activate && PYTHONPATH=src python3 -m pytest tests/people_gallery/test_ops_event_filters.py tests/people_gallery/test_logs_tail_and_prune.py -v`
- 通过阶段：`source .venv/bin/activate && PYTHONPATH=src python3 -m pytest tests/people_gallery/test_ops_event_filters.py tests/people_gallery/test_logs_tail_and_prune.py tests/people_gallery/test_api_contract.py::test_logs_api_filter_event_type -q`

Task 4 回归记录（先失败后通过）：

- 失败阶段：`source .venv/bin/activate && PYTHONPATH=src python3 -m pytest tests/people_gallery/test_api_contract.py tests/people_gallery/test_api_actions.py -v`
- 通过阶段：`source .venv/bin/activate && PYTHONPATH=src python3 -m pytest tests/people_gallery/test_api_contract.py tests/people_gallery/test_api_actions.py -q`

当前已建立的命令树：

- `init`
- `source add|list|remove`
- `serve`
- `scan`
- `rebuild-artifacts`
- `export run`
- `logs tail|prune`

## 旧版一次性导出用法（兼容模式）

```bash
hikbox-pictures --input /path/to/photo-library --ref-a-dir /path/to/person-a-images --ref-b-dir /path/to/person-b-images --output /path/to/output --model-name ArcFace --detector-backend retinaface --distance-metric cosine --distance-threshold-a 0.32 --distance-threshold-b 0.36 --align
```

如需关闭对齐，可改为 `--no-align`。

参考目录建议每人准备多张正脸、清晰、光照正常的照片，以提升匹配稳定性。

默认 `retinaface` 检测后端依赖 `tf-keras`，安装脚本会一并安装。

首次运行可能触发模型下载，需联网，首次启动会明显慢于后续运行；后续会复用本地缓存模型。

## 测试

先进入虚拟环境：

```bash
source .venv/bin/activate
```

运行全部测试：

```bash
PYTHONPATH=src python3 -m pytest -q
```

只运行某一组测试：

```bash
PYTHONPATH=src python3 -m pytest tests/test_cli.py -v
```

## 距离调试

如果想查看每张候选图片中每张人脸到两组参考图的模板距离、质心距离与 `joint_distance`，可运行：

```bash
source .venv/bin/activate
PYTHONPATH=src python3 scripts/inspect_distances.py --input test --ref-a-dir test/ref-a --ref-b-dir test/ref-b --model-name ArcFace --detector-backend retinaface --distance-metric cosine --distance-threshold-a 0.32 --distance-threshold-b 0.36 --align
```

如果还想生成带人脸框和距离标注的临时图片，可额外传入 `--annotated-dir`：

```bash
source .venv/bin/activate
PYTHONPATH=src python3 scripts/inspect_distances.py --input test --ref-a-dir test/ref-a --ref-b-dir test/ref-b --annotated-dir test/annotated --model-name ArcFace --detector-backend retinaface --distance-metric cosine --distance-threshold-a 0.32 --distance-threshold-b 0.36 --align
```

## 阈值标定

可以先用正负样本目录标定单人的模板阈值，再把建议值回填到 `--distance-threshold-a` 或 `--distance-threshold-b`：

```bash
source .venv/bin/activate
PYTHONPATH=src python3 scripts/calibrate_thresholds.py --ref-dir /path/to/person-a-images --positive-dir /path/to/person-a-positive --negative-dir /path/to/person-a-negative --model-name ArcFace --detector-backend retinaface --distance-metric cosine --align
```

脚本会输出 `best_f1_threshold` 与 `best_youden_j_threshold` 两组建议值。

## 输出结构

- `only-two/YYYY-MM/`：正好检测到两张人脸，且两人都命中的照片。
- `group/YYYY-MM/`：检测到两人且总人脸数大于两张的照片。
- 命中的 `HEIC` 文件若存在配对的隐藏 Live Photo `MOV`，会一并复制。

## 限制

- 匹配效果依赖 `deepface` 模型与图片质量。
- DeepFace 及其底层模型、检测器组件许可可能各不相同；用于生产或商业场景前请务必自行核对最新许可条款（常见限制包括非商业研究用途）。
- 工具只扫描图片文件，不分析视频内容。
- 归档月份优先使用图片 EXIF 拍摄时间；若缺失，则回退到文件创建时间和修改时间。
- 创建时间保留依赖 macOS `SetFile`，属于尽力而为。
