# Immich v6 人物图库产品化 Spec

## Goal

把 `hikbox_pictures/immich_face_single_file.py` 中已验证的 Immich v6 风格在线人物归属原型，产品化为本地单机智能相册人物库：通过 CLI 初始化、登记源目录、启动可恢复扫描；通过 WebUI 浏览人物、命名、合并、排除误归属，并创建模板导出同时包含指定已命名人物的照片。

## Global Constraints

- 产品语义以 `hikbox_pictures/immich_face_single_file.py` 和 `docs/group_pics_algo.md` 的 v6 章节为准。
- 功能按 feature slice 独立叠加交付；每个 slice 都必须通过公共入口观察真实 DB、文件、UI 或导出结果。
- 核心验收必须使用用户提供的小图库、真实 InsightFace 模型、真实 SQLite、真实 crop/context/export 文件；假后端只允许作为补充单元测试，不能替代验收。
- CLI 只负责 `init`、`source`、`scan start`、`serve`；人物维护和导出只通过 WebUI/API 操作。
- 所有 CLI 命令都必须显式传入 `--workspace <path>`。
- 扫描运行期间禁止启动 WebUI；存在 `running|aborting` 扫描会话时，`hikbox serve --workspace <path>` 必须失败退出且不监听端口。
- WebUI 仅面向本机单用户、`localhost` 使用；首版不做账号系统、多用户协作、远程访问和多标签页一致性保障。
- WebUI 使用 FastAPI + Jinja2 服务端渲染，可用少量原生 JS 增强表单提交、局部刷新和确认弹窗；首版不引入 React/Vue 等前端框架。
- 页面视觉验收使用 Playwright 跑 Chromium 或 Chrome，优先选择本地和 CI 更容易安装的一种；截图、JSON 报告和服务日志保存到 `.tmp/<task-name>/`。
- 每个 slice 都必须包含自己的可观测状态、错误边界、日志或账本记录、manifest 驱动验收；不单独设置“可观测性”交付 slice。
- 不允许用 mock、硬编码、no-op、直接改库或绕过公共入口的方式满足核心验收。
- 任何数据库 schema 修改都必须同步更新 `docs/db_schema.md`。

## 验收集 Manifest

最终验收使用用户提供的小图库和一份人工标注 manifest。manifest 是验收输入契约，至少包含：

- `people`：人物标签、期望显示名、是否期望扫描后自动形成匿名人物。
- `assets`：照片文件名、拍摄月份、应包含的人物标签、是否存在 Live MOV 配对。
- `expected_person_groups`：扫描后哪些照片或 face 应归到同一个人物标签。
- `expected_exports`：模板选择哪些人物时，哪些文件应导出到 `only/YYYY-MM` 或 `group/YYYY-MM`。
- `tolerances`：允许不计入自动断言的边界照片或边界 face，避免真实模型偶发差异阻塞核心流程验收。

验收流程中，人物命名必须通过 WebUI 完成。测试可先根据 `expected_person_groups` 在扫描结果里定位自动形成的匿名 person，再用 Playwright 打开人物详情页提交命名表单，之后再执行合并、排除或导出模板验收。

## Feature Slice 1: 工作区与源目录

- [ ] Status: Not done

### Behavior

- `hikbox init --workspace <path> --external-root <path>` 创建全新的 `workspace/.hikbox/config.json`、`workspace/.hikbox/library.db`、`workspace/.hikbox/embedding.db`。
- `config.json` 保存 `external_root` 的绝对路径。
- 初始化时创建 `external_root/artifacts/crops`、`external_root/artifacts/context`、`external_root/logs`。
- `hikbox source add --workspace <path> <source-path> --label <label>` 登记一个照片源目录。
- `hikbox source list --workspace <path>` 列出已登记源目录。
- `init` 发现目标工作区已存在 `.hikbox`、`library.db`、`embedding.db` 或 `config.json` 时直接失败，不复用、不迁移。
- 重复登记同一个源目录绝对路径必须失败，不产生第二条 source 记录。

