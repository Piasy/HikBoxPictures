# Immich v6 人物图库产品化 Slice C：v6 在线人物归属 Spec

## Goal

在完成扫描、人脸检测和 main embedding 入库后，通过同一个 `hikbox scan start --workspace <path>` 按 Immich v6 在线语义创建匿名人物、写入 active assignment，并保证重复扫描、低证据样本和损坏 embedding 都有可观察且可恢复的结果。

## Global Constraints

- 本 spec 是父 spec `docs/superpowers/specs/2026-04-24-immich-v6-people-gallery-productization-spec.md` 的 Slice C，只负责 v6 在线人物归属、匿名 person 创建、active assignment、assignment run 与归属日志。
- 本 slice 依赖 Slice 0 的固定真实小图库、Slice A 的 workspace/source 契约，以及 Slice B 已写入的 active asset、active face observation、main embedding、crop/context 和扫描批次状态；不重新定义初始化、source 管理、人脸检测或 artifact 生成行为。
- 公共入口仍是 `hikbox scan start --workspace <path> [--batch-size <n>]`；首版不新增独立 `assign`、`recognize`、`cluster`、`force-redetect` 或 `rebuild` CLI。
- assignment 输入只能来自 `library.db` 中 active face observation 和 `embedding.db` 中对应的 main embedding；不得重新读取照片、不得重新调用 InsightFace 检测或识别模型、不得依赖 manifest 作为产品逻辑输入。
- 产品语义以 `hikbox_pictures/immich_face_single_file.py` 和 `docs/group_pics_algo.md` 的 v6 章节为准：全量历史 active face 进入 HNSW cosine 索引，新 face 已在索引内后再做近邻检索，按两轮在线归属处理 pending face。
- 默认归属参数固定为 `max_distance=0.5`、`min_faces=3`、`num_results=max(min_faces, 1)`、`embedding_variant='main'`、`distance_metric='cosine_distance'`、`self_match_included=true`、`two_pass_deferred=true`。
- assignment run 必须持久化算法版本和参数快照；算法版本使用 `immich_v6_online_v1`，assignment 来源使用 `online_v6`。如果当前 DB schema 尚不支持这些值，必须在实现本 slice 时同步调整 schema 和 `docs/db_schema.md`。
- 核心验收必须通过公共 CLI、真实 SQLite、真实 embedding、真实小图库和真实日志观察结果；mock/stub/no-op、硬编码 fixture 结果、直接写 person/assignment 或跳过真实扫描不得满足验收。
- 合成 embedding 或 fake backend 只允许用于算法边界单元测试，例如两轮 deferred、近邻复用和同图 IoU 重检；这些测试不能替代真实小图库端到端验收。
- 任何数据库 schema 修改都必须同步更新 `docs/db_schema.md`。

## Feature Slice 1: v6 在线人物归属

- [x] Implementation status: Done

### Behavior

