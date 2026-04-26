# Immich v6 人物图库产品化 Slice E：人物合并与最近一次撤销 Spec

## Goal

在已有匿名/已命名人物和 active assignment 的 workspace 上，通过 `hikbox-pictures serve --workspace <path> [--port <port>] [--person-detail-page-size <n>]` 提供的本机 WebUI，让用户在人物首页把被误拆成两个 person 的同一人物合并，并在这次 merge 的 winner/loser 尚未发生后续真实人物相关写入时撤销最近一次成功合并；合并是 assignment 层的真实归一，后续扫描新增的 loser-like 样本必须继续归到 winner；这条增量扫描验收固定复用 Slice 0 新增的 `tests/fixtures/people_gallery_scan_2/`。

## Global Constraints

- 本 spec 是父 spec `docs/superpowers/specs/2026-04-24-immich-v6-people-gallery-productization-spec.md` 的 Slice E，只覆盖人物首页 two-person merge、最近一次撤销、相关账本与错误边界。
- 本 slice 依赖 Slice 0 的固定真实小图库和 manifest、Slice A 的 workspace/source 契约、Slice B 的扫描和 artifact 契约、Slice C 的匿名 person 与 active assignment 契约，以及 Slice D 的人物首页/详情页、命名/重命名和 `serve` PRG 契约；不重新定义扫描、在线归属、分页或命名语义。
- 本 slice 中用于验证“merge 后新增 loser-like 样本继续归到 winner”和“发生新增人物相关写入后 undo 被拒绝”的第二批真实扫描，统一复用 Slice 0 固定入库的 `tests/fixtures/people_gallery_scan_2/` 作为第二个 source；不得通过从 `tests/fixtures/people_gallery_scan/` 临时复制照片、拼装子目录或其它等价方式绕开这套固定增量验收基线。
- 公共入口仍然只有 `hikbox-pictures serve --workspace <path> [--port <port>] [--person-detail-page-size <n>]` 提供的真实 WebUI/API；本 slice 不新增 CLI。
- 本 slice 当前明确只支持 exactly-two merge：每次合并请求只能包含两个 person，且 undo 账本也只需要恢复这一次 two-person merge 的 winner/loser 关系。一次合并三个及以上 person 不在本 slice 范围内。
- 合并不是展示层 alias，也不是只在导出时解释的“人物关联”。合并必须真实修改 person 与 active assignment 的持久化归属，使后续 WebUI、导出和在线 assignment 看到的是同一个 canonical winner。
- winner 规则固定为：一个已命名人物加一个匿名人物时，已命名人物为 winner；两个匿名人物时 active 样本数更多者为 winner；若 active 样本数相同，则 `person_id` 更小者为 winner；两个已命名人物时必须拒绝。
- 合并成功后，loser person 必须变为 `inactive`，且不再出现在首页、详情页或后续可操作人物集合中；loser 的 active assignment 必须全部迁移到 winner，不能保留展示层二次解释。
- 本 slice 必须定义可撤销账本或等价快照，使“最近一次撤销”能够恢复 winner 和 loser 的可见性、名称、active assignment 归属，以及合并前的可观察首页/详情结果；不得依赖模糊推断或重新跑聚类恢复。
- 撤销只支持“最近一次成功且其 winner/loser 尚未发生后续真实人物相关写入的合并”。这里的“后续真实人物相关写入”只指会改变这次 merge 的 winner/loser 真相的持久化写入，例如：新的 active assignment 写入到 winner/loser、对 winner/loser 命名/重命名、再次合并 winner/loser、排除 winner/loser 样本或等价写入。与该 merge 无关的第三个人物写入不影响撤销资格；不会产生持久化变化的 no-op 请求也不得让撤销失效。
- 合并成功后，后续新增 source 并再次执行 `hikbox-pictures scan start --workspace <path>` 时，原本会归到 loser 的新样本必须因为历史锚点已归并到 winner，而继续归到 winner；这条语义属于本 slice 的核心验收。
- 成功的合并和成功的撤销都必须走 PRG：`POST -> 303 -> GET /people`。不允许成功后直接返回 `200` 重渲染 POST 响应，也不允许成功后跳到人物详情页。
- 失败路径必须返回可读错误，且 DB 不改变；实现可以选择在 `GET /people` 重新渲染时显示错误，也可以返回等价错误页，但不得留下半完成状态。
- manifest 只允许作为自动化断言输入；产品运行、合并决策、撤销账本和后续 assignment 逻辑不得读取 manifest。
- 核心行为必须通过真实 `hikbox-pictures serve` 进程、真实 HTML 页面、真实表单提交、真实 SQLite 和真实 `scan start` 验证；mock/stub/no-op、直接改库、硬编码页面结果、跳过 HTTP 服务或让测试直接调用内部模板函数不得满足验收。
- 如果实现引入或调整 merge operation、merge snapshot、person 状态约束、assignment 迁移账本或撤销游标 schema，必须同步更新 `docs/db_schema.md`。

