# Immich v6 人物图库产品化 Slice B：可恢复扫描与人脸产物 Spec

## Goal

在已初始化并登记源目录的工作区中，通过 `hikbox scan start --workspace <path>` 扫描真实照片，生成可恢复的 asset、metadata、face observation、main embedding、crop/context 产物和扫描日志，为后续 v6 在线人物归属提供可信输入。

## Global Constraints

- 本 spec 是父 spec `docs/superpowers/specs/2026-04-24-immich-v6-people-gallery-productization-spec.md` 的 Slice B，只负责扫描、人脸检测、main embedding 与产物入库。
- 本 slice 依赖 Slice A 已定义的 workspace、external_root、source、`config.json`、`library.db`、`embedding.db` 和日志目录语义，不重新定义初始化或 source 管理行为。
- 本 slice 引用父 spec 的真实验收小图库基线；测试图库生成会作为独立子 spec 管理，本 slice 不负责生成图库。
- 产品语义以 `hikbox_pictures/immich_face_single_file.py` 和 `docs/group_pics_algo.md` 的 v6 章节为准；本 slice 只产出 v6 归属所需输入，不执行人物归属。
- 核心验收必须使用真实小图库、真实 InsightFace 模型、真实 SQLite、真实 crop/context 文件和真实日志；mock/stub/no-op 路径不得满足验收。
- InsightFace 框架支持指定模型根目录：`FaceAnalysis(name='buffalo_l', root=<model_root>)` 会把模型目录解析为 `<model_root>/models/buffalo_l`；`model_zoo.get_model(..., root=<model_root>)` 也支持同一 root 约定。本 slice 必须把 `<model_root>` 固定为 `workspace/.hikbox/models/insightface`，权重文件位于 `workspace/.hikbox/models/insightface/models/buffalo_l/det_10g.onnx` 和 `workspace/.hikbox/models/insightface/models/buffalo_l/w600k_r50.onnx`。
- `hikbox scan start` 必须显式把 workspace 模型根目录传给 InsightFace 框架；不得使用框架默认 root `~/.insightface` 或当前工作目录隐式加载权重。模型下载、缓存填充和缓存缺失处理假定由 InsightFace 框架自身语义负责，不作为本 slice 的产品需求或失败验收范围。
- Slice B 验收只关心扫描命令是否真实调用 InsightFace 框架并传入 workspace 模型根目录；不得 skip、xfail 或自动切换 fake detector。
- CI 环境必须安装 HEIC/HEIF 解码依赖；如果运行环境不支持 HEIC/HEIF，相关集成测试必须失败并提示缺少依赖，而不是降级为只测 JPG/PNG。
- 失败恢复验收不得依赖环境变量、测试后门或直接改库；必须通过正式 CLI 启动真实扫描进程，并由测试用例向 CLI 进程发送标准信号或直接 kill 进程来制造异常退出。
- 任何数据库 schema 修改都必须同步更新 `docs/db_schema.md`。

## Feature Slice 1: 批次可恢复检测入库

- [ ] Implementation status: Not done

### Behavior