- `hikbox scan start --workspace <path> [--batch-size <n>]` 在 Slice B 的扫描检测阶段成功提交后，继续在同一 scan session 中执行 assignment 阶段；扫描失败、批次未完成或模型/embedding 写入失败时不得进入成功的 assignment 阶段。
- assignment 阶段读取所有 `asset_status='active'` 的 active face observation，并从 `embedding.db` 读取对应 main embedding，构建内存 HNSW cosine 索引；索引中必须包含本次待归属 face 自己。
- 待归属候选先由 `library.db` 选出：active、所属 asset active、无 active assignment、未因当前重检失效的 face observation 都属于候选；随后必须逐一校验其 `embedding.db` main embedding。任一候选缺少 main embedding、embedding 不可解码或维度不是 512，都属于数据损坏并导致 assignment 失败，不得被静默过滤为非候选。
- embedding 库一致性也必须校验：`embedding.db` 中 main embedding 若无法关联到 `library.db` 中任意已知 face observation，则视为孤儿 embedding。孤儿 embedding 不参与本次索引或候选，不得阻塞 assignment 成功；assignment run 摘要和日志必须记录 warning、孤儿数量和可定位的 embedding 稳定键。实现可以保留能关联到 inactive/missing/deleted 历史 face 的旧 embedding，但这些旧 face 同样不得参与本次索引或候选。
- 候选处理顺序必须稳定，至少按 `photo_asset` 的 source/相对路径/scan 顺序和 `face_index` 保持重复运行一致。
- 对每个候选 face，先按 `num_results=max(min_faces, 1)` 搜索 `distance <= max_distance` 的近邻，近邻列表按距离从近到远稳定排序。
- 若 `min_faces > 1` 且近邻数量 `<= 1`，该 face 保持未归属，记录 skipped，不创建 person，不写 active assignment。
- 第一轮中，若近邻数量大于 1 但小于 `min_faces`，该 face 进入 deferred 队列，第一轮不创建 person，也不挂已有 person。
- 第一轮中，若近邻数量达到 `min_faces`，先复用近邻中距离最近且已有 active assignment 的 person；若近邻中没有 person，再补做一次“只查已归属 face”的最近邻搜索，命中后复用该 person；仍无可复用 person 时创建新的匿名 active person。
- 第二轮只处理第一轮 deferred 的 face；第二轮不满足 `min_faces` 时仍不得创建新 person，但如果近邻或补充搜索命中已有 active person，可以挂到该 person，否则保持未归属。
- 创建匿名 person 时，`display_name` 为空、`is_named=0`、`status='active'`，并生成稳定 UUID；匿名 person 是否显示、如何命名由后续 WebUI 子 spec 定义。
- 写入 active assignment 时，必须保证同一 face 同时最多只有一个 active assignment；assignment 记录 `assignment_run_id`、`assignment_source='online_v6'`、可观察置信信息或距离摘要，以及创建/更新时间。
- 已经有 active assignment 的 face 再次进入 assignment 阶段时必须幂等跳过或保持原 active assignment；不得重复写入 assignment，也不得创建重复 person。
- 重复执行已完成的 `hikbox scan start --workspace <path> [--batch-size <n>]` 时，不得重复创建 person，不得重复 active assignment；允许记录一个候选数为 0 或全部 skipped 的 assignment run/log，但人物与 active assignment 结果必须稳定。
- assignment run 必须记录参数快照、候选数量、assigned 数、new person 数、deferred 数、skipped 数、失败数量、开始/结束时间和状态；对应日志必须能定位本次扫描归属是否执行、跳过或失败。
- 同一 asset 重检的 IoU 复用语义必须与原型一致：归一化 bbox IoU `> 0.5` 的新检测框复用旧 face 记录，不刷新旧 embedding 或既有 person；未被新检测框匹配的旧 face 失效并从索引候选中移除；新增 face 入 pending recognition。首版没有公开 force 重检入口，但该语义必须通过算法单元测试覆盖。

### Public Interface

- CLI：`hikbox scan start --workspace <path> [--batch-size <n>]`。
- DB：`library.db` 中的 `person`、active `person_face_assignment` 或等价 assignment 真相表、`assignment_run`、归属摘要和可观测事件。
- DB：`embedding.db` 中 active face 对应的 main embedding 是唯一向量输入。
- 日志：`external_root/logs` 下的 assignment 开始、完成、跳过、失败和摘要记录。
- 测试 fixture：复用 Slice 0 的 `tests/fixtures/people_gallery_scan/manifest.json`，只作为自动化断言输入，不作为产品运行输入。

### Prototype Source

- HNSW cosine 搜索索引来源：`hikbox_pictures/immich_face_single_file.py:131`。
- 引擎默认参数来源：`hikbox_pictures/immich_face_single_file.py:203`。
- detect 时同图 IoU 复用、新 face 入索引与 pending recognition、未匹配旧 face 删除来源：`hikbox_pictures/immich_face_single_file.py:249`。
- 在线归属主逻辑来源：`hikbox_pictures/immich_face_single_file.py:292`。
- pending recognition 队列处理来源：`hikbox_pictures/immich_face_single_file.py:324`。
- 创建/迁移 person assignment 来源：`hikbox_pictures/immich_face_single_file.py:334`。
- `_remove_face` 删除 face、索引、pending、person 空壳来源：`hikbox_pictures/immich_face_single_file.py:351`。
- `_match_existing_face` 的 IoU `> 0.5` 复用规则来源：`hikbox_pictures/immich_face_single_file.py:366`。
- v6 产品语义说明来源：`docs/group_pics_algo.md` 的“v6：Immich 风格的增量在线人物归属”章节。
- 实现可以复制这些原型代码，也可以逻辑等价重写；验收必须证明行为一致。产品实现和验收测试不得直接 import 或调用 `hikbox_pictures.immich_face_single_file` 来满足本 slice。

### Error and Boundary Cases

