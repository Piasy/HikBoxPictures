# Immich v6 人物图库产品化 Slice F：误归属排除 Spec

## Goal

在已有命名、合并和 active assignment 的 workspace 上，通过 `hikbox-pictures serve --workspace <path> [--port <port>] [--person-detail-page-size <n>]` 提供的人物详情页批量排除误归属样本：排除后样本立即从当前 person 移除，exclusion 真相持久化落库，后续 `scan start` 不得把这些 face 自动归回被排除的 person；但它们不是全局冻结样本，在新的证据下仍可重新形成别的人物。

## Global Constraints

- 本 spec 是父 spec `docs/superpowers/specs/2026-04-24-immich-v6-people-gallery-productization-spec.md` 的 Slice F，只覆盖人物详情页批量排除、exclusion 真相和后续在线归属对 exclusion 的尊重；不定义手动改派、恢复排除、人物拆分或导出模板。
- 本 slice 依赖 Slice 0 的固定真实小图库和 manifest、Slice A 的 workspace/source 契约、Slice B 的扫描和 artifact 契约、Slice C 的匿名 person 与 active assignment 契约、Slice D 的人物详情页和命名契约，以及 Slice E 的 merge/undo 契约；不重新定义初始化、扫描、分页、命名或 merge winner 语义。
- 公共入口仍然只有 `hikbox-pictures serve --workspace <path> [--port <port>] [--person-detail-page-size <n>]` 提供的真实 WebUI/API，以及后续再次执行的真实 `hikbox-pictures scan start --workspace <path> [--batch-size <n>]`；本 slice 不新增 CLI。
- 本 slice 只定义一种排除操作：人物详情页上的批量排除。用户勾选 1 条样本再提交，等价于“批量排除 1 条”；本 slice 不再定义独立的“单条排除”按钮、接口或语义。
- exclusion 的持久化真相必须是“这张 face 不能再自动归到这个 person”，而不是“这条 assignment 被点过一次按钮”。因此，POST 可以提交 `assignment_id`，但落库后的稳定 exclusion 键必须至少能表达 `face_observation_id + excluded_person_id`；同一对 `face_observation_id + excluded_person_id` 不得重复写入。一个 face 在不同时间可以累积针对不同 `person_id` 的 exclusion。
- 排除成功只会失效当前 active assignment，并记录 exclusion；不得删除 `face_observations`、`assets`、crop/context artifact、merge/name 历史或其他扫描产物。
- 没有 active assignment、但带有一个或多个 exclusion 的 face，在后续 `scan start` 中仍然是可再次归属的候选；区别是在线归属在为该 face 选择 person 时，必须排除所有 `excluded_person_id`。若没有满足条件的其它 active person，则该 face 保持未归属。
- 对于本 slice 的核心重扫验收，`tests/fixtures/people_gallery_scan/` 这套固定图库中的单条排除和两条批量排除，在不新增任何 source 的前提下再次执行 `scan start` 时，被排除的 face 必须保持未归属；不得因为 exclusion 被实现成全局删除、全局冻结或错误重建 assignment 而通过。
- 当有新的证据来源出现时，exclusion 只禁止“回到被排除的 person”，不禁止这些 face 重新形成新的 active 匿名人物或挂到别的 active person。用于证明这条语义的增量扫描路径，固定复用 Slice 0 新增的 `tests/fixtures/people_gallery_scan_2/` 作为第二个真实 source；不得通过复制第一套图库、拼装子目录或直接改库制造这条路径。
- 排除成功属于真实人物相关写入；如果它作用于 Slice E 最近一次 merge 的 winner/loser，则那次 merge 的 undo 资格会失效。这个 cross-slice 结果已经由 Slice E 定义，本 slice 不重复定义 undo 页面行为，只要求实现不得绕开这条真相。
- 父 spec 已声明“导出运行中禁止命名、合并、撤销合并、排除等人物归属写操作”。可观察的导出运行态、以及这把锁如何通过公共入口验证，留到 Slice G 定义；本 slice 不单独扩展导出系统，也不在本 slice AC 中发明导出 precondition。
- manifest 只允许作为自动化断言输入；产品运行、排除逻辑、exclusion 真相和后续 assignment 逻辑不得读取 manifest。
- 核心行为必须通过真实 `hikbox-pictures serve` 进程、真实 HTML 页面、真实表单提交、真实 `hikbox-pictures scan start`、真实 SQLite 和真实 artifact 验证；mock/stub/no-op、直接改库、硬编码页面结果、跳过 HTTP 服务或让测试直接调用内部模板函数不得满足验收。
- 如果实现引入或调整 exclusion 真相表、`person_face_assignments` 失效语义、相关索引或 merge/undo 与 exclusion 的联动 schema，必须同步更新 `docs/db_schema.md`。