### Public Interface

- CLI：`hikbox init --workspace <path> --external-root <path>`。
- CLI：`hikbox source add --workspace <path> <source-path> --label <label>`。
- CLI：`hikbox source list --workspace <path>`。
- 文件：`workspace/.hikbox/config.json`、`workspace/.hikbox/library.db`、`workspace/.hikbox/embedding.db`。
- 目录：`external_root/artifacts/crops`、`external_root/artifacts/context`、`external_root/logs`。

### Error and Boundary Cases

- `--workspace` 缺失时返回参数错误。
- `--external-root` 缺失或不可创建时返回可读错误。
- 源目录不存在、不可读或不是目录时返回可读错误。
- 重复 `init` 返回非 0 退出码，不修改已有文件。
- 重复 source 返回非 0 退出码，不新增记录。

### Non-goals

- 不做旧数据导入、自动迁移或 schema 升级。
- 不做远程 source、云盘鉴权或跨机器同步。

### Acceptance Criteria

- AC-1：真实文件系统执行 `hikbox init --workspace <ws> --external-root <ext>` 后，config、两个 DB、crops/context/logs 目录都存在。
- AC-2：`config.json` 中的 `external_root` 是绝对路径。
- AC-3：执行 `hikbox source add --workspace <ws> <source> --label family` 后，`library.db` 中有一条绝对路径 source 记录。
- AC-4：重复 `init`、重复 source、无效 source 都返回非 0 退出码和可读错误，且不产生重复 DB 记录。
- AC-5：`external_root/logs` 中记录 init/source 操作结果。

### Automated Verification

- CLI 集成测试在临时真实目录中执行 init/source/list，读取文件系统和 SQLite 断言结果。
- 错误用例必须通过 CLI 公共入口触发，不能直接调用内部函数或直接改 DB。

### Done When

- 所有 acceptance criteria 通过自动化验证。
- 没有任何核心要求由硬编码、no-op、直接状态修改或 fake integration 满足。

## Feature Slice 2: 批次可恢复检测入库

- [ ] Status: Not done

### Behavior

- `hikbox scan start --workspace <path>` 在本 slice 完成 discover、metadata、detect+embed+artifact 入库。
- 支持照片后缀 `jpg/jpeg/png/heic/heif`，大小写不敏感。
- metadata 阶段只为 `heic/heif` 尝试 Live Photo 配对；匹配同目录隐藏 MOV，形如 `IMG_8175.HEIC` 对应 `.IMG_8175_1771856408349261.MOV`，`heic/heif/mov` 后缀大小写均不敏感。
- `jpg/jpeg/png` 不尝试 Live MOV 配对，即使同目录存在相似 MOV 也不写入配对关系。
- 默认每批 200 张照片。
- 每批启动一个子进程串行处理；子进程加载真实 InsightFace 模型，处理本批，返回结果后退出，以释放模型和内存。
- 子进程对每张照片执行 EXIF 方向纠正、真实 InsightFace 检测、512 维 embedding 提取，并生成 face crop 与 480p context 图。
- context 图是整图按比例缩放到最长边不超过 480 后画出本图人脸框，不是原图局部裁剪。
- 主进程只有在整批成功后，才统一写入照片、face observation、embedding、crop/context 路径，并把批次标记为 completed。
- 批处理中断、子进程失败或主进程提交前失败时，该批不得标记 completed。
- 再次执行同一个 `hikbox scan start --workspace <path>` 时，必须跳过 completed 批次，整批重跑未完成批次。

### Public Interface

- CLI：`hikbox scan start --workspace <path>`。
- DB：`library.db` 中的 asset、face observation、scan session、scan batch、Live MOV 元数据、crop/context 路径。
- DB：`embedding.db` 中每个 face 的 512 维 embedding。
- 文件：`external_root/artifacts/crops` 与 `external_root/artifacts/context` 下的真实图片。
- 日志：`external_root/logs` 中的扫描阶段、批次开始、批次完成、批次失败记录。

