# 运行时语义说明

本文档描述不直接属于数据库 schema、但需要稳定维护的运行时契约，包括工作区配置文件、日志输出和扫描/归属相关运行语义。

## 1. `config.json`

初始化成功后，`workspace/.hikbox/config.json` 的最小结构如下：

```json
{
  "config_version": 1,
  "external_root": "/absolute/path/to/external-root"
}
```

说明：

- `config_version` 固定为数字 `1`。
- `external_root` 持久化为初始化命令解析后的绝对路径。

## 2. 日志与运行时可观测性

系统会在 `external_root/logs/` 下写 JSON Lines 日志：

- `init.log.jsonl`：`hikbox-pictures init` 成功日志。
- `source.log.jsonl`：`hikbox-pictures source add` 成功日志。
- `scan.log.jsonl`：扫描相关日志，至少包括：
  - `scan_started`
  - `batch_started`
  - `asset_failed`
  - `batch_completed`
  - `scan_completed`
  - `scan_skipped`
  - `assignment_started`
  - `assignment_completed`
  - `assignment_skipped`
  - `assignment_failed`
  - `assignment_warning`

`scan.log.jsonl` 的单条记录会包含该事件最小必需字段，例如时间戳、`session_id`、`batch_id`、`batch_index`、统计摘要和模型根目录。

## 3. 运行语义摘要

- discover 只扫描 active source 根目录下一层文件，按文件绝对路径稳定排序。
- 支持后缀：`jpg`、`jpeg`、`png`、`heic`、`heif`，大小写不敏感。
- 只有 `heic`/`heif` 尝试匹配同目录隐藏 `.MOV/.mov`，并把命中的绝对路径写入 `assets.live_photo_mov_path`。
- `jpg`/`jpeg`/`png` 不做 live MOV 配对。
- 扫描 worker 使用 `det_thresh=0.7` 调用 InsightFace `buffalo_l`。
- 每批调用一个独立 worker 子进程处理真实 InsightFace 检测、embedding 与产物生成。
- 主进程只在整批 worker 成功返回后，才统一提交 `assets`、`face_observations`、`face_embeddings` 和批次 `completed` 状态。
- 单图失败不会阻断同批其它图片提交，但该图不会产生 `face_observations`、`face_embeddings` 或产物。
- 当 discover/批次阶段收敛后，同一个 `hikbox-pictures scan start` 会继续执行在线 assignment 阶段。
- assignment 只读取 `library.db` 中已存在的 active `face_observations` 和 `embedding.db` 中的 `main` embedding；不会重新读照片，也不会重新调用 InsightFace 做归属。
- assignment 使用 HNSW 余弦索引执行两轮在线归属，算法版本固定为 `immich_v6_online_v1`，assignment 来源固定为 `online_v6`。
- orphan `main` embedding 只记录 warning，不进入索引或候选；损坏候选 embedding 会使 assignment 与 `scan_sessions` 一起失败。
