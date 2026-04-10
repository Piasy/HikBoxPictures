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

## 用法

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
