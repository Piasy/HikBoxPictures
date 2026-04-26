# Immich v6 人物图库产品化 Slice D：人物库 WebUI 浏览与命名 Spec

## Goal

在已有匿名人物和 active assignment 的 workspace 上，通过 `hikbox serve --workspace <path> [--port <port>] [--person-detail-page-size <n>]` 提供本机 WebUI，让用户浏览已命名/匿名人物、查看人物详情样本，并通过详情页完成命名或重命名；分页、Live 标记、错误反馈和 rename 审计都可通过真实页面和真实 DB 验收。

## Global Constraints

- 本 spec 是父 spec `docs/superpowers/specs/2026-04-24-immich-v6-people-gallery-productization-spec.md` 的 Slice D，只覆盖 `serve`、人物首页/详情页浏览、命名/重命名和 rename 审计。
- 本 slice 依赖 Slice 0 的固定真实小图库和 manifest、Slice A 的 workspace/source 契约、Slice B 的 asset/face/artifact 契约，以及 Slice C 已创建的匿名 person、active assignment 和 assignment run；不重新定义初始化、扫描、人脸检测或在线归属语义。
- 公共入口是 `hikbox serve --workspace <path> [--port <port>] [--person-detail-page-size <n>]`；首版不新增独立 `rename-person` CLI，也不要求公开 JSON API。
- `hikbox serve` 固定监听本机 `localhost/127.0.0.1`；本 slice 不提供 `--host` 参数，也不定义 host 相关配置或校验分支。
- `--port` 是可选参数；省略时固定监听 `8000`。自动化验收为了避免并发冲突，必须显式传入 `--port <free-port>`，不得依赖默认端口抢占成功。
- `--person-detail-page-size` 是 `serve` 的可选参数，只影响人物详情页样本分页；默认值固定为 `200`，传入值必须是正整数。
- 本 slice 的分页验收只验证显式传入 `--person-detail-page-size 7` 时的公开页面行为；不为默认值 `200` 单独增加服务级注入或分页验收逻辑。
- WebUI 只面向本机单用户、`localhost` 使用；首版不做账号系统、多用户协作、远程访问和多标签页一致性保障。
- WebUI 根路径必须可直接进入人物首页；实现可以直接渲染人物首页，也可以把 `/` 重定向到 `/people`，但用户不需要手工猜测入口。
- 存在 `running` 扫描会话时，`hikbox serve` 必须失败退出且不监听端口；不得提供“边扫描边命名”的并发写路径。
- 本 slice 只定义人物首页、人物详情页和命名表单的产品语义；不定义合并、撤销合并、排除、导出模板、导出历史、运行日志页或源目录管理页的可操作语义。
- manifest 只允许作为自动化断言输入；产品运行、页面渲染、命名和 Live 标记逻辑不得读取 manifest。
- 页面自动化验收统一使用 Chromium；这是当前用户明确确认后的浏览器基线，也与 `python3 -m playwright install chromium` 和 CI 安装路径保持一致；仍需遵循仓库现有 Playwright 入口约定。
- 核心行为必须通过真实 `hikbox serve` 进程、真实 HTML 页面、真实表单提交、真实 SQLite 和真实日志验证；mock/stub/no-op、直接改库、硬编码页面结果、跳过 HTTP 服务或让测试直接调用内部模板函数不得满足验收。
- 如果实现引入或调整 `person.display_name`、`person.is_named`、rename 审计真相表或相关 schema，必须同步更新 `docs/db_schema.md`。

## Feature Slice 1: 人物库浏览

- [x] Implementation status: Done

### Behavior

- `hikbox serve --workspace <path> [--port <port>] [--person-detail-page-size <n>]` 启动本机 WebUI；服务固定监听 `localhost/127.0.0.1`，默认 `--port=8000`、`--person-detail-page-size=200`。
- 人物首页必须展示两个视觉区块：`已命名人物` 和 `匿名人物`。
- 首页只展示 `status='active'` 且仍有 active assignment 的 person。匿名区只展示已创建但尚未命名的 person；未归属 face 不出现在首页。
- 每张人物卡至少展示：代表 context 图、当前显示名或稳定匿名标识、active assignment 样本数、进入详情页的可点击入口。已命名人物卡的可见名称必须等于该 person 的 `display_name`；匿名人物卡必须展示非空、跨刷新稳定的匿名标识，并且同一 person 在 `/`、`/people` 和重复刷新之间保持一致。
- 人物详情页展示当前 person 的 active assignment 样本；每张样本卡只显示对应样本的 context 图。
- Live Photo 样本在人物详情页样本卡上显示 `Live` 标记；是否显示只由真实 `assets.live_photo_mov_path` 或等价持久化事实决定，不得依赖文件名猜测。
- 人物详情页按 `--person-detail-page-size` 分页。页大小只影响详情页，不影响首页卡片数量。
- 首页必须通过人物卡片的真实点击入口进入详情页，不能要求用户手工拼接 `person_id` URL。
- 详情页的当前页必须反映在可刷新、可回放的稳定 URL 中；刷新该 URL 后仍显示同一页样本，而不是退回第一页或丢失分页状态。
- 当前桌面验收视口固定为 `1440x900`；在这个视口下，人物详情页的样本网格一行必须展示 6 个样本，第 7 个样本换到下一行。
- 页面必须暴露稳定的自动化定位标识，例如 `data-person-id`、`data-asset-id`、`data-assignment-id`、当前页码和总页数字段，便于 Playwright 和 DB 结果对齐。