## Feature Slice 1: 人物详情页批量排除

- [x] Implementation status: Done

### Behavior

- 人物详情页为当前 person 的每个 active assignment 样本暴露可选择控件，并提供一个统一的“批量排除”提交动作。
- 一次批量排除请求必须包含 1 条或多条当前详情页所属 person 的 active `assignment_id`；服务端必须把这些 `assignment_id` 解析回稳定的 `face_observation_id`，再落库 exclusion 真相。
- 批量排除成功后，这些 assignment 立即失效；返回后的详情页不再展示这些样本，当前 person 的可见样本数与 DB 中 active assignment 数同步减少。
- 如果排除后该 person 仍然有 active assignment，成功路径必须走 PRG：`POST /people/{person_id}/exclude -> 303 -> GET /people/{person_id}`，并在当前详情页显示非错误成功反馈。
- 如果排除后该 person 已无任何 active assignment，成功路径也必须走 PRG，但目标变为 `POST /people/{person_id}/exclude -> 303 -> GET /people`；此时该 person 不再出现在首页，直接访问 `GET /people/{person_id}` 返回 404 或等价可读错误页。
- 每条被成功排除的样本都必须新增且仅新增 1 条 exclusion 真相记录；这条记录至少能查询 `face_observation_id`、`excluded_person_id`、触发时间，以及可选的来源 `assignment_id` 或请求上下文。
- 后续再次执行 `hikbox-pictures scan start --workspace <path> [--batch-size <n>]` 时，任何没有 active assignment 的 face 都会重新进入在线归属候选；但如果该 face 对某个 person 已存在 active exclusion，则在线归属在为它选择 person 时不得把它写回这个 `excluded_person_id`。
- 如果后续存在新的 active person 或新的证据，使某个被排除 face 可以稳定归到另一个未被排除的 person，则系统可以重新写入一条新的 active assignment 到那个其它 person；已有 exclusion 记录继续保留，不会被自动删除。

### Public Interface

- 页面：`GET /people/{person_id}`
- 页面表单：`POST /people/{person_id}/exclude`
- 表单字段：一个或多个重复出现的 `assignment_id`
- 成功返回：
  - 目标 person 仍然存在时：`303` 到 `GET /people/{person_id}`
  - 目标 person 被排空时：`303` 到 `GET /people`
- DB：`person`、`person_face_assignments`、exclusion 真相表或等价事件表
- 页面定位：详情页样本卡必须继续暴露稳定 `data-assignment-id` / `data-face-observation-id` 或等价标识，便于页面选择结果与 DB 对齐

### Error and Boundary Cases

- 未选择任何 `assignment_id`、同一请求中重复提交同一个 `assignment_id`、`person_id` 不存在、`assignment_id` 不存在、`assignment_id` 不属于当前 `person_id`、或其中任一 `assignment_id` 在处理时已经不是 active，整个请求都必须失败并返回明确、可读的错误反馈，DB 不改变。
- 批量排除必须全有或全无；不得出现一部分 assignment 已失效、另一部分失败，或 exclusion 只写入部分样本的半完成状态。
- 如果批量排除在请求处理中途遭遇 exclusion 落库失败、assignment 失效失败、唯一约束冲突或等价事务内错误，整个请求都必须整体回滚：不得留下“前几条样本已被排除、后几条失败”的状态，失败响应也必须包含明确、可读的错误反馈。
- 对同一 `face_observation_id + excluded_person_id` 的重复排除不得产生第二条 exclusion 记录；如果用户或测试通过 crafted 请求重放同一批次排除，服务端必须明确拒绝或以等价可读错误终止，而不是默默追加重复真相。
- 当前 slice 不支持“恢复排除”“移动到指定人物”“把被排除样本送入独立待审核页”或“跨人物批量拆分”；这些能力都不能通过隐藏接口或未文档化参数偷偷出现。
- 导出运行锁的 cross-slice 契约继续生效，但导出运行态的公共可观测语义留给 Slice G；本 slice 不定义如何创建导出运行态。