- `hikbox scan start --workspace <path> [--batch-size <n>]` 读取已初始化工作区的 `config.json`、`library.db`、`embedding.db`、workspace 本地模型缓存和已登记 active source，不隐式初始化工作区或 source。
- `--batch-size` 是正式 CLI 可选参数，默认值为 200；自动化测试可以传 `--batch-size 10`。`n` 必须是正整数；缺失、非整数或小于 1 时返回非 0 退出码和可读参数错误。
- 扫描支持照片后缀 `jpg`、`jpeg`、`png`、`heic`、`heif`，后缀大小写不敏感；非支持后缀文件不入库为 asset。
- discover 阶段必须按稳定顺序遍历支持照片，排序规则需能在不同平台和重复运行中保持一致；同一 source 中相同文件集合应产生相同批次边界。
- metadata 阶段为每个支持照片创建或复用 asset 记录，保存 source、绝对路径、文件名、拍摄月份或可回退月份、文件指纹或等价幂等键、Live MOV 配对信息和处理状态。
- 只有 `heic`/`heif` 照片尝试 Live Photo 配对；匹配同目录隐藏 MOV，例如 `IMG_8175.HEIC` 对应 `.IMG_8175_1771856408349261.MOV`，`heic`、`heif`、`mov` 后缀大小写均不敏感。
- `jpg`、`jpeg`、`png` 不尝试 Live MOV 配对；即使同目录存在相似 MOV，也不得写入 Live 配对关系。
- 批次边界和批次状态持久化到 `library.db`，用于恢复扫描；批次大小来自本次命令的 `--batch-size`，未传时为 200。
- 每批启动一个子进程串行处理；子进程从 `workspace/.hikbox/models/insightface` 加载真实 InsightFace 模型，处理本批照片，返回检测、main embedding 和产物结果后退出，以释放模型和内存。
- 子进程对每张照片执行 EXIF 方向纠正、真实 InsightFace 检测、512 维 main embedding 提取，并为每个检测到的人脸生成 face crop 与 context 图。
- context 图是整图按比例缩放到最长边不超过 480 后画出本图人脸框，不是原图局部裁剪；crop 图是对应单张人脸裁剪或对齐裁剪。
- 主进程只有在整批成功后，才统一提交 asset、face observation、main embedding、crop/context 路径和批次 completed 状态。
- 单张照片读取失败或无法解码时记录 asset 级失败并进入扫描摘要，不阻断同一批次内其它照片提交；该失败照片不得生成 face、embedding、crop 或 context。
- 自动化测试会通过向 CLI 进程发送 `SIGTERM`、`SIGINT` 或 `SIGKILL` 来模拟进程异常退出；产品代码不需要为这些信号实现专门处理。只要进程退出发生在某批次完成提交前，该批次不得标记 completed；已完成并提交的批次保持 completed；再次执行同一命令时必须跳过 completed 批次，整批重跑未完成批次。
- 已完成批次再次扫描不得重复写入 asset、face observation、main embedding、crop/context 文件或重复记录 Live MOV 配对。
- 扫描成功、失败和被测试杀进程后的恢复状态都必须可追踪；对杀进程场景，只要求中断前已有 `external_root/logs` 扫描/批次日志与 DB 状态足以判断已完成批次和未完成批次，不要求记录信号本身。扫描摘要至少包含批次数、完成批次数、失败 asset 数、成功 face 数和产物数量。

### Public Interface

- CLI：`hikbox scan start --workspace <path> [--batch-size <n>]`。
- 文件：workspace 本地模型缓存 `workspace/.hikbox/models/insightface/models/buffalo_l/det_10g.onnx` 和 `workspace/.hikbox/models/insightface/models/buffalo_l/w600k_r50.onnx`。
- DB：`library.db` 中的 asset、asset metadata、Live MOV 元数据、face observation、scan session、scan batch、scan batch item、crop/context 路径和扫描摘要。
- DB：`embedding.db` 中每个成功 face 的 512 维 main embedding；本 slice 不写入 flip embedding。
- 文件：`external_root/artifacts/crops` 下的真实 face crop 图片。
- 文件：`external_root/artifacts/context` 下的真实 480p 整图加框 context 图片。
- 日志：`external_root/logs` 下的扫描启动、批次开始、批次完成、asset 失败、扫描完成或扫描失败记录；信号杀进程验收只要求中断前已有扫描/批次日志和 DB 状态足以追踪恢复，不要求产品代码捕获或记录信号本身。

### Prototype Source

- 向量归一化参考：`hikbox_pictures/immich_face_single_file.py:21` 的 `_normalize_vector`。
- EXIF 方向纠正与 RGB 读取参考：`hikbox_pictures/immich_face_single_file.py:29` 的 `load_rgb_image_with_exif`。
- bbox IoU 与归一化 bbox 参考：`hikbox_pictures/immich_face_single_file.py:35` 的 `BoundingBox`。
- `DetectedFace` 数据结构参考：`hikbox_pictures/immich_face_single_file.py:70`。
- InsightFace `buffalo_l` 模型根目录、`det_10g.onnx`、`w600k_r50.onnx`、`input_size=(640, 640)`、CPU provider 参考：`hikbox_pictures/immich_face_single_file.py:386` 的 `InsightFaceImmichBackend.__init__`。
- BGR 转换、RetinaFace 检测、`norm_crop`、ArcFace main embedding 提取和归一化参考：`hikbox_pictures/immich_face_single_file.py:411` 的 `InsightFaceImmichBackend.detect_faces`。
- 实现必须把上述原型语义迁移到产品代码中；不得在产品实现或验收测试中直接 import 或调用 `hikbox_pictures.immich_face_single_file` 的函数、类或全局对象来满足本 slice。

