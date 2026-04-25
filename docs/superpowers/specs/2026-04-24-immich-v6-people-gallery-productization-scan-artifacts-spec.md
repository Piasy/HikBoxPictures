# Immich v6 人物图库产品化 Slice B：可恢复扫描与人脸产物 Spec

## Goal

在已初始化并登记源目录的工作区中，通过 `hikbox scan start --workspace <path>` 扫描真实照片，生成可恢复的 asset、metadata、face observation、embedding、crop/context 产物和扫描日志，为后续 v6 在线人物归属提供可信输入。

## Global Constraints

- 本 spec 是父 spec `docs/superpowers/specs/2026-04-24-immich-v6-people-gallery-productization-spec.md` 的 Slice B，只负责扫描、人脸检测、embedding 与产物入库。
- 本 slice 依赖 Slice A 已定义的 workspace、external_root、source、`config.json`、`library.db`、`embedding.db` 和日志目录语义，不重新定义初始化或 source 管理行为。
- 产品语义以 `hikbox_pictures/immich_face_single_file.py` 和 `docs/group_pics_algo.md` 的 v6 章节为准；本 slice 只产出 v6 归属所需输入，不执行人物归属。
- 核心验收必须使用真实小图库、真实 InsightFace 模型、真实 SQLite、真实 crop/context 文件和真实日志；mock/stub/no-op 路径不得满足验收。
- CI 验收必须有明确前置资源：测试运行前 `HIKBOX_INSIGHTFACE_MODEL_ROOT` 指向包含 `models/buffalo_l/det_10g.onnx` 和 `models/buffalo_l/w600k_r50.onnx` 的模型缓存目录；模型可由 CI 缓存恢复或由仓库脚本下载到 `.tmp/ci-models/`，测试不得在模型缺失时静默跳过真实模型验收。
- CI 小图库必须是固定、可复现的测试资源：优先放在仓库 `tests/fixtures/people_gallery_scan/`；如果因体积或授权不能入库，必须由仓库脚本下载到 `.tmp/fixtures/people_gallery_scan/`，并用 checksum 校验。小图库至少包含可检测人脸照片、HEIC/HEIF 与隐藏 MOV 配对、JPG/PNG 的 MOV 反例、非支持后缀和损坏图片；文件排序必须稳定，且按测试排序后的前两个支持照片都必须是可解码、可进入真实检测路径的图片，其中至少第 1 批能产生真实 face artifact。
- CI 环境必须安装 HEIC/HEIF 解码依赖；如果运行环境不支持 HEIC/HEIF，相关集成测试必须失败并提示缺少依赖，而不是降级为只测 JPG/PNG。
- 失败注入可以用于验证批次恢复，但必须通过公共 CLI 入口触发扫描流程，不能直接改库制造完成状态。
- 失败注入只能使用测试专用环境变量开启，不属于用户公开 CLI：`HIKBOX_TEST_SCAN_BATCH_SIZE=<n>` 只允许在测试中缩小批次大小以低成本制造多批次；`HIKBOX_TEST_SCAN_FAILPOINT=<kind>:<batch_index>` 只允许触发指定批次的子进程崩溃或主进程提交前失败。实现不得让这些变量绕过 discover、metadata、真实模型检测、embedding、artifact 生成或事务提交路径。
- 任何数据库 schema 修改都必须同步更新 `docs/db_schema.md`。

## Feature Slice 1: 批次可恢复检测入库

- [ ] Implementation status: Not done

### Behavior