### Public Interface

- CLI：`hikbox serve --workspace <path> [--port <port>] [--person-detail-page-size <n>]`
- 页面：`GET /`、`GET /people`、`GET /people/{person_id}`
- 详情页分页：通过页面上的分页入口在多页之间切换；实现可以使用查询参数或等价 URL 形式，但对用户必须是可直接刷新和可回放的稳定 URL。
- 页面中的代表图和详情页样本图都必须通过浏览器可访问的真实图片资源 URL 提供；自动化可以直接请求这些 URL，并与底层 artifact 文件对齐。
- DB：`person`、active assignment 真相表、`assets.live_photo_mov_path`、`face_observations.context_path`

### Error and Boundary Cases

- workspace 未初始化、缺少 WebUI 依赖的 schema、`--person-detail-page-size < 1` 或端口被占用时，`hikbox serve` 返回非 0 和可读错误。
- 存在 `running` 扫描会话时，`hikbox serve` 返回非 0，且目标端口不被监听。
- `person_id` 不存在时，详情页返回 404 或等价可读错误页，而不是 500。
- 首页没有任何可展示人物时，显示 empty state，不返回 500。

### Non-goals

- 不做搜索、筛选、虚拟滚动、待复核页、Identity Run 证据页或移动端专项适配。
- 不做合并、撤销、排除、导出模板和导出历史的可操作入口。
- 不做点击样本后展示 crop/context 双图的详情预览。
- 不要求首页卡片排序或封面选择做复杂“最佳样本”策略；只要求行为稳定、可回放、可自动化定位。

### Acceptance Criteria

- AC-1：通过访问 `GET /` 和 `GET /people` 都能到达同一人物首页语义；用户无需手工猜测入口。首页能看到 `已命名人物` 和 `匿名人物` 两个区块，人物卡片至少展示代表 context 图、当前名称或匿名标识、样本数，并且能通过卡片上的真实点击入口进入详情页；首页展示的人物卡集合必须与真实 DB 中“`status='active'` 且存在 active assignment 的 person 集合”一致，不得混入未归属 face 或其他非 person 卡片。每张已命名人物卡的可见名称必须等于 DB `display_name`，每张匿名人物卡都必须显示非空稳定匿名标识；首页代表图必须对应到该 person 某个 active assignment 的真实 `context_path`，不得使用占位图、空白图或与该 person 无关的图片。
- AC-2：基于当前 Slice 0 固定图库，`manifest.json` 的 `expected_person_groups` 中 `target_alex`、`target_blair`、`target_casey` 每个目标人物当前都关联 18 个 asset；在 `--person-detail-page-size 7` 下，这三个人物的详情页都必须稳定分页为 `7 + 7 + 4` 共 3 页。把三页上所有 `data-assignment-id` 汇总后，得到的集合必须与该 person 在 DB 中的 active assignment 集合完全相等，且不允许重复、漏项或混入其它 person 的 assignment。
- AC-3：在 AC-2 的同一验收中，任一目标人物详情页第一页在 `1440x900` 视口下的样本网格第一行必须正好展示 6 个样本，第 7 个样本必须换到下一行。
- AC-4：人物详情页样本卡只显示对应样本的 context 图，且页面中展示出来的样本图必须与该样本对应 `face_observations.context_path` 的真实 artifact 内容一致。切到第 2 页后，当前分页状态必须体现在稳定 URL 中；直接刷新该 URL 后，仍展示第 2 页对应的 7 个样本，而不是退回第一页。
- AC-5：人物详情页中每个样本卡的 `Live` 标记必须与该样本对应 `assets.live_photo_mov_path` 的真实持久化值一一对应：`live_photo_mov_path IS NOT NULL` 的样本显示 `Live`，`live_photo_mov_path IS NULL` 的样本不得显示 `Live`。在当前 Slice 0 固定图库中，这意味着 `target_alex` 关联的 `asset_047` 和 `target_casey` 关联的 `asset_048` 显示 `Live`；`asset_049`、`asset_050` 不显示 `Live`；除 `asset_047`、`asset_048` 外，其他样本也都不得显示 `Live`。
- AC-6：`hikbox serve --workspace <missing> --port <free-port>`、`hikbox serve --workspace <valid-workspace> --port <free-port> --person-detail-page-size 0`、“目标端口已被占用”和“workspace 缺少 WebUI 依赖的 schema”这四类场景都必须返回非 0 和可读错误，且不得留下监听中的服务端口。
- AC-7：存在 `running` 扫描会话时，`hikbox serve` 返回非 0，且目标端口未被监听。
- AC-8：在一个已完成初始化、已添加 Slice 0 固定图库 source、但尚未执行 `scan start` 的真实 workspace 中访问 `GET /`，首页显示 empty state，而不是 500 或空白页。
- AC-9：`person_id` 不存在时，详情页返回 404 或等价可读错误页，而不是 500。