### Non-goals

- 不做独立的单条排除入口。
- 不做恢复排除或 undo exclusion。
- 不做手动改派到指定人物。
- 不做人物拆分向导、待审核页或排除历史浏览页。
- 不做独立导出锁页面或导出运行态 UI。

### Acceptance Criteria

- AC-1：在基于 Slice 0 固定图库 `tests/fixtures/people_gallery_scan/` 完成真实扫描的 workspace 上，根据 manifest 和 DB 定位一个目标人物，在其详情页只勾选 1 条当前可见的 active assignment 并提交批量排除。浏览器必须观察到 `POST /people/{person_id}/exclude -> 303 -> GET /people/{person_id}`；返回后的详情页不再显示这条样本，页面样本数和 DB 中该 person 的 active assignment 数都减少 1。DB 中原 assignment 失效，并新增 1 条 exclusion 真相记录，且该记录绑定被排除 face 的 `face_observation_id` 与当前 `excluded_person_id`。
- AC-2：在独立 workspace 中，对同一个目标人物的详情页一次勾选恰好 2 条当前可见 active assignment 后提交批量排除。成功后，详情页和 DB 中该 person 的 active assignment 集合必须精确等于排除前集合减去本次所选 2 条；这 2 条被选样本都对应新增且仅新增 1 条 exclusion 真相记录，不得部分成功、部分遗漏。
- AC-3：在独立 workspace 中，以 `--person-detail-page-size 50` 启动真实 `serve`，定位一个当前 Slice 0 基线中关联 18 个 asset 的目标人物，并在同一详情页一次选中其全部样本后提交批量排除。成功后浏览器必须观察到 `POST /people/{person_id}/exclude -> 303 -> GET /people`；该 person 不再出现在首页；直接访问其详情页返回 404 或等价可读错误页；DB 中该 person 不再有 active assignment，但原始 `face_observations`、artifact 和 exclusion 真相都保留。
- AC-4：在 AC-1 或 AC-2 成功后，仍然只使用 Slice 0 固定图库 `tests/fixtures/people_gallery_scan/` 的同一 workspace，再次执行真实 `hikbox-pictures scan start --workspace <ws> --batch-size 10`。被排除的 `face_observation_id` 在这次重跑 assignment 后必须保持未归属：这些 face 不得产生任何新的 active assignment，不得回到原 person，也不得归到其它 person；原 person 的样本数不得恢复，且 exclusion 记录数不得重复增长。
- AC-5：对公共入口 `POST /people/{person_id}/exclude` 发起 crafted 请求时，`person_id` 不存在、未选择任何 `assignment_id`、同一请求中重复提交同一个 `assignment_id`、包含不存在 assignment、包含不属于当前 person 的 assignment、或包含已经被排除后不再 active 的 assignment，这几类情况都必须整体失败并返回明确、可读的错误反馈；DB 中 active assignment 集合、exclusion 记录数和人物首页/详情可观察结果都保持不变。
- AC-6：在独立 workspace 中，先用 Slice 0 固定图库 `tests/fixtures/people_gallery_scan/` 完成真实扫描；再通过 Slice D 把 `target_alex` 命名；再通过 Slice E 把已命名 `target_alex` 与 `target_blair` 合并，使 `target_alex` 成为 winner；然后在该 winner 的详情页一次选中并批量排除所有来自第一套固定图库、原本属于 `target_blair` 的样本。此时这些 `target_blair` face 都必须处于未归属状态。接着把 `tests/fixtures/people_gallery_scan_2/` 作为第二个真实 source 加入，并执行 `hikbox-pictures scan start --workspace <ws> --batch-size 10`。扫描完成后，系统必须存在一个 active 的匿名 `target_blair` person，且它与已命名 `target_alex` winner 不是同一个 person；`people_gallery_scan_2` 中 5 张新增 `target_blair` 照片对应的 face，以及第一套固定图库里先前被排除的旧 `target_blair` face，都必须归到这个 active 匿名 `target_blair` person，而不得回到已命名的 `target_alex` winner。该 AC 用来证明 exclusion 只禁止“回到原 person”，而不是全局冻结这批 face。
- AC-7：在 AC-6 形成新的 active 匿名 `target_blair` person 后，从这个新匿名 blair person 的详情页中选择 1 条“第一套固定图库里先前被排除、后来又重新归到新 blair person”的旧 `target_blair` face，再次提交批量排除 1 条。成功后，DB 中同一个 `face_observation_id` 必须同时保留两条 exclusion 真相记录：一条指向先前的已命名 `target_alex` winner，另一条指向当前新的匿名 `target_blair` person；这两条记录的 `excluded_person_id` 必须不同，且两条记录都带非空触发时间。该 AC 用来锁定 exclusion 真相必须支持同一 face 对不同 person 的累积排除，而不是“每个 face 只允许一条 exclusion”。
- AC-8：在独立 workspace 中，对同一详情页选择 2 条当前可见 active assignment 后发起一次真实批量排除，并通过可重复的确定性 fault injection 让请求在事务中途失败，例如第一条样本的 exclusion/assignment 更新已经执行、但第二条尚未完成时失败。系统必须整体回滚：这 2 条 assignment 都仍然保持 active，exclusion 真相记录数保持请求前不变，详情页样本集合与请求前完全一致，且失败响应或落地错误页包含明确、可读的错误反馈。不得出现“前一条成功、后一条失败”的半完成状态。
- AC-9：在独立 workspace 中，先通过 Slice E 完成一次仍可撤销的 two-person merge，再在 merge winner 的详情页通过本 slice 成功排除 1 条当前 active 样本；随后无论首页撤销入口是否已禁用/隐藏，只要再触发真实 `POST /people/merge/undo`，都必须失败并返回明确、可读的错误反馈，说明这次 merge 在 exclusion 后已经不再可撤销；merge 结果、person 状态、active assignment 归属、exclusion 真相和 merge operation 状态都保持不变。该 AC 用来把“exclusion 属于真实人物相关写入，会让最近一次 merge 的 undo 资格失效”落到自动化验收，而不是只停留在跨 slice 文本约束。

