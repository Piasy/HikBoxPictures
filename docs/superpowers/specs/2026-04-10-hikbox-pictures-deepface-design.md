# HikBox Pictures DeepFace 迁移设计文档

## 目标

将当前基于 `InsightFace` 的双人照片筛选流程迁移到 `DeepFace`，同时保持工具的本地 CLI 形态、输出目录结构、Live Photo `MOV` 复制逻辑、时间元数据处理逻辑，以及 `only-two` / `group` 分桶语义不变。

迁移后的工具需要满足：

- 主 CLI、距离调试脚本、人脸裁剪脚本统一使用 `DeepFace` 作为检测与 embedding 来源。
- 继续支持 `--ref-a-dir` 和 `--ref-b-dir` 两组参考图目录。
- 候选图中的每张人脸都要分别计算它到 A、B 两组参考图的距离，并继续要求命中 A 和 B 的必须是两张不同的人脸。
- 对用户暴露最关键的效果调优参数：识别模型、检测后端、距离度量、对齐开关、距离阈值。

## 范围

范围内：

- 用 `DeepFace` 替换 `InsightFace` 作为检测与 embedding 引擎。
- 保留当前参考图目录输入模式，不引入身份库持久化。
- 主 CLI、`scripts/inspect_distances.py`、`scripts/extract_faces.py` 统一复用同一个 `DeepFaceEngine` 边界。
- 暴露 `--model-name`、`--detector-backend`、`--distance-metric`、`--distance-threshold`、`--align` / `--no-align` 这组核心调参项。
- 更新安装脚本、README、测试和错误处理。

范围外：

- 保留 `InsightFace` / `DeepFace` 双引擎切换能力。
- 引入 `DeepFace.find` 驱动的持久化数据库模式。
- Apple Photos / PhotoKit 集成。
- 视频内容分析。
- GPU 推理配置。
- DeepFace 其他能力，例如年龄、性别、情绪、反欺骗等。

## 已选方案

采用“使用 `DeepFace.represent` 提取每张脸的 embedding 和检测框，再用 DeepFace 自带距离与阈值逻辑完成判定”的迁移方案。

关键决策：

- 不以 `DeepFace.verify` 作为主流程接口。`verify` 本质上也是“检测 -> embedding -> 距离 -> 阈值”的封装，但更偏向两张图片之间的高层判定，不适合当前双参考组、多候选脸、需要 distinct faces 约束的业务逻辑。
- 不以 `DeepFace.find` 作为主流程接口。`find` 更适合“查询图对数据库”的有状态识别流程，而本项目更需要无状态、可控、显式的参考图目录匹配逻辑。
- 主流程继续保留当前“参考图预加载 -> 候选图逐张评估 -> distinct faces 判断 -> only-two/group 分桶”的业务结构，只替换底层检测、embedding、距离和阈值来源。
- `scripts/extract_faces.py` 一并迁移，但只替换检测来源，不改裁剪规则、输出结构、命名规则和统计口径。

不选其他方案的原因：

- 不保留双引擎共存，避免短期内维护两套效果语义和测试夹具。
- 不直接套 `find` 的数据库语义，避免参考图目录出现额外的 `pkl` 副作用、弱化当前“参考图必须恰好一张脸”的质量门槛。
- 不一步到位暴露 DeepFace 所有参数，避免首版 CLI 过于复杂。

## 运行时与依赖约束

- Python 依赖从 `insightface` / `onnxruntime` 切换为 `deepface` 及其所需依赖。
- DeepFace 是一个聚合框架，底层模型与 detector 的许可和下载行为依赖其上游组件；README 中需要明确提示用户在生产或商业场景下自行核对模型许可。
- 首次运行可能触发模型下载，需联网，首次启动会明显慢于后续运行。
- 本仓库仍然优先面向 macOS 本地 CLI，不引入服务端部署和 HTTP API 模式。

## 默认参数策略

默认配置直接影响迁移后的效果基线，必须明确：

- 默认 `model_name`：`ArcFace`
- 默认 `detector_backend`：`retinaface`
- 默认 `distance_metric`：`cosine`
- 默认 `align`：开启
- 默认 `distance_threshold`：不在 CLI 中写死，未显式传入时使用 DeepFace 的 `find_threshold(model_name, distance_metric)` 预调阈值

这样做的原因：

- 识别模型继续维持在 ArcFace 这一代际，便于把变量主要集中在引擎与 detector 的变化上。
- `retinaface` 通常比轻量检测器更稳，更适合作为首个迁移基线。
- `cosine` 是 DeepFace 的常见默认度量，也更贴合其预调阈值体系。