## Feature Slice 1: 人物合并

- [x] Implementation status: Done

### Behavior

- 人物首页提供 two-person merge 入口；用户只能从首页当前可见的 active person 中选择两个合并对象。
- 合并请求必须携带恰好两个 `person_id`；winner 完全由已命名优先、active 样本数和 `person_id` 决定，页面不提供手选 winner。
- 合并 winner 规则固定为：
  - 恰好一个已命名人物加一个匿名人物时，该已命名人物为 winner。
  - 两个匿名人物时，active assignment 样本数更多者为 winner。
  - 两个匿名人物且 active assignment 样本数相同，则 `person_id` 更小者为 winner。
  - 两个已命名人物时，请求直接失败，不支持用户额外手选 winner。
- 合并成功后，winner `person.id`、`display_name`、`is_named` 保持不变；唯一 loser 的 active assignment 全部迁移到 winner，且迁移后仍保持 active。
- loser `person.status` 必须变为 `inactive`；合并后 loser 不再出现在首页，访问其详情页返回 404 或等价可读“人物不存在”页。
- 合并成功后，winner 的首页卡片样本数和详情页样本集合必须等于这两个参与 person 合并前 active assignment 的并集；同一 assignment 不得重复、丢失或残留在 loser 名下。
- 合并必须持久化一条 merge operation 真相记录，至少能查询：merge id、发起时间、winner person id、loser person id、参与 assignment id 列表、winner/loser 合并前的 `display_name`/`is_named`/`status`，以及“这次合并的 winner/loser 之后是否已发生后续真实人物相关写入、因此不再可撤销”的判定依据。
- 成功合并后返回 `303` 到 `GET /people`，首页展示成功反馈，并且合并结果已经在首页可见。
- 合并后的 winner 会成为后续在线 assignment 的 canonical person：新增 source 并再次执行 `scan start` 时，如果新 face 命中的历史锚点来自原 loser，新的 active assignment 也必须写到 winner，而不是 resurrect loser 或创建第三个 person。

### Public Interface

- 页面：`GET /people`
- 页面表单：`POST /people/merge`
- 表单字段：恰好两个重复出现的 `person_id`
- 成功返回：必须是 `303` 到 `GET /people`
- DB：`person`、`person_face_assignments`、merge operation 真相表及其 snapshot/关联表
- 日志或事件：如果实现为 merge/undo 写审计日志，也必须能和 merge operation 真相表相互对应

### Error and Boundary Cases

- `person_id` 数量不是恰好 2、两个 `person_id` 重复、包含不存在 person、或包含 `inactive` person 时，合并失败并显示可读错误，DB 不改变。
- 请求中包含两个已命名人物时，合并失败并显示可读错误，DB 不改变。
- 合并执行期间若出现唯一约束冲突、事务失败、或 assignment 迁移不完整，必须整体回滚；不得留下 loser 已 `inactive` 但 assignment 未全迁、或 assignment 已部分迁移的状态。
- 本 slice 不支持跨 workspace 合并、一次合并三个及以上 person、批量指定自定义 winner、或把已 inactive 的历史 loser 重新拉回合并候选。
- 导出运行中禁止人物归属写操作的跨 slice 契约继续生效；当后续 Slice G 引入可观察导出运行锁后，本 slice 的合并必须被拒绝。本 slice 先只定义这个锁定契约，不负责实现导出。

### Non-goals

- 不支持一次合并三个及以上 person。
- 不支持合并两个已命名人物。
- 不支持撤销任意历史 merge，只支持最近一次满足条件的 merge。
- 不支持只在 WebUI/导出层解释 alias、而不修改 active assignment 真相。
- 不支持在人物详情页发起合并，也不支持人物搜索、筛选、批量命名或批量改 winner。

### Acceptance Criteria