- `hikbox scan start --workspace <path>` 读取已初始化工作区的 `config.json`、`library.db`、`embedding.db` 和已登记 active source，不隐式初始化工作区或 source。
- 扫描支持照片后缀 `jpg`、`jpeg`、`png`、`heic`、`heif`，后缀大小写不敏感；非支持后缀文件不入库为 asset。
- discover/metadata 阶段为每个支持照片创建或复用 asset 记录，保存 source、绝对路径、文件名、拍摄月份或可回退月份、文件指纹或等价幂等键、Live MOV 配对信息和处理状态。
- 只有 `heic`/`heif` 照片尝试 Live Photo 配对；匹配同目录隐藏 MOV，例如 `IMG_8175.HEIC` 对应 `.IMG_8175_1771856408349261.MOV`，`heic`、`heif`、`mov` 后缀大小写均不敏感。
- `jpg`、`jpeg`、`png` 不尝试 Live MOV 配对；即使同目录存在相似 MOV，也不得写入 Live 配对关系。
- 默认每批最多 200 张照片；批次边界和批次状态持久化到 `library.db`，用于恢复扫描。
- 自动化测试可以通过 `HIKBOX_TEST_SCAN_BATCH_SIZE=1` 或其它小于默认值的正整数制造多批次；该变量只改变批次大小，不改变扫描输入、模型执行、产物生成或 DB 提交语义。
- 每批启动一个子进程串行处理；子进程加载真实 InsightFace 模型，处理本批照片，返回检测、embedding 和产物结果后退出，以释放模型和内存。
- 子进程对每张照片执行 EXIF 方向纠正、真实 InsightFace 检测、512 维 embedding 提取，并为每个检测到的人脸生成 face crop 与 context 图。
- context 图是整图按比例缩放到最长边不超过 480 后画出本图人脸框，不是原图局部裁剪；crop 图是对应单张人脸裁剪或对齐裁剪。
- 主进程只有在整批成功后，才统一提交 asset、face observation、embedding、crop/context 路径和批次 completed 状态。
- 单张照片读取失败或无法解码时记录 asset 级失败并进入扫描摘要，不阻断同一批次内其它照片提交；该失败照片不得生成 face、embedding、crop 或 context。
- 批处理中断、子进程崩溃或主进程提交前失败时，该批不得标记 completed；再次执行同一命令时必须跳过 completed 批次，整批重跑未完成批次。
- 已完成批次再次扫描不得重复写入 asset、face observation、embedding、crop/context 文件或重复记录 Live MOV 配对。
- 扫描成功和失败都必须在 `external_root/logs` 下写入可追踪日志；扫描摘要至少包含批次数、完成批次数、失败 asset 数、成功 face 数和产物数量。

### Public Interface

- CLI：`hikbox scan start --workspace <path>`。
- DB：`library.db` 中的 asset、asset metadata、Live MOV 元数据、face observation、scan session、scan batch、scan batch item、crop/context 路径和扫描摘要。
- DB：`embedding.db` 中每个成功 face 的 512 维 embedding，至少包含 main embedding；如果实现生成 flip embedding，则必须按 schema 明确 variant 并保持幂等。
- 文件：`external_root/artifacts/crops` 下的真实 face crop 图片。
- 文件：`external_root/artifacts/context` 下的真实 480p 整图加框 context 图片。
- 日志：`external_root/logs` 下的扫描启动、批次开始、批次完成、asset 失败、批次失败、扫描完成或扫描失败记录。

### Prototype Source

- 向量归一化来源：`hikbox_pictures/immich_face_single_file.py` 中的 embedding normalize 逻辑。
- EXIF 方向纠正与 RGB 读取来源：`hikbox_pictures/immich_face_single_file.py` 中的图片读取逻辑。
- bbox IoU 与归一化 bbox 来源：`hikbox_pictures/immich_face_single_file.py` 中的 bbox 工具逻辑。
- `DetectedFace` 数据结构来源：`hikbox_pictures/immich_face_single_file.py` 中的人脸检测结果结构。
- InsightFace `buffalo_l` 后端、`det_10g.onnx`、`w600k_r50.onnx`、`input_size=(640, 640)`、BGR 转换、`norm_crop`、embedding 提取来源：`hikbox_pictures/immich_face_single_file.py` 中的 InsightFace 检测实现。
- 实现可以复制这些代码，也可以逻辑等价重写；验收必须通过真实模型、真实产物和真实 DB 证明行为一致。

### Error and Boundary Cases

