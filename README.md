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
- `GET /api/logs/events`

当前可用扫描控制命令：

- `scan --workspace <dir>`：默认恢复最近可恢复会话（`pending/running/paused/interrupted`），若不存在则创建新的增量会话。
- `scan status --workspace <dir>`：查看当前可恢复会话状态；若无可恢复会话则显示 `idle`。

Task 5 回归记录（先失败后通过）：

- 失败阶段：`source .venv/bin/activate && PYTHONPATH=src python3 -m pytest tests/people_gallery/test_scan_resume_semantics.py tests/people_gallery/test_scan_owner_reaper.py -v`
- 通过阶段：`source .venv/bin/activate && PYTHONPATH=src python3 -m pytest tests/people_gallery/test_scan_resume_semantics.py tests/people_gallery/test_scan_owner_reaper.py tests/people_gallery/test_cli_control_plane.py::test_scan_status_command -q`

Task 6 回归记录（先失败后通过）：

- 失败阶段：`source .venv/bin/activate && PYTHONPATH=src python3 -m pytest tests/people_gallery/test_asset_stage_idempotency.py tests/people_gallery/test_scan_session_source_progress.py -v`
- 通过阶段：`source .venv/bin/activate && PYTHONPATH=src python3 -m pytest tests/people_gallery/test_asset_stage_idempotency.py tests/people_gallery/test_scan_session_source_progress.py tests/people_gallery/test_api_contract.py::test_scan_status_reports_source_progress -q`

Task 7 回归记录（先失败后通过）：

- 失败阶段：`source .venv/bin/activate && PYTHONPATH=src python3 -m pytest tests/people_gallery/test_person_truth_actions.py tests/people_gallery/test_review_actions_contract.py -v`
- 通过阶段：`source .venv/bin/activate && PYTHONPATH=src python3 -m pytest tests/people_gallery/test_person_truth_actions.py tests/people_gallery/test_review_actions_contract.py tests/people_gallery/test_api_actions.py::test_people_rename_action_persists_to_db -q`

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