- AC-1：基于 Slice 0 主基线固定图库 `tests/fixtures/people_gallery_scan/` 初始化一个新的 workspace 并完成真实扫描后，根据 manifest 与页面定位 `target_alex` 和 `target_casey` 对应的两个匿名 person，通过首页执行 two-person merge。由于这两个 target person 在该基线中的 active assignment 样本数相同，winner 必须等于这两者中 `person_id` 更小的那个，不得退化为点击顺序、页面 DOM 顺序、HTTP 请求体字段顺序或 DB 默认顺序。成功后，这次参与 merge 的两个人里只剩 winner 继续可见；winner 的样本数等于两人合并前样本数之和，loser 不再出现在首页，访问 loser 详情页返回 404 或等价可读错误页。
- AC-2：在独立于 AC-1 的新 workspace 中，先复用 AC-1 的真实路径把 `target_alex` 和 `target_casey` 合并成一个仍然匿名的 winner，再把该 winner 与仍然匿名的 `target_blair` 发起第二次 two-person merge，并在 `POST /people/merge` 请求体里把 `target_blair` 对应的 `person_id` 放在第一个、把前述匿名 winner 放在第二个。由于这次 merge 中匿名 winner 的 active assignment 样本数更多，它必须继续成为 winner，不得退化为始终取请求体第一个 `person_id` 为 winner。成功后，这次参与第二次 merge 的两个人里只剩新的 winner 继续可见；winner 的样本数等于两人合并前样本数之和，loser 不再出现在首页，访问 loser 详情页返回 404 或等价可读错误页。
- AC-3：在 AC-1 的 two-person merge 成功后，把 Slice 0 固定入库的 `tests/fixtures/people_gallery_scan_2/` 作为第二个真实 source，通过 `hikbox-pictures source add --workspace <ws> tests/fixtures/people_gallery_scan_2` 和 `hikbox-pictures scan start --workspace <ws> --batch-size 10` 触发增量扫描；扫描完成后，`people_gallery_scan_2/manifest.json` 中属于 `target_alex` 的 5 张新增照片和属于 `target_casey` 的 5 张新增照片产生的 active assignment，必须全部归到 AC-1 的 merge winner；属于 `target_blair` 的 5 张新增照片则必须继续归到 `target_blair` 对应的 person，不得被错误归到 winner、resurrect loser 或创建多余 person。
- AC-4：在独立于 AC-1 的新 workspace 中，先复用 AC-1 的真实路径把 `target_alex` 和 `target_casey` 合并成一个仍然匿名且样本数更高的 winner；再通过 Slice D 把 `target_blair` 命名为 manifest `display_name`，随后把这个已命名 `target_blair` 与前述匿名 winner 发起 two-person merge。由于这次 merge 里匿名 winner 的 active assignment 样本数更多，已命名 `target_blair` 仍然必须成为 winner，从而证明 winner 规则不会退化为“只有平票时已命名才赢”或“始终取样本数更多者”。成功后该 winner 的 `display_name`、`is_named` 保持不变；匿名 loser 的全部 active assignment 迁移到该 winner，首页中不再显示 loser。
- AC-5：对公开入口 `POST /people/merge` 发起 crafted 请求时，`person_id` 数量不是恰好 2、两个 `person_id` 重复、包含不存在 person，或包含已 `inactive` loser，都必须失败并显示明确、可读的错误反馈；`person.status`、`display_name`、active assignment 归属和 merge operation 记录数都保持不变。
- AC-6：尝试合并两个已命名 target person 时，页面或响应中显示明确、可读的错误反馈；`person.status`、`display_name`、active assignment 归属、merge operation 记录数都保持不变。
- AC-7：成功合并后，winner 详情页和 DB 中的 active assignment 集合必须精确等于参与 merge 的 winner/loser 在合并前 active assignment 集合的并集；不得出现重复、丢失或残留 assignment。loser 的 active assignment 数必须变为 0，且 `person.status='inactive'`。
- AC-8：成功合并必须走 `POST /people/merge -> 303 -> GET /people`，并在首页显示非错误成功反馈；不得返回 `200` 直接重渲染 POST 响应，也不得成功跳转到人物详情页。
- AC-9：当 merge 请求在 assignment 迁移、loser 置为 `inactive` 或 merge operation 落库过程中遭遇可重复故障注入并最终失败时，系统必须整体回滚：winner/loser 的 `person.status`、`display_name`、active assignment owner、merge operation 记录数和首页/详情可观察结果都保持与请求前一致；不得出现 loser 已 `inactive`、assignment 部分迁移、或半条 merge operation 记录。失败响应或落地错误页还必须包含明确、可读的错误反馈，不能退化为裸 `500`、空白页或通用异常页。