## CLI 接口

迁移后的 CLI 形态为：

```bash
hikbox-pictures \
  --input /path/to/photo-library \
  --ref-a-dir /path/to/person-a-dir \
  --ref-b-dir /path/to/person-b-dir \
  --output /path/to/output \
  --model-name ArcFace \
  --detector-backend retinaface \
  --distance-metric cosine \
  --align
```

接口约束：

- `--input`、`--ref-a-dir`、`--ref-b-dir`、`--output` 继续保留。
- 新增 `--model-name`，默认 `ArcFace`。
- 新增 `--detector-backend`，默认 `retinaface`。
- 新增 `--distance-metric`，默认 `cosine`。
- 新增 `--distance-threshold`，可选；如果不传，则使用 DeepFace 的预调阈值。
- 新增 `--align` / `--no-align` 成对开关，默认开启。
- 首版不暴露 `expand_percentage`、`normalization`、`anti_spoofing`，这些只在内部保留为实现细节。

## 用户流程

1. CLI 校验输入目录、两个人物参考目录和输出目录。
2. CLI 解析识别模型、检测后端、距离度量、对齐开关和可选阈值。
3. 创建一个 `DeepFaceEngine` 实例，持有整次运行的识别配置。
4. 递归扫描 `ref-a-dir` 和 `ref-b-dir` 中的受支持参考图。
5. 对每张参考图调用 `DeepFaceEngine.detect_faces` 提取检测框与 embedding。
6. 校验每张参考图必须且仅能检测到 1 张脸。
7. 将 A、B 两组参考 embedding 保留为两个列表。
8. 递归扫描输入目录中的候选图片。
9. 对每张候选图提取全部人脸与 embedding。
10. 计算候选图中每张脸到 A、B 两组参考 embedding 的最小距离。
11. 如果存在两张不同的人脸分别命中 A 与 B，则这张图命中。
12. 按原有规则导出到 `only-two/YYYY-MM` 或 `group/YYYY-MM`，并复制配对的 Live Photo `MOV`。

## 架构

### 1. `deepface_engine.py`

这是本次迁移的核心边界，职责如下：

- 封装对 `DeepFace.represent` 的调用。
- 保存当前运行配置：`model_name`、`detector_backend`、`distance_metric`、`align`、显式阈值或默认阈值来源。
- 提供统一的 `detect_faces(image_path)` 接口，返回仓库内部统一的 `DetectedFace` 结构。
- 将 DeepFace 的 `facial_area`（`x/y/w/h`）转换为仓库当前使用的 `(top, right, bottom, left)` bbox 语义。
- 封装距离计算和默认阈值查询，避免上层直接依赖 DeepFace 内部模块。

除该模块外，CLI、匹配器、参考加载器、调试脚本和裁脸脚本都不应直接使用 DeepFace 原始 API。

### 2. `reference_loader.py`

职责保持“参考目录加载器”，但底层实现切到 `DeepFaceEngine`：

- 递归扫描参考目录。
- 过滤受支持图片扩展名。
- 逐张提取 embedding。
- 对任一参考图出现 `0` 张脸或 `>1` 张脸直接报错。
- 为每个人物返回一组 embedding 和来源文件列表。

### 3. `matcher.py`

职责保持“候选图评估器”，但距离逻辑改为跟随 `DeepFaceEngine`：

- 对每张候选图提取全部人脸与 embedding。
- 对每张候选脸分别计算到 A、B 两组参考 embedding 的最小距离。
- 判断某张脸是否命中 A 或 B 时，不再调用写死的欧氏距离，而是统一走引擎的距离度量与阈值策略。
- 继续要求 A 命中集合和 B 命中集合中必须存在不同的人脸索引。
- 继续保留 `ONLY_TWO` / `GROUP` 分桶逻辑和“大额外人脸”判定逻辑。

### 4. `cli.py`

职责仍是编排层，但需要改成：

- 解析新增的 DeepFace 参数。
- 初始化并复用一个 `DeepFaceEngine` 实例。
- 将 engine 传给参考图加载器和候选图评估逻辑。
- 在运行摘要和错误处理里继续保持现有统计口径。

### 5. `scripts/inspect_distances.py`

职责保持调试脚本定位，但需要迁移为：

- 统一使用 `DeepFaceEngine` 获取候选脸和 embedding。
- 打印当前使用的 `model_name`、`detector_backend`、`distance_metric`、是否显式传入阈值，以及最终阈值值。
- 输出每张候选脸到 A/B 参考组的最小距离。
- 继续支持带人脸框与距离标注的临时图片输出。

### 6. `scripts/extract_faces.py`