### Prototype Source

- 向量归一化来源：`hikbox_pictures/immich_face_single_file.py:21`。
- EXIF 方向纠正与 RGB 读取来源：`hikbox_pictures/immich_face_single_file.py:29`。
- bbox IoU 与归一化 bbox 来源：`hikbox_pictures/immich_face_single_file.py:35`。
- `DetectedFace` 数据结构来源：`hikbox_pictures/immich_face_single_file.py:70`。
- InsightFace `buffalo_l` 后端、`det_10g.onnx`、`w600k_r50.onnx`、`input_size=(640, 640)`、BGR 转换、`norm_crop`、embedding 提取来源：`hikbox_pictures/immich_face_single_file.py:386`。
- 实现可以 copy 这些代码，也可以逻辑等价重写；验收必须证明行为一致。

### Error and Boundary Cases

- 无 source 时 `scan start` 返回可读错误。
- 缺少 InsightFace 模型文件时返回可读错误，不能创建 completed 批次。
- 单张照片读取失败时记录 asset 级失败，不阻断整批其它照片；该失败必须进入扫描摘要。
- 子进程崩溃时，本批保持未完成，下次 `scan start` 整批重跑。
- 已完成批次再次扫描不得重复写 face、embedding、crop/context 文件。

### Non-goals

- 不做多子进程并行处理。
- 不做人脸归属、人物创建、命名、合并、排除或导出。
- 不识别视频内容；MOV 只作为 HEIC/HEIF 的 Live Photo 配对文件。

### Acceptance Criteria

- AC-1：小图库真实模型扫描后，`library.db` 有 asset、face observation、Live MOV 元数据、crop/context 路径。
- AC-2：`embedding.db` 中每个 face 都有 512 维归一化向量。
- AC-3：每个成功检测到 face 的 crop/context 文件真实存在；context 最长边不超过 480，且能看到整图上的人脸框。
- AC-4：HEIC/HEIF 大小写变体能匹配同目录隐藏 MOV；JPG/PNG 不匹配 MOV。
- AC-5：失败注入让第 2 批在主进程提交前失败后，再次执行 `scan start --workspace <ws>`，第 1 批不重复检测、不重复写入，第 2 批整批重跑并完成。

### Automated Verification

- 真实小图库验收运行 `hikbox scan start --workspace <ws>`，读取 SQLite、产物目录和日志断言结果。
- 批次恢复测试通过公共 CLI 触发，并用可控失败注入模拟子进程或提交前失败。
- artifact 验证读取真实图片尺寸与像素内容，确认 context 为 480p 整图加框。

### Done When

- 所有 acceptance criteria 通过自动化验证。
- 没有任何核心要求由硬编码、no-op、直接状态修改或 fake integration 满足。

## Feature Slice 3: v6 在线人物归属

- [ ] Status: Not done

### Behavior

- 本 slice 在检测入库之后，由同一个 `hikbox scan start --workspace <path>` 继续触发 assignment。
- assignment 输入是已入库且待归属的 face observation，不重新读取照片，不重新调用检测模型。
- 主进程从 `embedding.db` 恢复所有可参与检索的 face embedding，构建 HNSW cosine 索引。
- 对待归属 face 按原型在线语义执行：新 face 先进入索引，再近邻搜索。
- 默认参数沿用原型：`min_score=0.7`、`max_distance=0.5`、`min_faces=3`。
- 近邻不足时不建人物、不归属；达到 `min_faces` 且没有已有 person 时创建匿名 person；能命中已有 person 时写 active assignment。
- 未达到 `min_faces` 的未归属 face 不进入人物库匿名区。
- 同一 asset 重检时保留原型语义：归一化 bbox IoU `> 0.5` 的 face 复用旧记录，不刷新旧 embedding/person；未被新检测框匹配的旧 face 删除；新增 face 入 pending recognition。首版不提供公开 force 重检入口，但内部能力必须可测试。
- 每次 assignment 记录参数快照和摘要，便于定位真实小图库验收差异。