### Automated Verification

- 新增 Playwright 端到端测试，例如 `tests/people_gallery/test_webui_people_gallery_playwright.py`，通过真实 CLI 流程 `hikbox init --workspace <workspace> -> hikbox source add --workspace <workspace> -> hikbox scan start --workspace <workspace> -> hikbox serve --workspace <workspace> --port <free-port> --person-detail-page-size 7` 启动服务，再使用 Chromium 打开真实页面。
- Playwright 必须分别通过 `GET /` 和 `GET /people` 进入首页，再从首页卡片的真实点击入口进入详情页；不得只通过手工拼接 `person_id` URL 完成主路径验收。
- Playwright 必须根据 manifest 的 `expected_person_groups` 和真实 DB 结果定位 `target_alex`、`target_blair`、`target_casey` 对应 person，再断言三个人物详情页样本数都为 18、分页精确为三页，且三页样本数分别为 7、7、4。
- AC-1 的测试必须从真实 `library.db` 读取首页应展示的 active person 集合和样本数，再逐一对齐首页 DOM：每张人物卡都必须有代表 context 图、样本数文本、非空可见名称或匿名标识，以及对应 `data-person-id`，且首页不得多出任何不在该集合中的卡片。对于每张首页卡片，测试还必须抓取其代表图 URL，并验证图片内容与该 person 某个 active assignment 的真实 `context_path` 文件一致。匿名人物卡的可见匿名标识还必须在 `/`、`/people` 和页面刷新后保持一致。
- AC-2 的测试还必须汇总三页中出现的全部 `data-assignment-id`，并与真实 DB 中该 person 的 active assignment 集合做精确相等校验，证明没有重复、漏项或串入其它 person 样本。
- AC-3 的测试必须在 `1440x900` 桌面视口下读取真实 DOM 布局或盒模型位置，证明前 6 个样本位于同一行、第 7 个样本换行，而不是只断言存在 7 个卡片。
- AC-4 的测试必须断言详情页样本卡只展示 context 图，不展示额外 crop 预览。测试还必须根据样本的 `data-assignment-id` 读取真实 `face_observations.context_path`，抓取页面中对应样本图的 URL 并校验其内容与 artifact 文件一致。测试还必须切换到第 2 页并刷新当前 URL，确认仍显示第 2 页对应的 7 个样本。
- AC-5 的测试必须分别进入 `target_alex`、`target_blair`、`target_casey` 的详情页，遍历该人物的全部分页，汇总所有已渲染样本卡的 `data-asset-id`，再与真实 `library.db.assets.live_photo_mov_path` 逐条对齐，断言跨所有分页出现的每张样本卡 `Live` 标记都与 DB 真相一致。当前固定图库里，还必须额外点名验证 `asset_047`、`asset_048` 显示 `Live`，`asset_049`、`asset_050` 不显示 `Live`，以及 `target_blair` 详情页中的所有样本都不得显示 `Live`。
- 主线 Playwright 测试必须始终显式传入 `--port <free-port>` 和 `--person-detail-page-size 7`。
- AC-6 和 AC-7 必须通过 CLI/服务级集成测试覆盖未初始化 workspace、非法 `--person-detail-page-size`、端口占用、缺少 WebUI 依赖 schema 和扫描运行中 `serve` 失败；这些测试必须验证非 0 退出码、可读 stderr 和“目标端口未监听”。
- AC-8 必须复用 Slice 0 固定图库作为 source，但不执行 `scan start`：测试执行真实 `hikbox init --workspace <workspace> -> hikbox source add --workspace <workspace> -> hikbox serve --workspace <workspace> --port <free-port>`，再断言首页 empty state。
- AC-9 必须通过真实页面或真实 HTTP 请求访问一个不存在的 `person_id` 详情页，断言返回 404 或等价可读错误页，而不是 500。
- Playwright 验收默认以 DOM/HTTP/DB/artifact 断言为准；服务日志和必要 JSON 指标报告按调试需要保留到 `.tmp/people-gallery-webui-naming/`。页面截图只在视觉或布局不确定、需要人工复核、用户明确要求，或正在排查视觉回归时保留。
- 测试不得通过直接插入 person、assignment、Live 标记或分页元数据来制造页面状态；必须让页面从真实 DB 和真实 HTTP 响应中产生这些状态。