### Automated Verification

- 新增或扩展 Playwright 端到端测试，例如在 `tests/people_gallery/test_webui_people_gallery_playwright.py` 中补充 merge 场景；至少拆成两条真实 workspace 主路径：第一条执行真实 `init -> source add tests/fixtures/people_gallery_scan -> scan start -> serve`，覆盖 AC-1、AC-3 和后续 undo 相关场景；第二条在独立 workspace 中复用同一基线，先完成 AC-1 的匿名 tie merge，再继续完成 AC-2 的匿名不同票数 merge。
- AC-1、AC-2、AC-3、AC-4、AC-6、AC-7、AC-8 必须通过真实首页 two-person merge 控件和真实 `POST /people/merge` 提交触发，再同时读取真实 `library.db` 验证 `person.status`、`display_name`、`is_named`、active assignment owner、merge operation 记录和首页/详情结果。
- AC-1 的测试必须在完整 `tests/fixtures/people_gallery_scan/` 基线上定位 `target_alex` 和 `target_casey` 两个匿名 person，并通过真实首页 merge 控件触发提交；测试必须同时记录这两人的 `person_id`、首页卡片顺序和实际 `POST /people/merge` 请求体，但最终断言只能依赖“较小 `person_id` 获胜”这一规则，而不是任何页面顺序或请求体顺序。
- AC-2 的测试必须在独立 workspace 中复用 AC-1 的真实路径，先得到一个由 `target_alex + target_casey` 合成的匿名 winner，再把 `target_blair` 放在第二次 merge 请求体的第一个位置、把该匿名 winner 放在第二个位置，并证明最终 winner 仍然是样本数更多的匿名 winner，而不是请求体第一个 `person_id`。
- AC-3 的测试必须直接把真实 `tests/fixtures/people_gallery_scan_2/` 目录作为 AC-1 同一 workspace 的第二个 source，再通过真实 `source add` 和真实 `scan start` 触发新增归属；测试必须根据 `people_gallery_scan_2/manifest.json` 中属于 `target_alex`、`target_casey`、`target_blair` 的新增 asset id/文件名，以及 DB active assignment 结果，证明 alex/casey 的新增样本都被归到 winner，而 blair 的新增样本不会被归到 winner。测试不得通过复制文件、筛选出子目录、直接插入 assignment、直接更新 `person_id`、或跳过 `scan start` 满足验收。
- AC-4 的测试必须在独立 workspace 中先完成一次 `target_alex + target_casey` 的匿名 merge，得到样本数更高的匿名 winner，再通过 Slice D 的真实命名路径把 `target_blair` 命名，最后回到首页发起“已命名 `target_blair` + 匿名 merged winner”合并。测试必须证明最终 winner 固定是已命名 `target_blair`，即使它样本数更少，也不会输给匿名 winner。
- AC-5 的测试必须通过真实运行中的 `hikbox-pictures serve` 进程，向公共入口 `POST /people/merge` 发送构造过的 `application/x-www-form-urlencoded` 请求，至少覆盖“`person_id` 数量不是 2”“重复 `person_id`”“不存在 person”“先成功 merge 再拿已 `inactive` loser 重复提交”这几类服务端拒绝场景；每一种失败都必须断言页面或响应里存在明确、可读的错误反馈。测试不得只依赖首页 UI 禁止选择来替代服务端校验。
- AC-6 的测试必须断言两个已命名人物 merge 失败时页面或响应里存在明确、可读的错误反馈，而不是只有 bare 400、空白响应或通用错误页。
- AC-7 的测试必须在 merge 前后分别读取 winner 和 loser 的 active assignment id 集合，断言 merge 后 winner 的集合精确等于 merge 前并集，loser 的集合为空；同时验证 loser 详情页返回 404 或等价可读错误页。
- AC-8 的测试必须在网络层观察到 `POST /people/merge` 返回 `303`，随后浏览器发起 `GET /people` 并显示成功反馈。
- AC-9 的测试可以降级为 CI 可运行的更低层自动化验证，因为浏览器/E2E 无法稳定制造事务中途故障；但它必须针对真实 scanned workspace、真实 SQLite 和真实 merge request handling/service 路径执行确定性 fault injection，而不是直接写 SQL、fake transaction manager 或伪造回滚结果。测试必须证明失败后 winner/loser 的 DB 状态、merge operation 记录数以及随后真实 `GET /people`/详情请求能观察到的可见结果都与 merge 前一致；同时还要断言故障返回的响应或落地错误页里存在明确、可读的错误反馈。
- merge 类 Playwright 验收默认以 DOM/HTTP/DB/artifact 断言为准；服务日志和必要 JSON 指标报告按调试需要保留到 `.tmp/people-gallery-merge-undo/`。页面截图只在视觉或布局不确定、需要人工复核、用户明确要求，或正在排查视觉回归时保留。
- 测试不得通过直接改库修改 `person.status`、批量更新 `person_face_assignments.person_id`、伪造 merge 成功 cookie、或绕过 HTTP 表单满足核心行为。