### Public Interface

- CLI：`hikbox scan start --workspace <path>`。
- DB：`library.db` 中的 person、active assignment、assignment run、参数快照、归属摘要。
- DB：`embedding.db` 作为 assignment 的向量输入。
- 日志：assignment 开始、完成、跳过、失败和摘要。

### Prototype Source

- HNSW cosine 搜索索引来源：`hikbox_pictures/immich_face_single_file.py:131`。
- 引擎默认参数来源：`hikbox_pictures/immich_face_single_file.py:203`。
- detect 时同图 IoU 复用、新 face 入索引与 pending recognition、未匹配旧 face 删除来源：`hikbox_pictures/immich_face_single_file.py:249`。
- 在线归属主逻辑来源：`hikbox_pictures/immich_face_single_file.py:292`。
- pending recognition 队列处理来源：`hikbox_pictures/immich_face_single_file.py:324`。
- 创建/迁移 person assignment 来源：`hikbox_pictures/immich_face_single_file.py:334`。
- `_remove_face` 删除 face、索引、pending、person 空壳来源：`hikbox_pictures/immich_face_single_file.py:351`。
- `_match_existing_face` 的 IoU `> 0.5` 复用规则来源：`hikbox_pictures/immich_face_single_file.py:366`。
- 实现可以 copy 这些代码，也可以逻辑等价重写；验收必须证明行为一致。

### Error and Boundary Cases

- `embedding.db` 缺失、face 缺 embedding 或 embedding 维度不是 512 时，assignment 失败并记录可读错误。
- 重复执行 `scan start` 不得重复创建 person，不得重复 active assignment。
- 已经归属的 face 再次进入 assignment 时必须幂等跳过或保持同一 active assignment。
- 小图库中低于 `min_faces` 的人物不得误创建匿名 person。

### Non-goals

- 不做离线全量聚类、AHC、HDBSCAN、person consensus 或 v5 归属链路。
- 不做受控 exemplar memory、多近邻投票、best-vs-second-best margin 或边界样本观察期。
- 不提供公开 force 重检 CLI。

### Acceptance Criteria

- AC-1：manifest 期望自动成组的人物在扫描后形成对应匿名 person，相关 face 有 active assignment。
- AC-2：manifest 标记为不足 `min_faces` 或容忍边界的 face 不创建匿名人物，不出现在匿名人物区。
- AC-3：重复执行 `hikbox scan start --workspace <ws>` 不重复创建 person，不重复 active assignment。
- AC-4：assignment run 记录参数快照和摘要，包含 `min_score=0.7`、`max_distance=0.5`、`min_faces=3`。
- AC-5：单元测试覆盖同一 asset 重检时 IoU 复用、未匹配旧 face 删除、新增 face 入 pending recognition。

### Automated Verification

- 真实小图库验收通过 CLI 触发扫描和归属，再读取 DB 与 manifest 断言人物组。
- 原型等价单元测试可使用合成 embedding 或假后端，但只能覆盖算法边界，不能替代真实模型验收。

### Done When

- 所有 acceptance criteria 通过自动化验证。
- 没有任何核心要求由硬编码、no-op、直接状态修改或 fake integration 满足。

## Feature Slice 4: 人物库 WebUI 基础展示

- [ ] Status: Not done

### Behavior