### Done When

- 所有验收标准都通过自动化验证。
- 首页分区、详情分页、`1440x900` 视口下的一行 6 个样本布局、`Live` 标记和 `serve` 启动失败边界都能通过真实页面稳定复现。
- 没有任何核心需求通过直接状态修改、硬编码数据、假页面、跳过 HTTP 服务或 fake integration 满足。

## Feature Slice 2: 人物命名与重命名

- [ ] Implementation status: Not done

### Behavior

- 人物详情页提供命名表单，提交 `display_name` 后对当前 person 生效。
- 首次命名把 person 从匿名人物区移动到已命名人物区；再次提交新的 `display_name` 视为重命名。两种情况下 person 身份和现有 active assignment 都保持不变。
- 名称在写入前必须做首尾空白裁剪；裁剪后不能为空。
- active 的已命名人物之间，裁剪后的 `display_name` 必须唯一。当前 slice 只要求按裁剪后的完整字符串精确比较，不做大小写折叠、拼音归并或别名归并。
- 成功首次命名时，person 写入 `display_name`，并把 `is_named` 设为真；成功重命名时，更新同一 person 的 `display_name`，`is_named` 保持为真。
- 每次成功命名或重命名都必须落一条 rename 审计记录，最小字段包括：`person_id`、事件类型、旧名称、新名称、事件时间。首次命名的事件类型是 `person_named`，旧名称为 `NULL` 或等价空值；重命名的事件类型是 `person_renamed`。
- 成功命名、重命名和 no-op 都必须使用同一条 PRG 路径：`POST /people/{person_id}/name` 处理完成后返回 `302` 或 `303` 重定向，再由浏览器发起 `GET /people/{person_id}`。直接返回 `200` 并在 POST 响应里重渲染详情页不符合本 slice 契约。
- 重复提交当前已生效的同一名称，或仅在前后空白上不同但裁剪后相同的名称，视为 no-op 成功：必须沿用与成功命名相同的 PRG 返回路径回到当前详情页，并显示“名称未变化”或等价的非错误反馈；不得新增 rename 审计记录，也不得制造额外状态变化。
- 成功提交后，浏览器通过 PRG 返回当前人物详情页并显示更新后的名称；人物首页同步反映该变化。

### Public Interface

- 页面：`POST /people/{person_id}/name`
- 表单字段：`display_name`
- 成功返回：必须以 `302` 或 `303` 重定向回当前 `GET /people/{person_id}`；不允许用 `200` 直接重渲染 POST 响应替代 PRG
- DB：`person.display_name`、`person.is_named`，以及 rename 审计真相表；如果实现使用通用事件表，也必须能查询出本 slice 要求的 `person_named` 和 `person_renamed` 事件

### Error and Boundary Cases

- 裁剪后为空、与其他 active 已命名人物在裁剪后重名、或 `person_id` 不存在时，提交失败并显示可读错误，DB 不改变。
- 重复提交当前已生效名称不得新增 rename 审计，也不得写入任何 person 记录字段；`updated_at` 也必须保持不变。
- 当前 slice 不提供“清空名称恢复匿名”的能力；匿名到已命名、已命名到新名称是本 slice 唯一支持的名称写操作。

### Non-goals

- 不做人物合并、撤销合并、误归属排除、导出模板人物选择或批量重命名。
- 不做名称搜索、别名、多语言名称、大小写归一或重名冲突消歧工作流。
- 不做 rename 审计浏览页；当前只要求 rename 审计落库且可通过 DB 自动化验证。

### Acceptance Criteria