### Done When

- 所有验收标准都通过自动化验证。
- 合并成功后，winner 成为后续 WebUI、导出和在线 assignment 共享的 canonical person，而不是展示层 alias。
- `docs/db_schema.md` 已同步描述 merge operation、snapshot、可撤销判定和 `person.status='inactive'` 的新增契约。
- 没有核心需求通过直接状态修改、硬编码结果、假页面、fake merge、只改展示层关系或 fake integration 满足。

## Feature Slice 2: 最近一次撤销

- [ ] Implementation status: Not done

### Behavior

- 人物首页提供“撤销最近一次合并”入口；它只针对最近一次成功的 two-person merge operation。
- 只有当最近一次成功 merge 的 winner/loser 之后尚未发生新的“真实人物相关写入”时，撤销入口才可执行。这里的真实人物相关写入至少包括：新的 active assignment 写入到 winner/loser、对 winner/loser 命名/重命名、再次合并 winner/loser、排除 winner/loser 样本，或等价改变这次 merge winner/loser 真相的写入。
- 当当前没有可撤销 merge 时，首页可以把该入口禁用或隐藏；但如果客户端仍直接向公共入口 `POST /people/merge/undo` 发起 crafted 请求，服务端必须返回明确、可读的错误反馈，且 DB 不改变。
- 撤销成功后，winner 和 loser 的 `person.status`、`display_name`、`is_named`、active assignment 归属、首页展示集合和详情页样本集合都恢复到该 merge 前的可观察状态。
- 撤销成功后，被恢复的 loser 重新变为 `active` 并重新出现在首页；winner 的样本数与详情页集合回到 merge 前状态。
- 撤销必须标记该 merge operation 已撤销，并且同一 merge operation 不能被第二次撤销。
- 成功撤销后返回 `303` 到 `GET /people`，首页显示非错误成功反馈。
- 与这次 merge 无关的第三个人物发生真实写入时，不得让这次最近一次 merge 的撤销资格失效；不会产生持久化变化的 no-op 请求也不得让撤销资格失效。

### Public Interface

- 页面：`GET /people`
- 页面表单：`POST /people/merge/undo`
- 成功返回：必须是 `303` 到 `GET /people`
- DB：merge operation 真相表及其撤销状态字段、person、person_face_assignments

### Error and Boundary Cases

- 没有任何成功 merge、最近一次成功 merge 已撤销、最近一次成功 merge 的 winner/loser 之后已有新的真实人物相关写入，或 merge operation 快照不完整时，撤销失败并显示可读错误，DB 不改变；页面层可以选择提前禁用入口，但服务端拒绝语义仍然必须成立。
- 撤销必须事务一致；不得出现部分 person 恢复、部分 assignment 回滚、或 winner/loser 首页状态与 DB 不一致。
- 重复点击撤销、并发撤销、或在新的 merge 成功后撤销更旧 merge，必须被拒绝或幂等失败，不能生成第二次回滚。

### Non-goals

- 不支持撤销不是“最近一次成功 merge”的更早历史 merge。
- 不支持在 merge 之后已经发生新的 assignment/命名/合并/排除写入时，做复杂三方重放或冲突解决。
- 不支持保留 loser 继续接收新 assignment，再用撤销重新分流这些新样本。

### Acceptance Criteria