- `--workspace` 缺失、workspace 未初始化、缺少 `config.json`、缺少 `library.db` 或缺少 `embedding.db` 时返回非 0 退出码和可读错误，不隐式初始化工作区。
- `embedding.db` 缺少某个候选 active face 的 main embedding、embedding 维度不是 512 或向量不可解码时，assignment 阶段失败，必须落库 `assignment_run.status='failed'` 并记录失败原因；CLI 返回非 0 退出码并输出可读错误。
- `embedding.db` 存在无法关联到任意已知 face observation 的孤儿 main embedding 时，assignment 阶段不得失败；该 embedding 不参与索引、候选或归属，只在 assignment run 摘要和日志中记录 warning。
- active face 数为 0 时，assignment 阶段可以成功完成，记录候选数为 0，不创建 person 或 assignment。
- 近邻不足 `min_faces` 且未命中已有 person 的 face 必须保持未归属；不得为了让 manifest 通过而强行创建匿名人物。
- manifest 的确定正向集合只来自 `expected_person_groups`；不在 `expected_person_groups` 中的非目标有脸样本、无脸样本、损坏文件、非支持后缀文件或 tolerance-only 样本，不得推动创建目标匿名人物。
- 已有 active assignment 的 face、inactive face、所属 asset 非 active 的 face 不得作为待归属候选；inactive face 或 missing/deleted asset 不得参与后续搜索和归属。
- 重复执行 scan 或 assignment 不得产生重复 active assignment；如果 DB 唯一约束冲突，必须事务回滚并返回可读错误，而不是留下部分 assignment。
- assignment 失败时不得把 scan session 标记为 completed；已成功提交的检测批次可保留，后续重跑必须能继续完成 assignment，且不得重复检测已完成批次。
- 当前 slice 不负责人工排除；后续 Slice F 引入 exclusion 后，后续归属必须尊重 active exclusion，具体行为由 Slice F 定义。

### Non-goals

- 不做 WebUI、人物命名、人物首页、人物详情页、合并、撤销合并、误归属排除或导出。
- 不做离线全量聚类、AHC、HDBSCAN、person consensus、cluster recall、flip embedding 晚融合或 v5 归属链路。
- 不做受控 exemplar memory、多近邻投票、best-vs-second-best margin、边界样本观察期或大图库索引持久化优化。
- 不提供公开 force 重检 CLI；同一 asset 重检语义只要求内部算法可测试。
- 不把 manifest 暴露为产品配置、产品 API 或运行时业务输入。

### Acceptance Criteria

- AC-1：使用 Slice 0 固定真实小图库执行 `hikbox init -> hikbox source add -> hikbox scan start --workspace <ws> --batch-size 10` 后，manifest 中 `expected_person_groups` 对应的目标人物都形成 active 匿名 person，组内 face 都存在 active `online_v6` assignment；每个目标组内部必须映射到唯一 person，且不同目标组必须映射到不同 person。
- AC-2：manifest 的 `expected_person_groups` 是确定的正向成组集合；不在 `expected_person_groups` 中的非目标有脸样本、无脸样本、损坏文件、非支持后缀文件或 tolerance-only 样本不会创建目标匿名 person；无脸、损坏和非支持后缀样本没有 active assignment。
- AC-3：重复执行同一个 `hikbox scan start --workspace <ws> --batch-size 10` 后，active person 数、active assignment 数、每个 expected group 到 person 的映射都保持不变；不得出现重复 person、重复 assignment 或 face 归属迁移。
- AC-4：成功 assignment run 在 `library.db` 中记录 `status='completed'`、开始/结束时间、`algorithm_version='immich_v6_online_v1'`，参数快照包含 `max_distance=0.5`、`min_faces=3`、`num_results=3`、`embedding_variant='main'`、`distance_metric='cosine_distance'`、`self_match_included=true`、`two_pass_deferred=true`，并记录候选数、assigned 数、new person 数、deferred 数、skipped 数和 failed 数。
- AC-5：`external_root/logs` 中存在 assignment 开始、完成和摘要日志；重复扫描或无候选扫描时日志能区分 skipped/zero-candidate 与失败。
- AC-6：候选 active face 的 embedding 缺失、维度不是 512 或向量不可解码时，`hikbox scan start` 返回非 0 退出码和可读 stderr，`assignment_run.status='failed'` 并记录失败原因，不会留下部分 person 或部分 active assignment，scan session 不得标记为 completed；修复损坏 embedding 后重跑同一命令能继续完成 assignment，且已提交检测批次、face observation 和 main embedding 不重复。
- AC-7：`embedding.db` 存在无法关联到任意已知 face observation 的孤儿 main embedding 时，`hikbox scan start` 仍可完成 assignment；孤儿 embedding 不参与索引、候选或 active assignment，不能让原本低于 `min_faces` 的低证据 face 被推过归属阈值；assignment run 摘要和日志记录 warning、孤儿数量和可定位的 embedding 稳定键。
- AC-8：合成 embedding 算法测试覆盖 v6 两轮在线语义：`min_faces=3` 时“自己 + 1 个近邻”第一轮 deferred、第二轮只能挂已有 person 不能新建 person；“自己 + 2 个近邻”可创建匿名 person；近邻中已有 person 时按最近可复用 person 归属而不是投票。
- AC-9：同一 asset 重检算法测试覆盖归一化 bbox IoU `> 0.5` 时复用旧 face、不刷新旧 embedding/person；未匹配旧 face 失效；新增 face 进入 pending recognition。