职责保持“递归扫描并裁剪所有人脸”的辅助脚本定位，但检测实现切到 `DeepFaceEngine`：

- 用统一的 `detect_faces` 结果作为裁剪框来源。
- 保持当前的方形外扩裁剪、越界补黑、输出尺寸、输出结构和命名规则不变。
- 继续默认跳过输出目录，避免递归处理自己生成的 PNG。
- 不引入 embedding 匹配逻辑，只消费 bbox。

## 参考图规则

参考目录规则保持严格：

- 递归扫描目录中的 `HEIC`、`JPG`、`JPEG`、`PNG`。
- 忽略不支持的文件。
- 目录中至少要有一张可用参考图。
- 每张参考图必须恰好检测到一张脸。
- 参考图不做均值聚合或中心向量聚合。

继续使用“目录中所有参考 embedding 取最小距离”的策略，而不是做 embedding 均值，是为了保留同一个人在不同姿态、光照和表情下的多样性，避免平均后损失有效特征。

## 匹配与距离规则

候选图的匹配规则为：

- 对候选图中每张人脸，分别计算：
  - 到 A 组所有参考 embedding 的最小距离。
  - 到 B 组所有参考 embedding 的最小距离。
- 如果某张候选脸对 A 的最小距离小于等于阈值，则记入 A 命中集合。
- 如果某张候选脸对 B 的最小距离小于等于阈值，则记入 B 命中集合。
- 只有当 A 和 B 命中集合中存在不同的人脸索引时，这张候选图才算命中。

分类规则保持不变：

- 检测到的人脸总数正好为 `2` 时，分类为 `only-two`。
- 检测到的人脸总数大于 `2` 时，继续使用现有“主匹配对 + 较大额外人脸”逻辑决定落入 `group` 还是 `only-two`。

“较大额外人脸”规则需要完整保留，具体为：

- 先从所有满足条件的 A/B 命中组合中，筛出“索引不同，且两张脸都能拿到面积”的候选人脸对。
- 将候选人脸框面积定义为 `宽 * 高`。
- 在这些候选对中，选择“两张脸面积之和最大”的一对作为主匹配对。
- 如果无法选出主匹配对，例如缺少可用 bbox 或面积信息，则保守地将这张图片归为 `group`。
- 对主匹配对之外的每一张额外人脸，计算它的人脸框面积；如果面积信息缺失，同样保守地归为 `group`。
- 额外人脸的判定阈值使用“主匹配对中较小那张脸面积的四分之一”。
- 只要存在任意一张额外人脸的面积大于等于这个阈值，就认为图中还存在足够明显的第三人或更多人，归类为 `group`。
- 只有当所有额外人脸都小于这个阈值时，才认为这些额外检测更接近远景小脸、边缘小脸或噪声，允许该图片仍归为 `only-two`。

这个规则的意图是：在多人合照里，只要第三张脸达到“与主角脸相比仍然足够显著”的程度，就应该进入 `group`；但如果多出来的脸非常小，则保留进入 `only-two` 的机会，避免被微小背景人脸过度影响。

距离规则约定：

- 默认走 `DeepFace` 的 `find_distance` 和 `find_threshold` 语义。
- 当用户显式传入 `--distance-threshold` 时，覆盖默认阈值。
- 主 CLI、距离调试脚本和测试必须复用同一套距离计算和阈值来源，避免“调试看到的距离”和“实际命中规则”不一致。

## 输出与导出行为

下列行为不变：

- 输出目录仍为：

```text
/output/
  only-two/
    YYYY-MM/
  group/
    YYYY-MM/
```

- 命中 `HEIC` 时仍查找并复制隐藏配对 `MOV`。
- 年月目录仍按 EXIF -> 文件创建时间 -> 文件修改时间解析。
- 导出时仍保留修改时间并尽力保留创建时间。
- 文件名冲突仍通过稳定后缀规避覆盖。

## 安装与首次运行行为

安装与文档需要同步调整：

- `pyproject.toml` 中移除 `insightface` / `onnxruntime` 依赖，切换到 `deepface` 所需依赖集合。
- `scripts/install.sh` 的提示文案需要从 InsightFace 路线切到 DeepFace 路线。
- README 要明确说明：
  - 首次运行可能下载模型，需要联网。
  - 可通过 `--model-name`、`--detector-backend`、`--distance-metric`、`--distance-threshold` 调参。
  - `scripts/inspect_distances.py` 与 `scripts/extract_faces.py` 都已经跟随主流程迁移。
  - DeepFace 及其底层模型的许可需要用户自行核对。

## 错误处理

致命错误：