- AC-10：在一次成功 merge 后、且该 merge 的 winner/loser 尚未发生新的真实人物相关写入时，点击首页“撤销最近一次合并”，人物首页和 DB 中的 person 状态、名称、active assignment owner、winner/loser 详情页样本集合都恢复到 merge 前快照。
- AC-11：成功撤销必须走 `POST /people/merge/undo -> 303 -> GET /people`，并在首页显示非错误成功反馈；不得返回 `200` 直接重渲染 POST 响应。
- AC-12：没有可撤销 merge 时，首页撤销入口可以禁用或隐藏；但如果客户端仍直接向 `POST /people/merge/undo` 发起 crafted 请求，服务端必须返回明确、可读的错误反馈，且 DB 不改变。
- AC-13：在一次成功 merge 之后，如果通过 Slice D 的真实名称写入路径 `POST /people/{person_id}/name` 对一个与这次 merge 无关的第三个 active person 成功产生新的名称写入，这次最近一次 merge 的撤销资格仍然有效；随后执行“撤销最近一次合并”仍然必须成功，并恢复该 merge 的 winner/loser 到 merge 前快照。
- AC-14：在 AC-3 场景中，merge 后已经通过把 `tests/fixtures/people_gallery_scan_2/` 作为第二个 source 并执行新的 `scan start`，向该 merge 的 winner 产生了新的 active assignment 写入，此时再触发“撤销最近一次合并”必须失败，并显示明确错误说明该 merge 之后已发生新的人物相关写入；person、assignment 和 merge operation 状态保持不变。
- AC-15：在一次成功 merge 之后，如果再通过 Slice D 的真实名称写入路径 `POST /people/{person_id}/name` 对 merge winner 成功产生新的名称写入，则“撤销最近一次合并”必须失败，并显示明确错误说明该 merge 之后已发生新的人物相关写入；这条验收至少覆盖两种真实写入子情形：匿名 winner 的首次命名，以及已命名 winner 的再次重命名。同时还必须覆盖一个 no-op 子情形：对 merge winner 提交与当前 `display_name` 等价的同名请求时，由于没有新的真实人物写入，这次 merge 的撤销资格仍然保持有效。
- AC-16：在同一 workspace 中连续完成两次成功的 two-person merge（`merge-1` 后再 `merge-2`）时，第一次执行“撤销最近一次合并”必须只回滚 `merge-2`，而保留 `merge-1` 的 merge 结果；随后首页撤销入口可以进入禁用/隐藏状态，但如果客户端仍再次向 `POST /people/merge/undo` 发起 crafted 请求，服务端必须返回明确、可读的错误反馈，证明更早的 `merge-1` 不能在 `merge-2` 已撤销后再被回滚。
- AC-17：当 undo 请求在 assignment 回滚、loser 恢复为 `active` 或 merge operation 标记为已撤销的过程中遭遇可重复故障注入并最终失败时，系统必须整体回滚：merge operation 仍保持“未撤销”，winner/loser 的 `person.status`、`display_name`、active assignment owner 和首页/详情可观察结果都与 undo 尝试前完全一致；不得出现部分 assignment 已回滚、loser 已恢复但 merge operation 未同步、或其它半完成状态。失败响应或落地错误页还必须包含明确、可读的错误反馈，不能退化为裸 `500`、空白页或通用异常页。
- AC-18：当两个针对同一最近一次 merge 的 `POST /people/merge/undo` 请求发生真实重叠并竞争同一个撤销目标时，系统最多只允许一次真实回滚生效；另一个请求必须被拒绝或以幂等失败结束。最终 DB、merge operation 状态和首页/详情可观察结果必须与“只成功执行一次 undo”完全一致，不得出现双回滚、部分回滚或 winner/loser 状态互相覆盖。
- AC-19：当最近一次 merge 的 snapshot/关联账本不完整时，`POST /people/merge/undo` 必须失败并返回明确、可读的错误反馈；DB 不改变，winner/loser 的 `person.status`、`display_name`、active assignment owner 和首页/详情可观察结果都保持与 undo 尝试前一致，不得退化为裸 `500`、空白页或通用异常页。

### Automated Verification