- `--workspace` 缺失、workspace 未初始化、缺少 `config.json`、缺少 `library.db` 或缺少 `embedding.db` 时返回非 0 退出码和可读错误，不创建新工作区。
- 没有 active source 时 `scan start` 返回非 0 退出码和可读错误，不创建 completed 批次。
- source 路径不存在、不可读或不再是目录时，该 source 的扫描失败必须可观察；不得静默跳过并报告全局成功。
- 缺少 InsightFace 模型文件、模型加载失败或模型输出 embedding 维度不是 512 时返回非 0 退出码和可读错误，不能创建 completed 批次。
- 单张照片读取失败、格式损坏或无法解码时记录 asset 级失败和摘要，不阻断其它可读照片。
- 子进程崩溃、超时或返回无效结果时，本批保持未完成，下次 `scan start` 整批重跑。
- 主进程提交前失败时，本批不得出现 completed 状态；不得留下会让下次扫描误判为完成的半成品 DB 状态。
- 测试 failpoint 只能触发两类失败：`child_crash:<batch_index>` 在指定批次子进程已经加载真实模型并对目标批次完成至少一次真实检测尝试后、返回结果前以崩溃方式失败；`parent_before_commit:<batch_index>` 在主进程收到真实结果后、提交该批 DB 事务前失败。failpoint 不得直接写 scan batch 状态、asset、face observation 或 embedding。
- 已完成批次重扫必须幂等；不得重复插入 face observation、embedding、Live MOV 配对或重复覆盖已存在 crop/context。

### Non-goals

- 不做人脸归属、人物创建、active assignment、命名、合并、撤销、排除或导出。
- 不做离线全量聚类、AHC、HDBSCAN、person consensus 或 v5 归属链路。
- 不提供公开 force 重检 CLI；同一 asset 重检的 IoU 复用语义由 Slice C 归属 spec 约束。
- 不做多子进程并行处理；首版每批一个子进程串行处理。
- 不识别视频内容；MOV 只作为 HEIC/HEIF 的 Live Photo 配对文件。
- 不做跨 source 重复照片去重；相同文件出现在不同 source 时仍按独立 asset 处理。

### Acceptance Criteria

- AC-1：在已初始化并登记真实 source 的工作区执行 `hikbox scan start --workspace <ws>` 后，`library.db` 中存在支持后缀照片对应的 asset 记录、scan session、scan batch、scan batch item、face observation、crop/context 路径和扫描摘要。
- AC-2：`embedding.db` 中每个成功检测到的人脸至少存在一条 512 维 main embedding；向量已归一化，且与 `library.db` 中的 face observation 可通过稳定键关联。
- AC-3：每个成功检测到 face 的 crop/context 文件真实存在、可被图片库打开；context 最长边不超过 480，且是整图加人脸框，不是局部裁剪。
- AC-4：HEIC/HEIF 大小写变体能匹配同目录隐藏 MOV 并写入 Live 配对元数据；JPG/JPEG/PNG 即使存在相似 MOV 也不会写入 Live 配对关系。
- AC-5：source 中的非支持后缀文件不会创建 asset；损坏照片记录 asset 级失败并进入扫描摘要，不阻断其它照片完成。
- AC-6：使用测试小图库和 `HIKBOX_TEST_SCAN_BATCH_SIZE=1` 制造至少两个批次；失败注入让第 2 批在子进程崩溃或主进程提交前失败后，再次执行 `scan start` 时，第 1 批不重复检测、不重复写入，第 2 批整批重跑并完成。
- AC-7：重复执行已完成扫描的 `hikbox scan start --workspace <ws>` 后，asset、face observation、embedding、crop/context 和 Live MOV 配对数量保持不变，日志记录跳过 completed 批次或无新增待处理批次。
- AC-8：无 source、缺少模型、workspace 未初始化、source 不可读等失败场景返回非 0 退出码和可读 stderr，且不会创建 completed 批次。
- AC-9：成功和失败扫描都会在 `external_root/logs` 中留下可追踪日志；成功摘要包含批次数、完成批次数、失败 asset 数、成功 face 数和产物数量。

