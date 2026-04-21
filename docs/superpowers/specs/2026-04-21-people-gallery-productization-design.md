# HikBox Pictures 人物图库产品化设计（v5 原型统一重构）

## 1. 背景与目标

当前仓库 `docs/group_pics_algo.md` 的 v5 已完成算法原型验证，识别与归类效果可接受。当前目标不是继续调算法，而是将原型产品化：

- 保持 v5 归类语义，目标“业务一致”。
- 统一到可持续维护的本地产品架构（数据库、任务、WebUI、导出、日志）。
- 保留现有关键工程实践：子进程批处理释放内存、阶段化续跑、本地优先。

## 2. 已确认约束（本设计硬边界）

1. 运行形态：本地单机、单用户、仅 `localhost` WebUI。
2. 技术路线：统一重构（方案 3），但算法行为冻结，不做识别/聚类策略优化。
3. 一致性目标：与当前原型“业务一致”，允许少量边缘样本差异。
4. 迁移策略：不兼容旧 `manifest/pipeline.db`，仅支持从源图库重新扫描建库。
5. 检测参数：`det_size` 固定为 `640`。
6. 并发策略：`workers` 默认 `max(1, cpu_count // 2)`。
7. 子进程批处理：保留，默认 `batch_size=300`，且 `batch_size` 约束的是“单轮总处理照片数”。
8. flip embedding：必须入库，不再写 JSON 缓存。
9. 路径模型：保留 `external_root`，但不再要求 `external_root/exports`。
10. 导出目录：模板 `output_root` 必须绝对路径，允许在 `external_root` 之外。
11. 待审核页面：删除，不保留独立待审核队列。
12. Identity Run 证据页：删除。
13. 人物首页：分“已命名人物 / 匿名人物”分区；不做搜索筛选。
14. 人物维护：在人物详情页完成，核心动作为重命名、单条/批量排除。
15. 排除语义：
    - 以后不能再自动归到被排除的人物；
    - 后续可归到其他人物。
16. 人物合并：首页多选批量合并；仅支持撤销“全局最近一次合并”。
17. 导出模板：只能选已命名人物。
18. Live Photo 导出：始终导出配对视频（不提供开关）。

## 3. 范围与非范围

### 3.1 范围内

- 本地工作区初始化与路径加载（workspace + external_root）。
- 多源目录扫描、阶段化处理、恢复执行。
- 人脸产物生成：`crop/aligned/context`。
- v5 归属引擎接入（冻结语义）。
- 人物库浏览、匿名/已命名分区、详情维护、批量合并与最近一次撤销。
- 导出模板、预览、执行、交付账本。
- Live Photo 配对导出。
- only/group 分桶与 `YYYY-MM` 目录组织。
- 运行日志与关键事件索引。

### 3.2 范围外

- 多用户协作、账号系统、远程访问。
- 视频内容识别或视频人脸分析。
- 新模型训练、阈值调优、det_size 1280 试验。
- 旧原型产物自动导入/迁移。

## 4. 总体架构

采用“单机本地服务 + 阶段化任务 + SQLite 真相”的产品结构：

- **数据真相层**：`library.db`（人物、归属、排除、扫描会话、导出账本）。
- **引擎层**：复用当前 v5 归类流程语义，作为 cluster/assignment 的执行核心。
- **任务编排层**：扫描分阶段执行（metadata/detect/embed/cluster/assignment），支持中断恢复。
- **Web/API 层**：人物库、人物详情、扫描页、导出页、日志页。
- **产物层**：`external_root/artifacts`、`external_root/logs`。

## 5. 路径与工作区模型

### 5.1 workspace（本机）

```text
<workspace>/
  .hikbox/
    library.db
    config.json
```

`config.json` 最小结构：

```json
{
  "version": 1,
  "external_root": "/absolute/path/to/external-root"
}
```

### 5.2 external_root（可网络盘）

```text
<external_root>/
  artifacts/
    crops/
    aligned/
    context/
    thumbs/
    ann/
  logs/
```

说明：

- 不定义固定 `exports/` 子目录。
- 导出目标路径仅由模板 `output_root` 决定。

## 6. 核心数据模型

> 下述为首版最小可落地结构，命名可与现有迁移风格对齐。

### 6.1 资产与扫描

- `library_source`
- `scan_session`
- `scan_session_source`
- `scan_checkpoint`
- `photo_asset`
- `face_observation`

`photo_asset` 关键字段：

- `primary_path`
- `primary_fingerprint`
- `fingerprint_algo`（固定 `sha256`）
- `file_size`
- `mtime_ns`
- `capture_datetime`
- `capture_month`
- `is_live_photo`
- `live_mov_path`
- `live_mov_size`
- `live_mov_mtime_ns`