### Automated Verification

- AC-1 到 AC-4、AC-6、AC-7 优先通过 Python Playwright + pytest 覆盖，复用仓库现有 `tests/people_gallery/test_webui_*_playwright.py` 入口风格；主路径固定为真实 `init -> source add tests/fixtures/people_gallery_scan -> scan start --batch-size 10 -> serve`，并同时读取真实 `library.db` 对齐页面结果。
- AC-1 的测试必须在网络层看到 `POST /people/{person_id}/exclude -> 303 -> GET /people/{person_id}`，并同时断言页面样本消失、person 样本数减 1、原 assignment 失效，以及新增 exclusion 记录绑定的是同一 `face_observation_id + excluded_person_id`，而不是只记了一次表单提交。
- AC-2 的测试必须在排除前后分别读取该 person 的 active assignment id 集合，断言成功后精确等于“排除前集合减去本次所选 2 条”；不能只靠页面计数。
- AC-3 的测试必须显式以 `--person-detail-page-size 50` 启动真实服务，确保这 18 个样本都通过公开页面一次可选；随后断言网络层出现 `POST -> 303 -> GET /people`，首页卡片消失、详情页返回 404 或等价可读错误页、DB 无 active assignment，但 `face_observations` 和 artifact 仍在。
- AC-4 的测试必须复用 AC-1 或 AC-2 产生的“1 条或 2 条 exclusion”主路径，并在排除后的同一 workspace、且不新增任何 source 的前提下重新执行公开 `scan start`；随后读取 DB 证明这些被排除 `face_observation_id` 仍然没有任何 active assignment，而不是只断言“没回原 person”。
- AC-5 可以用服务级集成测试覆盖：保持真实 `serve` 进程运行，通过真实 HTTP `application/x-www-form-urlencoded` POST crafted 表单，覆盖“`person_id` 不存在 / 空选择 / 同一 assignment 重复提交 / 他人 assignment / 不存在 assignment / 已排除 assignment”六类失败，并断言全有或全无、错误可读、DB 不变。这里不需要浏览器，但必须经过真实 HTTP 入口和真实 DB。
- AC-6 的测试必须在同一真实 workspace 中串起 Slice D、Slice E 和 Slice F：先命名 `target_alex`，再合并 `target_alex + target_blair`，再批量排除 merged winner 下所有原 `target_blair` 样本，然后追加第二个真实 source `tests/fixtures/people_gallery_scan_2/` 并重新执行公开 `scan start`。测试必须通过 manifest、页面和 DB 共同证明：新增的 5 张 `target_blair` 照片以及之前被排除的旧 `target_blair` face，最终都进入同一个 active 匿名 `target_blair` person，而不是回到已命名 `target_alex` winner。
- AC-7 的测试必须直接复用 AC-6 的同一 workspace 和同一条旧 `target_blair` face：先确认它已经拥有“排除自 alex winner”的 exclusion 真相，再在新匿名 blair person 下通过真实页面把它再次排除。测试必须读取 DB 证明同一个 `face_observation_id` 最终拥有两条 `excluded_person_id` 不同的 exclusion 记录，且两条记录的触发时间都非空；不能只验证“再次排除成功”。
- AC-8 可以降级为 CI 可运行的服务级自动化验证，因为浏览器/E2E 无法稳定制造批量排除事务中途故障；但它必须针对真实 `serve` 进程、真实 HTTP/request handling 路径和真实 SQLite 执行确定性 fault injection，而不是直接写 SQL、fake transaction manager 或伪造回滚结果。测试必须证明失败后两个目标 assignment 都仍然 active、exclusion 记录数不变，且随后访问真实详情页时样本集合与请求前一致。
- AC-9 可以通过 Playwright 或服务级集成测试覆盖，但必须串起真实 `POST /people/merge`、真实 `POST /people/{person_id}/exclude` 和真实 `POST /people/merge/undo` 公共入口；测试必须证明 exclusion 成功后，随后的 undo 被服务端拒绝，且拒绝后 merge 结果与 exclusion 结果都保持不变。不得通过直接改 merge operation 状态或伪造“不可撤销”标志满足验收。
- 排除类调试产物按需保留到 `.tmp/people-gallery-exclusion/`；默认以 DOM/HTTP/DB 断言为主，不默认保留截图。
- 测试不得通过直接写 SQL 失效 assignment、手工插 exclusion、跳过 HTTP 表单、跳过真实 `scan start`、或直接调用内部 service 来冒充排除成功。

### Done When

- 所有验收标准都通过自动化验证。
- exclusion 真相被稳定持久化为 person 维度限制，而不是一次性 UI 动作或全局冻结标记。
- 同图库重扫时，被排除 face 在本 slice 主路径里保持未归属；在“命名 alex -> merge alex/blair -> 排除所有旧 blair -> 加入 `people_gallery_scan_2`”这条增量路径里，被排除旧 blair face 能与新增 blair face 一起重新形成 active 匿名 `target_blair` person。
- 批量排除在事务中途失败时不会留下部分失效的 assignment 或半条 exclusion 真相；merge 后发生 exclusion 时，最近一次 undo 会被真实公共入口拒绝。
- `docs/db_schema.md` 已同步描述 exclusion 真相表、`person_face_assignments` 失效语义，以及 exclusion 对后续在线归属的约束。
- 没有核心需求通过直接状态修改、硬编码页面结果、假排除、全局冻结 face、fake integration 或绕过真实 HTTP/CLI 路径满足。