- AC-10 到 AC-16 必须通过真实首页撤销入口和真实 `POST /people/merge/undo` 触发，同时读取真实 `library.db` 验证 person、assignment 和 merge operation 状态。AC-17 到 AC-19 允许降级为经过真实 HTTP/request handling/service 路径的服务级自动化验证，但仍必须读取真实 `library.db` 与后续真实页面结果验证最终状态。
- AC-10 的测试必须在 merge 前先记录完整 DB 快照：至少包括 winner/loser 的 `person` 行、active assignment owner 映射和首页应展示 person 集合；撤销后验证这些快照精确恢复。
- AC-11 的测试必须在网络层观察到 `POST /people/merge/undo` 返回 `303`，随后浏览器发起 `GET /people` 并显示成功反馈。
- AC-12 的测试必须覆盖“从未 merge 过”和“最近一次 merge 已经撤销”两种情形；每种情形都要同时验证首页入口的禁用/隐藏状态，以及对 crafted `POST /people/merge/undo` 的明确、可读错误反馈。
- AC-13 的测试必须在独立 workspace 中先完成一次可撤销 merge，再通过 Slice D 的真实命名路径成功重命名一个与这次 merge 无关的第三个 active person，并证明这不会让最近一次 merge 的撤销资格失效；随后真实执行 `POST /people/merge/undo` 仍必须成功。
- AC-14 的测试必须复用 AC-3 中把 `tests/fixtures/people_gallery_scan_2/` 挂为第二个 source 的真实增量扫描场景，证明这批新增 active assignment 写入到 merge winner 后撤销被拒绝；拒绝理由必须能从页面或可读错误中观察到，不能仅靠内部日志断言。
- AC-15 的测试必须复用 Slice D 的真实 `POST /people/{person_id}/name` 路径，至少覆盖“匿名 winner 首次命名后 undo 被拒绝”“已命名 winner 再次重命名后 undo 被拒绝”“已命名 winner 提交同名 no-op 后 undo 仍可成功”三种子情形；测试必须分别证明真实写入会让撤销失效，而 no-op 不会，且 merge operation 的撤销状态变化与页面结果一致。
- AC-16 的测试必须在同一 workspace 中完成“merge-1 -> merge-2 -> undo 只回滚 merge-2 -> crafted 再次 POST /people/merge/undo 失败”的主路径，并通过真实 DB 证明 `merge-1` 的 winner/loser 关系仍然保留、`merge-2` 的 loser 已恢复、首页撤销入口进入禁用/隐藏状态，且更早的 `merge-1` 没有被第二次 undo 误回滚。
- AC-17 的测试可以降级为 CI 可运行的更低层自动化验证，因为浏览器/E2E 无法稳定制造事务中途故障；但它必须针对真实 merge snapshot、真实 SQLite 和真实 undo request handling/service 路径执行确定性 fault injection，而不是直接写 SQL、fake transaction manager 或伪造回滚结果。测试必须证明失败后 merge operation 仍保持未撤销，winner/loser 的 DB 状态保持不变，且随后在去掉故障注入后仍可通过真实 `POST /people/merge/undo` 成功完成撤销；同时还要断言故障返回的响应或落地错误页里存在明确、可读的错误反馈。
- AC-18 的测试必须使用 CI 可运行的服务级集成测试，对同一 workspace、同一 merge target 发起两个真实重叠的 `POST /people/merge/undo` 请求；测试可以通过同步栅栏、确定性故障注入或等价机制稳定制造请求重叠，但必须经过真实 HTTP/request handling/service 路径，而不是直接并发调用内部事务函数。测试必须证明最多只有一个请求完成真实回滚，另一个请求被拒绝或幂等失败，且最终 DB 与后续真实 `GET /people`/详情页结果都与“单次成功 undo”一致。
- AC-19 的测试可以降级为 CI 可运行的服务级自动化验证，因为产品没有公开入口去制造损坏 snapshot；测试可以先通过真实 successful merge 生成账本，再用仅限测试环境的受控 precondition helper 或等价 fixture 制造“不完整 snapshot”状态，然后通过真实 `POST /people/merge/undo` 验证该分支。测试必须证明服务返回明确、可读的错误反馈，DB 与首页/详情最终状态保持不变，且不会退化为裸 `500`、空白页或通用异常页。
- 测试不得通过直接回填 merge snapshot、直接更新 `person.status`、直接批量改 `person_face_assignments.person_id` 或伪造“已撤销”状态满足验收。

### Done When

- 所有验收标准都通过自动化验证。
- 最近一次撤销的边界与“合并后已有新人物相关写入则拒绝”语义可以通过真实页面和真实 DB 稳定复现。
- `docs/db_schema.md` 已同步描述 merge operation 的撤销状态和不可撤销判定语义。
- 没有核心需求通过直接状态修改、硬编码成功、假撤销或 fake integration 满足。