- AC-11：根据 manifest 的 `expected_person_groups` 定位一个匿名目标人物后，通过详情页提交一个带前后空白、且裁剪后唯一的临时名称，例如 `  Temporary Alex  `；提交成功后，DB 中存储值必须是裁剪后的 `Temporary Alex`，该 person 从匿名人物区移动到已命名人物区，person 身份保持不变，active assignment 不迁移；返回首页后，这个人物必须只出现在 `已命名人物` 区块，不再出现在 `匿名人物` 区块，且首页卡片名称同步为 `Temporary Alex`。
- AC-12：在 AC-11 的同一人物上再次通过详情页提交其 manifest 中对应的 `display_name`，视为重命名；提交后 DB 中仍是同一个 person，active assignment 不变，首页和详情页名称都同步更新；返回首页后，这张人物卡仍位于 `已命名人物` 区块，且名称更新为新的 `display_name`。
- AC-13：当一个目标人物已被命名为其 manifest `display_name` 后，另一个 active person 尝试提交一个仅通过前后空白变化得到的同名值，例如 `  <same-display-name>  `，必须因裁剪后重名而失败；页面显示明确错误，DB 中两个 person 的名称和 active assignment 都保持不变。
- AC-14：提交空字符串或仅空白字符必须失败，页面显示明确错误，且不产生 rename 审计记录。
- AC-15：向不存在的 `person_id` 提交命名请求必须返回可读错误或 404，且不产生 rename 审计记录。
- AC-16：成功首次命名和成功重命名各自产生一条 rename 审计记录，事件类型分别是 `person_named` 和 `person_renamed`；两条记录都必须绑定正确 `person_id`，并写入非空事件时间。成功首次命名、成功重命名和 no-op 三种成功路径的 HTTP 行为都必须走 `POST -> 302/303 -> GET /people/{person_id}`，并把浏览器带回当前详情页。重复提交同一个已生效名称，或只在前后空白上不同但裁剪后相同的名称，都必须走 no-op 成功路径：详情页显示“名称未变化”或等价非错误反馈，不新增审计记录，首页和 DB 保持不变。

### Automated Verification

- Playwright 在真实页面执行 `hikbox init --workspace <workspace> -> hikbox source add --workspace <workspace> -> hikbox scan start --workspace <workspace> -> hikbox serve --workspace <workspace> --port <free-port>`，根据 manifest 和真实 DB 定位匿名目标人物后完成首次命名、重命名、重名失败、空名失败和同名 no-op。
- 自动化必须同时读取真实 `library.db`，验证 `person.display_name`、`person.is_named`、person 身份稳定性、active assignment 未迁移，以及 rename 审计记录的数量、`person_id`、事件类型、旧名称、新名称和事件时间。
- AC-11 的测试必须断言 DB 中最终写入的是裁剪后的名称，而不是带前后空白的原始输入。
- AC-11 和 AC-12 的测试必须证明同一个 `person_id` 在两次提交后保持不变，而不是通过新建 person 或迁移 assignment 伪装出“重命名成功”；并且两次提交都必须在网络层观察到 `POST -> 302/303 -> GET /people/{person_id}`，随后回到首页断言人物卡在正确区块、显示正确名称、且详情页与首页同步。
- AC-13、AC-14 和 AC-15 的失败测试必须断言页面可读错误、DB 状态不变、rename 审计记录数不变。
- AC-16 的 no-op 测试必须同时覆盖“提交完全相同名称”和“只在前后空白上不同、裁剪后相同的名称”两种输入，并断言两种输入都会走 no-op 成功路径：详情页显示非错误反馈、网络层出现 `POST -> 302/303 -> GET /people/{person_id}` 回到当前详情页、不会新增 `person_named` 或 `person_renamed` 审计记录，首页卡片和 DB 都保持不变，且 `person.updated_at` 不变化。
- 命名类 Playwright 验收默认以 DOM/HTTP/DB/artifact 断言为准；服务日志和必要 JSON 指标报告按调试需要保留到 `.tmp/people-gallery-webui-naming/`。页面截图只在视觉或布局不确定、需要人工复核、用户明确要求，或正在排查视觉回归时保留。
- 测试不得直接改库命名、手工插入 rename 审计或绕过 HTTP 表单；必须通过真实页面提交完成所有状态变化。

### Done When

- 所有验收标准都通过自动化验证。
- 命名、重命名、空名失败、重名失败和同名 no-op 都有清晰、可观察、可回放的页面结果和 DB 结果。
- `docs/db_schema.md` 已同步描述本 slice 新增或确认的 `person.display_name`、`person.is_named` 和 rename 审计真相表或等价事件表。
- 没有核心需求通过直接状态修改、硬编码名称、假表单成功、伪造审计记录或 fake integration 满足。
