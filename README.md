# HikBox Pictures

HikBox Pictures 是一个本地 macOS CLI，用于递归扫描照片目录，找出同时包含两位目标人物的图片，并将命中的照片复制到 `only-two/YYYY-MM` 和 `group/YYYY-MM` 输出目录中。

## 依赖要求

- macOS
- Python 3.13+
- Xcode Command Line Tools
- `face_recognition` 运行时依赖，包括 `dlib`

## 安装

```bash
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install --upgrade pip
python3 -m pip install -e '.[dev]'
```

如果 `dlib` 构建失败，请先安装 Xcode Command Line Tools，再在虚拟环境中重新执行安装。

## 用法

```bash
hikbox-pictures --input /path/to/photo-library --ref-a /path/to/person-a.jpg --ref-b /path/to/person-b.jpg --output /path/to/output
```

## 输出结构

- `only-two/YYYY-MM/`：正好检测到两张人脸，且两人都命中的照片。
- `group/YYYY-MM/`：检测到两人且总人脸数大于两张的照片。
- 命中的 `HEIC` 文件若存在配对的隐藏 Live Photo `MOV`，会一并复制。

## 限制

- 匹配效果依赖 `face_recognition` 模型和图片质量。
- 工具只扫描图片文件，不分析视频内容。
- 创建时间保留依赖 macOS `SetFile`，属于尽力而为。