- `hikbox serve --workspace <path> --host 127.0.0.1 --port <port>` 启动本地 WebUI。
- 启动前检查扫描会话；存在 `running|aborting` 时直接失败退出，不进入 WebUI 运行态。
- 导航保留人物库、源目录与扫描、导出模板、运行日志。
- 人物首页分两个视觉区块：已命名人物、匿名人物；API 不要求按这两个概念拆接口，但页面必须这样呈现。
- 匿名区只展示已创建 person；未归属散脸不展示。
- 人物卡片显示代表 context 图、样本数、名称或匿名标识。
- 人物首页、人物详情页、导出预览页的 context 网格统一为桌面一行 6 个。
- 人物详情页展示 active assignment 样本网格；默认展示 context，点击样本展开 crop + context。
- 人物详情页做简单分页，每页 200 个 active assignment 样本。
- Live 样本在 context 上标记 `Live`。
- 首版不做搜索、筛选、虚拟滚动和移动端适配。

### Public Interface

- CLI：`hikbox serve --workspace <path> --host 127.0.0.1 --port <port>`。
- Web 页面：人物首页、人物详情页、源目录与扫描页、导出模板页、运行日志页。
- API：支撑页面读取人物列表、人物详情和样本分页的数据接口。

### Error and Boundary Cases

- workspace 未初始化时 `serve` 失败并返回可读错误。
- 扫描会话为 `running|aborting` 时 `serve` 返回非 0 且不监听端口。
- 人物不存在时详情页返回 404 或可读错误页。
- 人物没有 active assignment 时不出现在首页。

### Non-goals

- 不做待审核页。
- 不做 Identity Run 证据页。
- 不做移动端专项适配。

### Acceptance Criteria

- AC-1：对小图库扫描结果启动 WebUI 后，Playwright 截图能看到人物首页的已命名/匿名分区。
- AC-2：未命名 person 出现在匿名区；命名后移动到已命名区。
- AC-3：未归属 face 不出现在人物首页。
- AC-4：人物详情页每页最多 200 个样本；context 网格一行 6 个；点击样本后 crop/context 都可见。
- AC-5：Live 样本在 context 上显示 `Live` 标记。
- AC-6：扫描运行中执行 `hikbox serve --workspace <ws>` 返回非 0 且端口未被监听。

### Automated Verification

- Playwright 使用 Chromium 或 Chrome 打开真实 WebUI，保存截图、JSON 指标报告和服务日志到 `.tmp/<task-name>/`。
- UI 验收通过页面文本、DOM、截图和 DB 状态共同断言，不直接改 DB 制造 UI 状态。

### Done When

- 所有 acceptance criteria 通过自动化验证。
- 没有任何核心要求由硬编码、no-op、直接状态修改或 fake integration 满足。

## Feature Slice 5: 人物命名与合并

- [ ] Status: Not done

### Behavior

- 人物详情页支持重命名。
- 空名称、重复名称或不存在人物返回可读错误。
- 首页支持多选人物合并。
- 合并 winner 规则：一个已命名人物 + 若干匿名人物时，已命名人物为 winner；全匿名时，样本数多者胜，样本数相同取用户选择顺序第一个。
- 合并请求中包含两个及以上已命名人物时直接拒绝，不支持选择 winner。
- 合并后 loser 的 active assignment 全部迁移到 winner，loser 变为 inactive 或等价不可见状态。
- 支持撤销全局最近一次合并；只能撤销最近一次。
- 撤销后恢复 person 可见性、名称和 assignment。
- 合并和撤销必须事务一致。

### Public Interface

- Web 页面：人物详情页命名表单、人物首页多选合并入口、撤销最近一次合并入口。
- API：重命名、合并、撤销最近一次合并。
- DB：person 名称、person 状态、active assignment、merge operation 快照或等价撤销账本。

### Error and Boundary Cases

- 空名称、重复名称、人物不存在时返回可读错误，DB 不改变。
- 合并少于 2 个人物时返回可读错误。
- 合并两个及以上已命名人物时返回可读错误，DB 不改变。
- 无可撤销合并时，撤销入口禁用或返回可读错误。
- 重复点击合并或撤销不能产生重复 assignment 或半完成状态。

### Non-goals