`face_observation` 补充字段：

- `pending_reassign`（`0/1`，排除后置 `1`，下次 assignment 成功后清零）

### 6.2 向量与归属

- `face_embedding`
- `person`
- `person_face_assignment`
- `person_face_exclusion`

`face_embedding` 关键约束：

- `feature_type='face'`
- `variant in ('main', 'flip')`
- 唯一键：`(face_observation_id, feature_type, model_key, variant)`

`person_face_exclusion` 关键约束：

- 唯一键：`(person_id, face_observation_id, active)`（或等价唯一 active 约束）
- 用于“永不再自动归回该人物”的硬过滤。

### 6.3 合并与撤销

- `merge_operation`
- `merge_operation_member`

用途：

- 记录每次批量合并前后映射。
- 仅允许回滚“全局最近一次”。

### 6.4 导出

- `export_template`
- `export_template_person`
- `export_run`
- `export_delivery`

规则：

- `export_template_person` 仅允许关联“已命名人物”。
- `export_delivery` 作为增量补齐与跳过依据。

### 6.5 可观测

- `ops_event`

用于关键事件索引，不参与业务状态恢复。

## 7. 关键规则定义（精确定义）

### 7.1 `photo_asset.capture_datetime` 生成规则

在 metadata 阶段按以下优先级解析：

1. EXIF `DateTimeOriginal`（36867）
2. EXIF `DateTimeDigitized`（36868）
3. EXIF `DateTime`（306）
4. 文件 `birthtime`（若系统可用）
5. 文件 `mtime`

存储规则：

- `capture_datetime`：ISO8601 字符串。
- EXIF 无时区时，按运行机器本地时区解释。
- `capture_month`：基于 `capture_datetime` 的本地时区 `YYYY-MM`。

### 7.2 `primary_fingerprint` 规则

- 仅对静态照片（识别输入文件）计算。
- 算法：全文件内容 `sha256`，64 位小写十六进制。
- 首次入库必须计算。
- 增量扫描先比较 `(file_size, mtime_ns)`：
  - 未变化：复用旧指纹；
  - 变化：重算 `sha256`。

### 7.3 Live Photo 识别规则（仅配对，不识别视频）

当静态文件是 `HEIC` 时，必须在同目录查找隐藏 `MOV` 配对文件：

- 先取 `HEIC` 文件名的 `stem`（不含扩展名）。
- 匹配模式（后缀大小写不敏感）：
  - `^\.` + `stem` + `_[0-9]+\.mov$`
- 示例：
  - 图片：`IMG_8175.HEIC`
  - 视频：`.IMG_8175_1771856408349261.MOV`
- 若匹配到多个文件，按文件名中的数字时间戳降序选最大值；若时间戳相同，再按 `mtime_ns` 取最新。

行为：

- `MOV` 永远不参与人脸识别/聚类。
- 找到配对 `MOV`：导出时与 `HEIC` 同目录输出。
- 未找到：仍导出 `HEIC`，并在运行摘要记录一条 `warning`。

### 7.4 排除与再归属规则

执行“排除”时，同事务执行：

1. 将该 observation 对当前人物的 active assignment 置为 inactive；
2. 写入/激活 `person_face_exclusion(person_id, observation_id)`。

后续自动归属：

- 候选人物若命中 active exclusion，直接硬过滤。
- observation 仍可归入其他人物。

下次 scan 的再处理集合包含：

- 本次新增/变更资产产生的 observation；
- 无 active assignment 的 observation；
- `pending_reassign=1` observation（由排除动作标记）。

### 7.5 only/group 分桶规则

对“命中模板人物集合”的照片：

- `selected_faces`：该照片中属于模板人物的 active assignment。
- `selected_min_area`：`selected_faces` 的最小脸框面积。
- `significant_extra_threshold = selected_min_area / 4`。

判定：

- 满足任一条件则归 `group`：
  1. 存在不属于模板人物的额外人脸，且面积 `>= threshold`；
  2. 存在未归属人脸，且面积 `>= threshold`；
  3. 存在额外人脸面积缺失。
- 其他情况归 `only`。

### 7.6 导出目录组织规则（`YYYY-MM`）

导出根目录为模板 `output_root`（绝对路径），结构固定：

```text
<output_root>/
  only/
    YYYY-MM/
  group/
    YYYY-MM/
```

- `YYYY-MM` 取 `capture_datetime`；若缺失则回退文件 `mtime`。
- 静态图输出到对应 bucket/month 目录。
- 若该静态图匹配到 Live `MOV`，则 `MOV` 同目录输出。

## 8. 扫描执行与并发分批

### 8.1 阶段