### Error and Boundary Cases

- `--workspace` 缺失、workspace 未初始化、缺少 `config.json`、缺少 `library.db` 或缺少 `embedding.db` 时返回非 0 退出码和可读错误，不创建新工作区。
- `--batch-size` 缺失值、非整数、0 或负数时返回非 0 退出码和可读参数错误，不开始扫描。
- 没有 active source 时 `scan start` 返回非 0 退出码和可读错误，不创建 completed 批次。
- source 路径不存在、不可读或不再是目录时，该 source 的扫描失败必须可观察；不得静默跳过并报告全局成功。
- 模型输出 embedding 维度不是 512 时返回非 0 退出码和可读错误，不能创建 completed 批次。
- 单张照片读取失败、格式损坏或无法解码时记录 asset 级失败和摘要，不阻断其它可读照片。
- 测试用例通过 `SIGTERM`、`SIGINT` 或 `SIGKILL` 杀掉 CLI 进程后，本批保持未完成；产品代码不需要捕获或特殊处理这些信号，但不得留下会让下次扫描误判为完成的半成品 DB 状态。
- 已完成批次重扫必须幂等；不得重复插入 face observation、main embedding、Live MOV 配对或重复覆盖已存在 crop/context。

### Non-goals

- 不生成测试图库；真实验收小图库由独立测试图库子 spec 定义和准备。
- 不做人脸归属、人物创建、active assignment、命名、合并、撤销、排除或导出。
- 不做离线全量聚类、AHC、HDBSCAN、person consensus 或 v5 归属链路。
- 不提供公开 force 重检 CLI；同一 asset 重检的 IoU 复用语义由 Slice C 归属 spec 约束。
- 不做多子进程并行处理；首版每批一个子进程串行处理。
- 不识别视频内容；MOV 只作为 HEIC/HEIF 的 Live Photo 配对文件。
- 不做跨 source 重复照片去重；相同文件出现在不同 source 时仍按独立 asset 处理。
- 不写入、不缓存、不验收 flip embedding；本 slice 只处理 main embedding。

### Acceptance Criteria

- AC-1：使用父 spec 定义的同一份测试图库基线执行 `hikbox scan start --workspace <ws> --batch-size 10` 后，`library.db` 中存在支持后缀照片对应的 asset 记录、scan session、scan batch、scan batch item、face observation、crop/context 路径和扫描摘要；该基线本身至少包含 50 张照片，不允许为本 slice 另建一套缩水图库。
- AC-2：扫描必须真实调用 InsightFace 框架，并显式指定 `workspace/.hikbox/models/insightface` 作为 InsightFace 模型根目录；验收通过调用观测、日志或可测 spy 证明产品代码没有使用框架默认 root `~/.insightface`，且没有切换到 fake detector。
- AC-3：`embedding.db` 中每个成功检测到的人脸恰好存在 main embedding；向量为 512 维并已归一化，且与 `library.db` 中的 face observation 可通过稳定键关联；不得写入 flip embedding。
- AC-4：每个成功检测到 face 的 crop/context 文件真实存在、可被图片库打开；context 最长边不超过 480，且是整图加人脸框，不是局部裁剪。
- AC-5：测试图库中的 HEIC/HEIF 大小写变体能匹配同目录隐藏 MOV 并写入 Live 配对元数据；JPG/JPEG/PNG 即使存在相似 MOV 也不会写入 Live 配对关系。
- AC-6：测试图库中的非支持后缀文件不会创建 asset；损坏照片记录 asset 级失败并进入扫描摘要，不阻断其它照片完成。
- AC-7：以 `--batch-size 10` 扫描 50 张测试照片时至少产生 5 个批次；测试用例分别覆盖 `SIGTERM`、`SIGINT` 和 `SIGKILL`，在第 2 批真实处理期间向 CLI 进程发送对应信号后，再次执行同一命令时，第 1 批不重复检测、不重复写入，第 2 批整批重跑并完成。
- AC-8：重复执行已完成扫描的 `hikbox scan start --workspace <ws> --batch-size 10` 后，asset、face observation、main embedding、crop/context 和 Live MOV 配对数量保持不变，日志记录跳过 completed 批次或无新增待处理批次。
- AC-9：无 source、workspace 未初始化、source 不可读、`--batch-size` 非法等失败场景返回非 0 退出码和可读 stderr，且不会创建 completed 批次。
- AC-10：成功和失败扫描都会在 `external_root/logs` 中留下可追踪日志；测试杀进程场景必须能通过中断前已有扫描/批次日志与 DB 状态判断已完成批次和未完成批次；成功摘要包含批次数、完成批次数、失败 asset 数、成功 face 数和产物数量。