- 不支持合并多个已命名人物。
- 不支持任意历史合并回滚，只支持全局最近一次合并。
- 不支持人物搜索筛选。

### Acceptance Criteria

- AC-1：根据 manifest 定位匿名 person 后，通过 WebUI 命名表单提交 `display_name`，DB 中 person 名称更新，首页从匿名区移动到已命名区。
- AC-2：合并两个匿名人物后，首页只剩 winner，winner 详情页样本数为两者之和。
- AC-3：一个已命名人物与匿名人物合并后，已命名人物保留为 winner。
- AC-4：尝试合并两个已命名人物失败并显示明确错误，DB 不改变。
- AC-5：撤销最近一次合并后，人物数量、名称、assignment 恢复到合并前。

### Automated Verification

- Playwright 通过真实页面完成命名、合并、撤销操作。
- 自动化验收根据 manifest 的 `expected_person_groups` 找到目标 person，再通过 WebUI 操作，不直接改库命名。
- DB 断言用于确认页面操作后的持久化状态。

### Done When

- 所有 acceptance criteria 通过自动化验证。
- 没有任何核心要求由硬编码、no-op、直接状态修改或 fake integration 满足。

## Feature Slice 6: 人物排除

- [ ] Status: Not done

### Behavior

- 人物详情页支持单条或批量排除 active assignment。
- 排除后，该 face 从当前人物详情页消失，person 样本数减少。
- 排除关系持久化；后续扫描/assignment 不得把该 face 自动归回同一人物。
- 排除后的 face 可在后续归属到其他人物。
- 首版不提供手动纠错，不提供“移动到指定人物”。
- 如果排除导致人物没有 active assignment，该人物不再出现在首页。
- 排除操作在导出运行中必须被禁用或返回锁定错误。

### Public Interface

- Web 页面：人物详情页单条排除、批量选择与批量排除。
- API：排除单个 assignment、批量排除 assignments。
- DB：active assignment 状态、exclusion 记录、person 样本数或等价查询结果。

### Error and Boundary Cases

- 排除不存在的 assignment 返回可读错误。
- 排除不属于当前人物的 assignment 返回可读错误。
- 批量排除部分失败时必须事务回滚或整体返回错误，不出现部分排除。
- 导出运行中排除必须被拒绝。
- 重复排除同一 face 不得产生重复 exclusion。

### Non-goals

- 不做手动指定归属。
- 不做拆分人物。
- 不做独立待审核页。

### Acceptance Criteria

- AC-1：通过 WebUI 排除一个样本后，DB 中 active assignment 失效并记录 exclusion，详情页不再显示该样本。
- AC-2：再次执行 `hikbox scan start --workspace <ws>` 后，被排除 face 不会回到原人物。
- AC-3：批量排除遇到非法 assignment 时整体失败，合法样本也不被部分排除。
- AC-4：导出运行中点击排除，页面显示“导出进行中，暂不可修改”或 API 返回锁定错误。
- AC-5：页面中不存在“移动到另一个人物”或“拆分人物”的入口。

### Automated Verification

- 排除验收先通过 Slice 5 的 WebUI 命名流程定位目标人物，再在人物详情页操作排除。
- 通过 WebUI/API 公共入口触发排除，读取 DB 和页面状态断言结果。

### Done When

- 所有 acceptance criteria 通过自动化验证。
- 没有任何核心要求由硬编码、no-op、直接状态修改或 fake integration 满足。

## Feature Slice 7: 导出模板与执行

- [ ] Status: Not done

### Behavior