`discover -> metadata -> detect -> embed -> cluster -> assignment`

### 8.2 子进程批处理模型

- 主进程做调度和统一写库。
- 每轮最多取 `batch_size` 张待处理照片（默认 300）。
- 若并发 `workers=N`，则将本轮总量均分给 N 个子进程。
  - 例：`batch_size=300, workers=3`，每子进程约 100 张。
- 子进程职责：
  - 加载检测模型；
  - 生成 `crop/aligned/context`；
  - 输出批次结果；
  - 显式释放模型并退出。
- 主进程汇总结果并落库后再进入下一轮。

### 8.3 默认参数

- `det_size=640`（固定）
- `workers=max(1, cpu_count // 2)`
- `batch_size=300`

## 9. WebUI 交互设计

### 9.1 导航

保留：

- 人物库
- 源目录与扫描
- 导出模板
- 运行日志

删除：

- 待审核
- Identity Run 证据页

### 9.2 人物库首页

- 两个分区：已命名人物、匿名人物。
- 无搜索与筛选。
- 支持多选人物批量合并。
- 提供“撤销最近一次合并”入口（仅全局最近一次）。

### 9.3 人物详情页

- 主视图为 active 样本网格。
- 每个样本默认显示 `context`。
- 点击样本后展开为 `crop + context`（统一预览器组件形态）。
- 若样本为 Live Photo，`context` 区域显示 `Live` 标签。
- 支持单条排除与批量排除。

### 9.4 源目录与扫描页

- 展示扫描会话状态、source 级进度、失败计数。
- 操作：恢复、停止、放弃并新建。
- 明确显示当前执行参数（workers、batch_size、det_size）。

### 9.5 导出模板页

- 创建/编辑/预览/执行/历史一体化。
- 人物选择器仅展示已命名人物。
- 不提供 Live 开关（始终导出配对 MOV）。
- 展示 only/group 统计与样例。

### 9.6 运行日志页

- 展示关键运行事件。
- 支持按 run 维度筛查。

## 10. API 与模块边界（首版）

### 10.1 Web 页面路由

- `GET /`
- `GET /people/{id}`
- `GET /sources`
- `GET /exports`
- `GET /logs`

### 10.2 关键动作 API

- `POST /api/scan/start_or_resume`
- `POST /api/scan/abort`
- `POST /api/scan/start_new`
- `POST /api/people/{id}/actions/rename`
- `POST /api/people/{id}/actions/exclude-assignment`
- `POST /api/people/{id}/actions/exclude-assignments`
- `POST /api/people/actions/merge-batch`
- `POST /api/people/actions/undo-last-merge`
- `GET/POST/PUT/DELETE /api/export/templates...`
- `POST /api/export/templates/{id}/actions/run`

### 10.3 媒体读取 API

- `GET /api/observations/{id}/crop`
- `GET /api/observations/{id}/context`
- `GET /api/photos/{id}/preview`

说明：首版页面不再依赖 `original` 视图，接口可按实现保留或后续清理。

## 11. 失败处理与可恢复性

- 单张图片失败不阻断整批扫描。
- 子进程崩溃仅回滚当前批次，已完成批次保持有效。
- 恢复时优先续跑最近未完成会话。
- 排除、合并、撤销最近一次合并均要求事务一致性。
- Live `MOV` 缺失不影响静态图导出，记录 warning。

## 12. 验收标准

1. 新工作区初始化后，DB 固定在本机 `workspace/.hikbox/library.db`。
2. `crop/aligned/context` 与日志写入 `external_root`。
3. `scan` 默认使用 `workers=max(1,cpu//2)` 与 `batch_size=300`。
4. 子进程批处理可观察到“每批加载-处理-释放-退出”行为。
5. `face_embedding` 中同 observation 同 model 同 feature 至少有 `main/flip` 两条记录。
6. 人物首页仅两分区（已命名/匿名），无搜索筛选。
7. 无独立待审核页、无 identity tuning 页面。
8. 人物详情支持单条/批量排除；被排除样本不会再自动归回原人物。
9. 首页支持批量合并与“撤销最近一次合并”。
10. 导出模板仅可选择已命名人物。
11. Live 配对规则按隐藏 `MOV` 模式生效，后缀大小写不敏感。
12. 导出目录为 `only/group/YYYY-MM`。

## 13. 风险与后续

### 13.1 已知风险

- 统一重构路径改动面大，需要强化回归测试。
- 网络盘 I/O 波动可能拉长导出耗时。
- 批量合并撤销仅支持“最近一次”，需要在 UI 上清晰提示边界。

### 13.2 后续可选扩展（不在首版）

- 原图预览层的按需回归。
- 更细粒度合并撤销历史。
- 更强的导出表达式能力。