### Automated Verification

- CLI 集成测试在真实临时目录中执行 Slice A 的 `init -> source add`，准备 workspace 本地模型缓存，再通过 subprocess 执行 `hikbox scan start --workspace <ws> --batch-size 10`，并读取真实 SQLite、产物目录和日志断言 AC-1、AC-3、AC-4、AC-6、AC-8、AC-10。
- 真实小图库验收必须使用父 spec 定义的同一份测试图库基线，固定 fixture 目录优先为 `tests/fixtures/people_gallery_scan/`，或由测试图库子 spec 定义的仓库脚本按 checksum 下载到 `.tmp/fixtures/people_gallery_scan/`。文件命名和 discover 排序必须稳定；不得为 Slice B 另建少量图片的专用缩水 fixture。
- AC-2 通过对 InsightFace 框架入口做可观测验证覆盖：测试必须证明产品扫描路径调用真实 InsightFace 框架，并向 `FaceAnalysis(..., root=<model_root>)` 或 `model_zoo.get_model(..., root=<model_root>)` 传入 `workspace/.hikbox/models/insightface`；该验证不得替换真实检测结果，也不得让 fake detector 满足 AC-1、AC-3 或 AC-4。
- HEIC/HEIF 解码能力必须纳入 CI 前置检查；缺少依赖时相关测试失败并提示依赖名称，不允许把 HEIC/HEIF 验收降级为仅断言文件名匹配。
- AC-3 的 embedding 验证读取 `embedding.db` 中真实 vector blob，断言每个 face 只有 main variant、维度为 512、范数接近 1，并通过 face observation 关联键确认没有孤儿 embedding。
- AC-4 的 artifact 验证读取真实图片尺寸和像素内容；context 必须断言最长边不超过 480，并通过图像差异或框线像素检查证明有人脸框，不允许只断言文件存在。
- AC-5 通过真实文件命名样本覆盖 HEIC/HEIF 大小写、MOV 大小写、隐藏 MOV 匹配和 JPG/PNG 不匹配 MOV。
- AC-7 通过正式 CLI 公共入口和 `--batch-size 10` 触发；测试分别对 `SIGTERM`、`SIGINT` 和 `SIGKILL` 启动独立临时工作区，等待日志或 DB 显示第 2 批进入 running 后发送对应信号，再重新执行同一命令并断言第 1 批 completed 状态、产物数量和检测次数不变，第 2 批重新运行后 completed。
- AC-9 通过未初始化 workspace、空 source 列表、source 不可读和非法 `--batch-size` 等 CLI 失败用例覆盖，并断言没有 completed 批次。
- 测试不得直接插入 scan batch、asset、face observation 或 embedding 记录来满足验收；不得用硬编码模型输出、fake detector、环境变量 failpoint 或 no-op artifact 文件替代真实模型验收。

### Done When

- 所有验收标准都通过自动化验证。
- `docs/db_schema.md` 已同步描述本 slice 引入或确认的 asset、metadata、Live MOV、face observation、scan session、scan batch、scan batch item、main embedding 和 artifact 路径 schema。
- 没有核心需求通过直接状态修改、硬编码数据、占位行为、stub detector、fake embedding、环境变量 failpoint 或 no-op artifact 满足。