- `--ref-a-dir` / `--ref-b-dir` 不存在或不是目录。
- 参考目录中没有任何可用图片。
- 任一参考图检测到 `0` 张脸。
- 任一参考图检测到多张脸。
- `DeepFaceEngine` 初始化失败。
- 模型下载失败或加载失败。

非致命错误：

- 候选图片解码失败。
- 候选图片未检测到人脸。
- 命中的 `HEIC` 缺失配对 `MOV`。
- 单张候选图片在推理过程中失败。
- `extract_faces.py` 中单张图片检测失败或解码失败。

CLI 摘要维持现有统计结构：

- 扫描文件数。
- `only-two` 命中数。
- `group` 命中数。
- 解码失败数。
- 无人脸图片数。
- 缺失 Live Photo `MOV` 数。
- warning 列表。

## 测试策略

测试需要整体迁移到新边界，但仍然尽量避免依赖真实 DeepFace 推理：

1. 引擎边界
- `DeepFaceEngine` 能正确透传 `model_name`、`detector_backend`、`distance_metric`、`align` 和阈值配置。
- `detect_faces` 能把 `facial_area` 转成统一 bbox。
- 距离与默认阈值查询能正确走到 DeepFace 封装层。
- 初始化或推理异常能被转化为明确错误。

2. 参考目录加载
- 目录递归扫描受支持图片。
- 空目录失败。
- 参考图 `0 / 1 / 多张脸` 行为正确。

3. 多参考图匹配
- 目录级匹配使用最小距离而不是均值。
- 同一张脸同时接近 A/B 时，不算命中。
- 两张不同脸分别命中 A/B 时，按人脸总数正确分桶。
- 显式阈值覆盖默认阈值。

4. CLI 编排
- 新参数 `--model-name`、`--detector-backend`、`--distance-metric`、`--distance-threshold`、`--align` / `--no-align` 生效。
- 参考目录错误仍是致命错误。
- 摘要统计行为不回归。

5. 距离调试脚本
- 与主流程复用同一引擎。
- 打印当前模型、detector、metric 和阈值信息。
- 继续输出每张脸到 A/B 的最小距离。
- 继续支持标注图输出。

6. 人脸裁剪脚本
- 使用 `DeepFaceEngine.detect_faces` 获取裁剪框。
- 外扩裁剪、补边、输出尺寸和命名规则保持不变。
- 默认跳过输出目录，避免递归处理生成结果。
- 无人脸图片、检测失败、解码失败的统计行为保持可观测。

7. 回归验证
- Live Photo 配对规则不变。
- 输出目录结构不变。
- 时间元数据回退顺序不变。

## 迁移影响

这次迁移是一个实现级别替换，但不打算把用户主流程改成全新产品形态：

- CLI 仍然使用参考图目录输入，而不是数据库或身份库。
- 调试能力增强为可直接围绕 DeepFace 的模型、detector 和距离度量做 A/B。
- `scripts/extract_faces.py` 和 `scripts/inspect_distances.py` 的结果将更贴近主流程，因为三者共享同一个检测来源。

以下用户可见行为保持稳定：

- 仍是本地 macOS CLI。
- 仍按 `only-two` / `group` 和 `YYYY-MM` 导出。
- 仍复制匹配的 Live Photo `MOV`。
- 仍要求参考图必须足够干净且只能有一张脸。

## 风险与约束

- DeepFace 依赖链相对更重，安装问题可能集中在 TensorFlow、OpenCV 及其上游依赖。
- 首次运行引入网络依赖，离线环境下首次运行会失败。
- 不同 `model_name + distance_metric` 组合的距离尺度不同，旧经验阈值不可直接复用。
- 多参考图虽然能提升召回，但也会增加误命中风险，因此保留并强化 `inspect_distances.py` 是必要的。
- `retinaface` 作为默认 detector 更稳，但速度可能慢于轻量 detector，后续可能需要通过配置指导用户折中速度与精度。

## 成功标准

满足以下条件则视为本次迁移成功：

- 用户可以通过 `--ref-a-dir` / `--ref-b-dir` 提供两组参考图目录。
- 主 CLI 基于 DeepFace 在本地完成检测、embedding、距离计算与命中判定。
- 用户可以通过 `--model-name`、`--detector-backend`、`--distance-metric`、`--distance-threshold`、`--align` / `--no-align` 做效果调优。
- 主 CLI、距离调试脚本和人脸裁剪脚本统一复用 `DeepFaceEngine`，不再出现两套检测来源。
- 参考图目录继续按“每张参考图必须恰好一张脸”的规则校验。
- 输出结构、Live Photo 复制和时间元数据行为保持不变。