### Automated Verification

- 新增 CLI 集成测试，例如 `tests/people_gallery/test_people_gallery_online_assignment.py`，在真实临时目录中通过 subprocess 执行 Slice A 的 `init -> source add`，复用 Slice 0 固定真实小图库，再执行 `hikbox scan start --workspace <ws> --batch-size 10`，读取真实 `library.db`、`embedding.db`、manifest 和日志断言 AC-1 到 AC-5。
- AC-1 的测试必须根据 manifest 的 `expected_person_groups` 在扫描结果中定位目标 face，再断言每个目标组内部归属到唯一 active person，且不同目标组映射到不同 active person；测试不得直接写 person、assignment 或用 manifest 驱动产品代码。
- AC-2 的测试必须覆盖非目标有脸样本、无脸样本、损坏文件、非支持后缀文件和 tolerance-only 样本；不得只检查 happy path，也不得把 manifest 当成产品运行输入。
- AC-3 的测试必须在同一 workspace 连续执行两次公开 `scan start`，比较两次后的 active person、active assignment、expected group 映射和 assignment run 摘要。
- AC-4 和 AC-5 通过读取真实 DB run 状态、开始/结束时间、参数快照、摘要字段和 `external_root/logs` 覆盖；如果实现使用 ops/event 表，也必须同时能通过日志或事件定位本次归属执行结果。
- AC-6 可以通过仓库级集成测试构造损坏的真实 SQLite/embedding 输入后调用公开 `scan start` 验证可读失败；测试必须分别覆盖候选 active face 缺 main embedding、512 维以外的 embedding、不可解码 vector blob，并断言 `assignment_run.status='failed'`、scan session 不是 completed、没有部分 person/assignment。测试还必须在修复同一损坏 embedding 后重跑公开 `scan start`，断言 assignment 成功完成，且已提交检测批次、face observation 和 main embedding 数量不重复。该测试只覆盖损坏库边界，不能替代 AC-1 到 AC-5 的真实小图库核心验收。
- AC-7 可以通过仓库级集成测试构造带孤儿 main embedding 的真实 `embedding.db` 后调用公开 `scan start`；测试必须构造至少一个低证据 face，使其在不计孤儿向量时低于 `min_faces`、但如果孤儿向量错误进入 HNSW 索引就会达到 `min_faces`。测试断言 CLI 成功或 assignment 成功完成、该低证据 face 仍未归属、孤儿未产生 active assignment、assignment run 摘要和日志包含 warning、孤儿数量和可定位的 embedding 稳定键。
- AC-8 和 AC-9 使用合成 embedding、fake backend 或等价算法单元测试覆盖，因为首版没有公开 force 重检入口，也无法仅靠真实小图库稳定触发所有两轮 deferred 和 IoU 边界。单元测试必须执行生产算法代码，不得断言复制粘贴的测试内逻辑。
- 测试不得 import 或调用 `hikbox_pictures.immich_face_single_file` 作为产品实现替身；原型文件只作为语义参考。
- 测试必须能在只有脚手架、硬编码成功、直接写 DB、跳过 assignment、用 manifest 直接生成 person/assignment、或绕过 `scan start` 的实现上失败。

### Done When

- 所有验收标准都通过自动化验证。
- `docs/db_schema.md` 已同步描述本 slice 引入或确认的 person、assignment_run、active assignment、`assignment_source='online_v6'`、参数快照和归属摘要 schema。
- 重复扫描、低证据样本、无脸样本、损坏 embedding 和孤儿 embedding warning 都有可观察、可测试的结果。
- 没有核心需求通过直接状态修改、硬编码数据、占位行为、fake assignment、manifest 驱动产品逻辑或 no-op 路径满足。