### Automated Verification

- CLI 集成测试在真实临时目录中执行 Slice A 的 `init -> source add`，再通过 subprocess 执行 `hikbox scan start --workspace <ws>`，并读取真实 SQLite、产物目录和日志断言 AC-1、AC-2、AC-3、AC-5、AC-7、AC-9。
- CI 真实模型测试必须显式设置 `HIKBOX_INSIGHTFACE_MODEL_ROOT`。该目录缺少 `models/buffalo_l/det_10g.onnx` 或 `models/buffalo_l/w600k_r50.onnx` 时，测试应失败并输出模型准备说明；不得把真实模型测试标记为 skip、xfail 或自动切换 fake detector。
- 真实小图库验收必须使用固定 fixture 目录，优先为 `tests/fixtures/people_gallery_scan/`，或由仓库脚本按 checksum 下载到 `.tmp/fixtures/people_gallery_scan/`。fixture 必须包含至少一张可检测人脸照片、至少一张无支持后缀文件、至少一张损坏或不可解码图片、至少一个 HEIC/HEIF 与隐藏 MOV 配对样本，以及 JPG/JPEG/PNG 旁边存在相似 MOV 的反例样本；测试必须固定 discover 排序，并确保 `HIKBOX_TEST_SCAN_BATCH_SIZE=1` 时前两个支持照片不是损坏图或无效文件。
- HEIC/HEIF 解码能力必须纳入 CI 前置检查；缺少依赖时相关测试失败并提示依赖名称，不允许把 HEIC/HEIF 验收降级为仅断言文件名匹配。
- AC-2 的 embedding 验证读取 `embedding.db` 中真实 vector blob，断言维度为 512、范数接近 1，并通过 face observation 关联键确认没有孤儿 embedding。
- AC-3 的 artifact 验证读取真实图片尺寸和像素内容；context 必须断言最长边不超过 480，并通过图像差异或框线像素检查证明有人脸框，不允许只断言文件存在。
- AC-4 通过真实文件命名样本覆盖 HEIC/HEIF 大小写、MOV 大小写、隐藏 MOV 匹配和 JPG/PNG 不匹配 MOV。
- AC-6 通过 CLI 公共入口、`HIKBOX_TEST_SCAN_BATCH_SIZE=1` 和 `HIKBOX_TEST_SCAN_FAILPOINT=child_crash:2` 或 `HIKBOX_TEST_SCAN_FAILPOINT=parent_before_commit:2` 触发失败；测试断言第 1 批 completed 状态、产物数量和检测次数不变，第 2 批重新运行后 completed。
- 失败注入测试必须证明 failpoint 仍经过真实扫描路径：第 1 批必须已有真实 crop/context 和 embedding；`child_crash:2` 必须能在日志或检测计数中证明第 2 批子进程已加载真实模型并完成至少一次真实检测尝试；`parent_before_commit:2` 必须能在日志中观察到子进程返回真实检测结果但该批 DB 未提交；任何直接改写 batch 状态的实现都不能通过验收。
- AC-8 通过未初始化 workspace、空 source 列表、设置 `HIKBOX_INSIGHTFACE_MODEL_ROOT` 指向缺失模型目录、模型不可加载、source 不可读等 CLI 失败用例覆盖，并断言没有 completed 批次。
- 测试不得直接插入 scan batch、asset、face observation 或 embedding 记录来满足验收；不得用硬编码模型输出、fake detector 或 no-op artifact 文件替代真实模型验收。

### Done When

- 所有验收标准都通过自动化验证。
- `docs/db_schema.md` 已同步描述本 slice 引入或确认的 asset、metadata、Live MOV、face observation、scan session、scan batch、scan batch item、embedding 和 artifact 路径 schema。
- 没有核心需求通过直接状态修改、硬编码数据、占位行为、stub detector、fake embedding 或 no-op artifact 满足。