- WebUI 提供导出模板创建、编辑、预览、执行、历史页面。
- 创建和编辑模板的人物选择器只展示已命名人物。
- 模板选择多个已命名人物；照片必须同时包含所有模板人物才进入导出候选。
- 对候选照片计算模板人物 face bbox 绝对像素面积的最小值 `selected_min_area`。
- 本图任意 face 的 bbox 面积 `>= selected_min_area / 4` 视为显著人脸。
- 如果显著人脸全部属于模板选择人物，静态图导出到 `only/YYYY-MM/`。
- 如果存在非模板人物显著人脸，静态图导出到 `group/YYYY-MM/`。
- 同一人物在同一照片中出现多张脸，只按“该人物已命中”判断；bbox 面积仍参与 `selected_min_area`。
- 导出预览页展示候选照片、only/group 分桶、月份目录和样例 context；context 网格一行 6 个。
- Live Photo 导出始终开启：仅当静态图为 `heic/heif` 且 metadata 已配对同目录隐藏 MOV 时复制 MOV；MOV 跟随静态图同目录。
- 目标文件已存在时跳过，不覆盖、不改名，并写入导出账本。
- 导出运行中禁止命名、合并、撤销合并、排除等人物归属写操作。

### Public Interface

- Web 页面：导出模板列表、创建/编辑页、预览页、执行确认、历史详情。
- API：模板创建、模板更新、预览、执行、历史查询。
- DB：export template、template person、export run、export delivery 或等价账本。
- 文件：用户选择的 `output_root/only/YYYY-MM/` 与 `output_root/group/YYYY-MM/`。

### Error and Boundary Cases

- 模板未选择人物时返回可读错误。
- 模板 output_root 不存在且无法创建时返回可读错误。
- 模板人物被改为匿名、inactive 或不存在时，预览/执行返回可读错误。
- 目标文件已存在时记录 `skipped_exists`，不覆盖、不改名。
- Live MOV 缺失或不可读时静默跳过 MOV，不影响静态图导出，并在账本中记录 MOV 跳过原因。
- 导出运行中人物写操作必须被拒绝。

### Non-goals

- 不提供导出模板删除能力。
- 不提供复杂导出表达式。
- 不提供 Live Photo 开关。

### Acceptance Criteria

- AC-1：通过 Slice 5 的 WebUI 命名流程命名 manifest 中的目标人物后，模板人物选择器只展示这些已命名人物。
- AC-2：按 manifest 创建模板后，预览结果与 `expected_exports` 一致，包括候选照片、only/group、月份目录和样例 context。
- AC-3：执行导出后，真实文件树包含预期静态图和 HEIC/HEIF 配对 MOV；JPG/PNG 不导出 MOV。
- AC-4：同名目标已存在时不会覆盖，账本记录 `skipped_exists`。
- AC-5：导出历史页展示本次 run 的 started/completed 状态、导出数量、跳过数量和账本明细。
- AC-6：导出运行中，命名、合并、撤销合并、排除都被拒绝。

### Automated Verification

- Playwright 通过真实 WebUI 完成命名、模板创建、预览和执行。
- 自动化验收根据 manifest 的 `expected_exports` 对比页面预览、DB 账本和真实导出文件树。
- 通过预先创建同名目标文件验证 `skipped_exists`，并断言文件内容未被覆盖。

### Done When

- 所有 acceptance criteria 通过自动化验证。
- 没有任何核心要求由硬编码、no-op、直接状态修改或 fake integration 满足。

## Cross-Slice Verification Strategy

- 最小端到端验收：`hikbox init --workspace <ws> --external-root <ext>`，`hikbox source add --workspace <ws> <gallery> --label fixture`，`hikbox scan start --workspace <ws>`，`hikbox serve --workspace <ws>`，Playwright 完成人物命名、合并或排除、导出模板预览与执行。
- 每个 slice 的自动化验收都必须从公共入口触发行为，再观察 DB、文件、页面或日志。
- 真实模型验收用小图库作为主信号；合成 embedding、假后端和失败注入只用于补充边界测试。
- Playwright 产物必须放在 `.tmp/<task-name>/`，至少包含页面截图、JSON 指标报告、本地服务日志。
- 数据库 schema 变更必须与 `docs/db_schema.md` 同步。

