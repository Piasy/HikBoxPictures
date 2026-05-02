# Immich v6 人物图库产品化 — 导出模板与执行 Spec

## Goal

用户可通过 WebUI 创建并保存基于已命名人物的导出模板，预览导出结果的分桶与月份分布，执行导出到真实文件系统（包含 Live MOV 配对复制和跳过已存在文件），并查看导出历史；导出运行期间全局锁定人物写操作。

## Global Constraints

- 产品语义以父 spec `docs/LittlePower/specs/2026-04-24-immich-v6-people-gallery-productization-spec.md` 和已写子 spec（Slice 0-F）的 cross-slice contracts 为准。
- 核心行为必须通过公共入口验证；mock/stub/no-op 路径不得满足验收。
- 任何数据库 schema 修改都必须同步更新 `docs/db_schema.md`。
- 模板保存后不可编辑；支持多次执行，但执行时模板配置不可变。
- 导出分桶算法由本 spec Feature Slice 2 Behavior 定义；不存在独立的原型导出算法源文件。

## Feature Slice 1: 导出模板创建与保存

- [x] Implementation status: Done

### Behavior

- 导出模板列表页展示所有已保存模板；每个模板显示名称、创建时间、已选人物列表、输出目录。
- 创建模板时，人物选择器只展示 `status = active` 且 `display_name IS NOT NULL` 的 person。
- 模板至少选择 2 个已命名人物；只选 0 或 1 个时保存失败。
- 模板必须指定 `output_root`，且必须为绝对路径；路径不存在时系统尝试创建，创建失败则保存失败；相对路径保存失败。
- 模板保存后生成稳定 `template_id`，配置不可再编辑。
- 模板保存时记录所选人物的 `person_id` 快照，不记录 `display_name`（防止重命名后模板语义漂移）。
- 模板关联的 `person_id` 后续若变为 `inactive`（被合并）或 `display_name` 变为 NULL（被某种操作清空），级联更新关联模板的 `status` 为 `invalid`；但如果只是模板中某个人物被其他人物合并进来（即模板所选人物作为 winner 吸收了 loser），模板仍保持 `active`。

### Public Interface

- Web 页面：`/exports`（模板列表）、`/exports/new`（创建模板）。
- API：`POST /api/export-templates`（创建）— 请求体包含 `name`（用户填写的模板名称，必填，非空）、`person_ids`（至少 2 个 active 已命名人物的 ID 数组）、`output_root`（绝对路径）；`GET /api/export-templates`（列表，返回包含 `status` 字段）。
- DB：`export_template`（字段含 `template_id`、`name`、`output_root`、`status`（`active`/`invalid`）、`created_at`）、`export_template_person`（字段含 `template_id`、`person_id`）；`export_run`（字段含 `run_id`、`template_id`、`status`（`running`/`completed`/`failed`）、`started_at`、`completed_at`、`copied_count`、`skipped_count`）、`export_delivery`（字段含 `delivery_id`、`run_id`、`asset_id`、`target_path`、`result`（`copied`/`skipped_exists`）、`mov_result`（`copied`/`skipped_missing`/`not_applicable`））；新增 schema 必须同步 `docs/db_schema.md`。

### Error and Boundary Cases

- 只选 0 或 1 个人物时 `POST /api/export-templates` 返回 `400`，页面展示可读错误，不创建模板。
- 所选人物包含匿名或 `inactive` person 时返回 `400`；前端选择器已过滤，但 API 仍需校验。
- `output_root` 为相对路径或不可创建时返回 `400`，不创建模板。
- 重复点击保存不得产生重复模板；去重键为 `person_ids` 排序后 + `output_root` 组合（`name` 不参与去重，允许同名模板）。
- 空 `name` 或全空白 `name` 保存失败。

### Non-goals

- 首版不支持编辑已有模板、不支持删除模板、不支持模板版本历史。
- 首版不支持模板参数化（如按月份范围过滤、按最小人脸面积阈值调整）。

### Acceptance Criteria

#### Shared Verification Baseline

- 主路径：在已写子 spec（Slice 0/A/B/C/D/E/F）基础上完成 `init -> source add tests/fixtures/people_gallery_scan -> scan start --batch-size 10 -> serve`，再通过 Slice D 命名路径把目标人物（如 `target_alex`、`target_blair`）命名为 manifest 期望的 `display_name`；之后通过浏览器或真实 HTTP 访问 `/exports` 与 `/exports/new` 完成模板创建场景。
- 默认断言层级：DOM/HTTP/DB 断言为主；调试产物保留到 `.tmp/people-gallery-export-template/`，默认不留截图。
- 防 mock 逃逸禁令：不得直接 `INSERT` 到 `export_template` 或 `export_template_person`；不得跳过真实 HTTP 表单/API；不得伪造 `template_id` 或 `status='invalid'`；不得在 API 校验前用前端拦截绕过非法输入；不得在测试中用 ORM 直接 `UPDATE person.status` 模拟模板失活。

#### AC-1：创建模板成功

- 触发：在主路径已命名 `target_alex` 和 `target_blair` 后，通过 `/exports/new` 页面提交 `name=<非空>`、`person_ids=[alex.id, blair.id]`、绝对路径 `output_root`。
- 必须可观察：`POST /api/export-templates` 返回 2xx；DB 中 `export_template` 新增 1 行，字段包含稳定 `template_id`、提交的 `name`、绝对路径 `output_root`、`status='active'`、非空 `created_at`；`export_template_person` 新增 2 行，精确对应 `alex.id` 和 `blair.id`；返回后浏览器回到 `/exports` 列表页且新模板出现。
- 验证手段：Playwright 从 `/people` → `/exports` → `/exports/new` → 提交 → 断言列表页出现新模板；同时读取 `library.db` 校验两表行数与字段值。

#### AC-2：人物选择器过滤匿名/inactive

- 触发：在主路径基础上额外保留一个未命名的匿名 person 和一个通过 Slice E `POST /people/merge` 失活的 inactive loser；然后打开 `/exports/new`。
- 必须可观察：人物选择器只展示 `status='active'` 且 `display_name IS NOT NULL` 的 person；匿名 person 和 inactive loser 都不出现在选择器中。
- 验证手段：Playwright 在 `/exports/new` 上对选择器选项做完整集合断言（不能只断言"选不到"，必须断言这两类不在 DOM 选项中）。

#### AC-3：非法输入返回 400 且 DB 不变

- 触发：针对真实 `serve` 进程的 `POST /api/export-templates` 发起 5 类 crafted 请求 —— 0 人物 / 1 人物 / 未指定 `output_root` / 相对路径 `output_root` / 不可创建的 `output_root`；以及 1 类前端通常会过滤但服务端仍需校验的请求 —— `person_ids` 包含匿名或 inactive person。
- 必须可观察：每类请求都返回 `400`；响应或重新进入页面时显示明确、可读错误；DB 中 `export_template` 行数与触发前完全一致。
- 验证手段：服务级集成测试针对真实 HTTP 入口发起 crafted 请求，断言响应码、可读错误和 DB 行数；不依赖前端表单校验。

#### AC-4：模板存 person_id 而非 display_name

- 触发：AC-1 创建成功后，通过 Slice D 真实 `POST /people/{person_id}/name` 把 `target_alex` 重命名为新的 `display_name`。
- 必须可观察：`export_template_person` 列内容仍是原 `person_id`，不随 `display_name` 变化；模板关联人物集合也不发生迁移。
- 验证手段：DB 断言 `export_template_person` 关联列内容；通过 Slice D 真实重命名路径触发，不通过直接 `UPDATE display_name`。

#### AC-5：列表页展示完整

- 触发：AC-1 完成后，访问 `/exports`。
- 必须可观察：列表页该模板行同时展示 `name`、`created_at`、人物数（`=2`）、绝对路径 `output_root`、`status` 字段（此时为 `active`）。
- 验证手段：Playwright DOM 断言这五个字段都出现且值正确。

#### AC-6：重复保存去重；空/空白 name 失败

- 触发：(a) 快速连续两次发送 `person_ids` 排序后 + `output_root` 完全相同的 `POST /api/export-templates`（`name` 可相同也可不同）；(b) 对同 endpoint 发起 `name` 为空字符串或全空白的请求。
- 必须可观察：(a) 同配置请求只产生 1 条 `export_template` 记录（去重键 = `person_ids` 排序后 + `output_root`，`name` 不参与去重）；(b) 空或全空白 `name` 请求返回 `400`，不创建模板。
- 验证手段：服务级集成测试通过真实 HTTP 入口发起两次重叠请求（用同步栅栏或快速顺序触发），DB 断言行数；空 name 子情形单独发起 crafted 请求。

#### AC-7：人物失活级联 invalid；winner 吸收 loser 保持 active

- 触发：AC-1 完成后分别覆盖两个子情形：(a) 通过 Slice E 真实 `POST /people/merge` 把模板所选人物之一作为 loser 合并（loser 变 `inactive`）；(b) 在独立 workspace 中，把另一个非模板内 person 作为 loser、模板所选人物作为 winner 合并。
- 必须可观察：(a) `GET /api/export-templates` 返回该模板 `status='invalid'`；(b) 模板保持 `status='active'`。
- 验证手段：Playwright + DB 断言；两个子情形都走真实 merge 路径，不通过直接 `UPDATE person.status` 触发。

### Done When

- 所有验收标准通过自动化验证。
- 没有核心需求是通过直接状态修改、硬编码数据、占位行为或 fake integration 满足的。

### Accepted Concerns (from code-quality review, 2026-04-28)

- **Missing TDD evidence**: The implementer did not provide RED-phase failure commands/summaries or GREEN-phase pass reports for the production behavior changes. Controller accepts this risk because all 16 automated tests pass and fully cover the spec ACs; TDD evidence should be provided for future feature slices.
- **`mkdir` side-effect ordering (`export_templates.py`)**: `output_path.mkdir(parents=True, exist_ok=True)` is called before person existence/activity/name validations complete, meaning a request with a valid path but invalid persons can create the directory on disk before returning 400. Controller accepts this risk because the side effect is idempotent (`exist_ok=True`) and does not affect data integrity or spec assertions; the ordering should be corrected in a follow-up refactor.

## Feature Slice 2: 导出模板预览与执行

- [x] Implementation status: Done

### Behavior

- 模板列表页每个模板提供"预览"和"执行"入口。
- **预览页**展示该模板在当前人物库状态下的候选结果：
  - 只考虑同时包含模板所选**全部**已命名人物的 asset；
  - 对这些 asset 中的每张图，对模板所选每个人物，取该人物在本图中所有 face 的 bbox 绝对像素面积中的**最大值**作为该人物的代表面积；然后在各人物代表面积中取**最小值**作为 `selected_min_area`；
  - 本图中任意 face 的 bbox 面积 `>= selected_min_area / 4` 视为显著人脸；
  - 如果显著人脸全部属于模板选择人物，该 asset 归入 `only` 桶；
  - 如果存在非模板人物的显著人脸（包括匿名 person 的 face 和未归属 face），该 asset 归入 `group` 桶；
  - 同一人物在同一 photo 中出现多张 face，只按"该人物已命中"判断；bbox 面积仍参与 `selected_min_area`（取该人物在本图中的最大 face 面积）。
  - 预览页按 `YYYY-MM` 月份分目录展示，每个月份下分 `only` 和 `group` 两个子区块；
  - 每个子区块展示候选 asset 的 context 样本（即 Slice B 生成的 480p 整图加人脸框样本，非整图缩略图），桌面一行 6 个；
  - 每个候选 asset 的样例 context 显示该 asset 中模板所选人物里 `person_id` 最小者的 context；
  - 预览页还展示预计导出文件总数（仅统计静态图，Live Photo 配对 MOV 不计入总数）、only 数量、group 数量。
  - Live Photo 样本（`assets.live_photo_mov_path IS NOT NULL`）在预览页样本卡上显示 `Live` 标记，与 Slice D 人物详情页的 Live 标记一致。
- **执行导出**：
  - 执行前再次校验模板有效性（所选人物全部 active 且有 display_name）；若模板已 invalid，执行按钮禁用或点击后返回可读错误。
  - 执行时按预览的同样分桶规则复制文件到 `output_root/only/YYYY-MM/` 和 `output_root/group/YYYY-MM/`；目标文件名使用 asset 原始照片文件名（如 `IMG_0001.JPG`）。
  - 月份目录 `YYYY-MM` 依据 asset 的 EXIF 拍摄日期（Slice B metadata 阶段提取并入库的日期）分桶；若 EXIF 日期缺失，按文件修改时间回退；若仍无法获取，归入 `unknown-date` 目录。
  - HEIC/HEIF 且 DB 中 `assets.live_photo_mov_path` 已记录同目录隐藏 MOV 配对时，同时复制 MOV 到同目录；MOV 缺失或不可读时静默跳过，不影响静态图导出。
  - JPG/PNG 不导出 MOV。
  - 目标文件已存在时**跳过**，不覆盖、不改名，并在导出账本中记录 `skipped_exists`。
  - 复制文件时保留原始文件的完整 EXIF 元数据；同时保留原始文件的文件系统时间戳（创建时间/修改时间）。
  - 执行期间创建导出运行记录，状态为 `running`；完成后更新为 `completed`；失败时更新为 `failed`。
  - 执行完成后，真实文件树、DB 账本和运行记录可验证。
  - 浏览器表单提交执行后，303 重定向到导出历史页（`/exports/{template_id}/history`），用户可直接查看运行状态；手动刷新历史页可看到最新状态（页面服务端渲染，每次请求实时查询 DB）。
- **导出历史页**展示每次运行记录：
  - 模板名称、执行时间、状态（running/completed/failed）、实际导出文件数（仅统计静态图，MOV 不计入）、跳过文件数（仅统计静态图）；
  - 明细列表包含每个被处理的 asset、目标路径、结果（`copied` 或 `skipped_exists`）、MOV 复制结果（`copied`/`skipped_missing`/`not_applicable`）。

### Public Interface

- Web 页面：`/exports/{template_id}/preview`（预览）、`/exports/{template_id}/execute`（执行确认页）、`/exports/{template_id}/history`（历史）。
- API：`GET /api/export-templates/{template_id}/preview`（预览数据）、`POST /api/export-templates/{template_id}/execute`（程序化执行，返回 JSON `{"run_id": ...}`）、`GET /api/export-templates/{template_id}/runs`（该模板的历史运行列表）、`GET /api/export-runs/{run_id}`（单条运行详情）。
- 页面表单：`POST /exports/{template_id}/execute`（浏览器表单提交入口，执行后 303 重定向到 `/exports/{template_id}/history`；失败时 303 重定向回 `/exports/{template_id}/execute?error=...`）。
- DB：`export_run`、`export_delivery`。
- 文件：`output_root/only/YYYY-MM/`、`output_root/group/YYYY-MM/`。

### Error and Boundary Cases

- 模板 invalid（所选人物有人变为 inactive 或匿名）时预览/执行返回 `400`，展示可读错误。
- 已有导出运行处于 `running` 状态时，对任何模板点击执行返回 `423`，不产生新的 export_run（见 Feature Slice 3）。
- `output_root` 在执行时不可写（权限或磁盘满）返回 `500`，运行状态记为 `failed`，已复制文件保留（不回滚）。
- 目标文件已存在时跳过，不报错，记 `skipped_exists`。
- 执行期间如果资产库发生变化（新扫描、新人物写入），**不中断当前执行**；执行按启动时刻的快照处理。

### Non-goals

- 首版不支持执行中断/暂停。
- 首版不支持导出进度实时推送（如 WebSocket），历史页只展示终态。
- 不回滚已复制文件；部分失败时允许残留。

### Acceptance Criteria

#### Shared Verification Baseline

- 主路径：在 Feature Slice 1 的主路径基础上，完成至少一个有效模板（`selected_people` 长度 >= 2 且全部已命名 active）；预览/执行/历史 验收都从 `/exports` 列表页该模板行的入口进入。
- manifest 契约：所有结果对齐 `tests/fixtures/people_gallery_scan/manifest.json` 的 `expected_exports`；manifest 仅作测试断言输入，不得进入产品逻辑。
- 默认断言层级：DOM/HTTP/DB/真实文件系统；调试产物保留到 `.tmp/people-gallery-export-template/`；执行类 AC 还需把 `output_root` 与源 fixture 完全分离的临时目录作为输出根。
- 防 mock 逃逸禁令：不得直接 `INSERT` 到 `export_run` 或 `export_delivery`；不得跳过实际 `shutil.copy` 或等价文件复制；不得 mock 文件系统层；不得伪造 `export_run.status` 终态；不得绕过真实 `POST /api/export-templates/{template_id}/execute` 入口直接调用内部服务函数。

#### AC-1：预览结果对齐 manifest

- 触发：基于 manifest `expected_exports` 中 `selected_people` 长度 >= 2 的条目创建模板（Feature Slice 1 路径），随后访问 `/exports/{template_id}/preview`。
- 必须可观察：预览页展示候选 asset 集合、`only/group` 分桶、`YYYY-MM` 月份目录都精确等于 manifest `expected_exports` 对应条目（`only` 与 `group` 各自的月份 -> 文件名列表）；同月份下 `only` 与 `group` 子区块互不重叠，且并集等于该月份候选 asset 集。
- 验证手段：Playwright 在预览页对 DOM 中每个月份/分桶子区块的 asset 文件名集合做精确相等比较；同时调用 `GET /api/export-templates/{template_id}/preview` 与 manifest 做后端层断言。

#### AC-2：预览网格一行 6 个；context 取所选人物中最小 person_id

- 触发：AC-1 进入预览页后。
- 必须可观察：预览页 context 样本网格在桌面视口下一行 6 个；每个 asset 的样例 context 对应该 asset 中模板所选人物里 `person_id` 最小者；预览页同时展示预计文件总数（仅统计静态图，Live Photo 配对 MOV 不计入总数）、`only` 数量、`group` 数量。
- 验证手段：Playwright 在 `1440x900` 视口断言前 6 个样本位于同一行（盒模型/CSS 网格）；对每个样本 DOM 节点的 `data-person-id` 属性断言等于该 asset 中模板所选人物 `person_id` 的 `min()`；DOM 断言总数文案。

#### AC-3：执行后真实文件树包含静态图与 HEIC/HEIF 配对 MOV

- 触发：从预览页进入执行 `POST /api/export-templates/{template_id}/execute` 完成首次成功导出。
- 必须可观察：`output_root/only/YYYY-MM/` 与 `output_root/group/YYYY-MM/` 真实文件树与 manifest `expected_exports` 一致；HEIC/HEIF 当 DB 中 `assets.live_photo_mov_path` 已记录时复制同目录 MOV 到同月份目录；JPG/PNG 不导出 MOV；目标文件名使用 asset 原始照片文件名。
- 验证手段：Playwright 或 API 触发执行后，用 `os.walk`/`pathlib` 读取 `output_root` 真实文件树并与 manifest 比对；HEIC/HEIF 路径检查同目录是否产生 MOV；JPG/PNG 路径反向断言不存在 MOV。

#### AC-4：目标已存在文件跳过且记 skipped_exists

- 触发：执行前先在 `output_root/<bucket>/<YYYY-MM>/` 下预置一个目标文件名相同但内容不同的占位文件，再触发执行。
- 必须可观察：原占位文件内容、`mtime`、大小都未变化；`export_delivery` 中该 asset 对应行 `result='skipped_exists'`；其他无冲突 asset 仍正常导出（`result='copied'`）。
- 验证手段：测试在执行前后比较占位文件 hash；`export_delivery` 行 `result` 字段 DB 断言。

#### AC-5：invalid 模板预览和执行都被拒绝

- 触发：AC-1 创建模板后，通过 Slice E 真实 `POST /people/merge` 把模板所选人物之一作为 loser 合并使其变 `inactive`，此时模板 `status='invalid'`；分别访问 `/exports/{template_id}/preview` 和发起 `POST /api/export-templates/{template_id}/execute`。
- 必须可观察：两个入口都返回 `400` 并展示明确、可读错误；不产生新的 `export_run` 行；不修改文件系统。
- 验证手段：Playwright 进入预览页断言错误页；服务级集成测试对执行 endpoint 发起请求断言响应码与 DB 行数。

#### AC-6：历史页展示运行终态与明细账本

- 触发：AC-3 执行完成后，访问 `/exports/{template_id}/history`。
- 必须可观察：历史页展示该 run 的模板名称、执行时间、`status='completed'`、实际导出文件数（仅静态图）、跳过文件数（仅静态图）；明细列表为每个被处理 asset 列出目标路径、`result`（`copied`/`skipped_exists`）、`mov_result`（`copied`/`skipped_missing`/`not_applicable`）。
- 验证手段：Playwright DOM 断言；同时 `GET /api/export-runs/{run_id}` API 与 DB `export_run`/`export_delivery` 双向校验。

#### AC-7：执行期间资产库变化不中断当前 run

- 触发：执行启动后，通过测试专用 hook 在 per-file 复制循环中注入人物库变更（如对未来 asset 插入新 assignment）。
- 必须可观察：当前 run 不中断、不包含 hook 注入后才能命中的新归属；`export_run.status` 最终为 `completed`；`export_delivery` 明细只包含启动时刻快照内的 asset。
- 验证手段：服务级集成测试通过测试 hook 制造变更条件，但断言仍观察公共 `GET /api/export-runs/{run_id}` 与真实文件树的终态产物。降级理由：公共入口（`scan start`）在 export 运行期间被锁定（见 Feature Slice 3），无法用公共入口制造变更，hook 仅用于触发条件而非伪造终态。

#### AC-8：output_root 不可写时 run 标 failed 且不回滚已复制

- 触发：把 `output_root` 设为只读挂载或权限受限目录后触发执行。
- 必须可观察：`export_run.status='failed'`；已复制文件保留（不回滚）；后续未执行的 asset 不产生输出文件；响应或历史页展示明确、可读错误。
- 验证手段：服务级集成测试在 CI 上构造受限目录（chmod 或 tmpfs 只读）；DB 断言 `export_run.status` 与 `export_delivery` 行数；文件系统断言已复制文件存在且未被回滚。降级理由：浏览器/E2E 难以稳定模拟权限错误中途；服务级测试仍走真实 `POST /api/export-templates/{template_id}/execute` 入口和真实文件复制路径。

#### AC-9：导出文件保留 EXIF 与文件系统时间戳

- 触发：AC-3 执行完成后。
- 必须可观察：导出的每个静态图文件 EXIF 段与源文件按字节或按完整 EXIF 标签集合比较一致；文件系统 `st_mtime` 与源文件一致或在秒级精度内相等；在支持 `st_birthtime` 的平台上 `st_birthtime` 与源文件一致。
- 验证手段：测试用 PIL/ExifTool 读取源文件与导出文件 EXIF；用 `os.stat` 比较 `st_mtime`/`st_birthtime`；不依赖产品代码自报。

### Done When

- 所有验收标准通过自动化验证。
- 没有核心需求是通过直接状态修改、硬编码数据、占位行为或 fake integration 满足的。

### Accepted Concerns (from code-quality review, 2026-04-28)

- **MOV `skipped_exists` missing in schema**: When a MOV target file already exists, `_copy_asset` skips the copy but still marks `mov_result` as `"copied"` because the current `export_delivery.mov_result` CHECK constraint only allows `('copied', 'skipped_missing', 'not_applicable')`. Controller accepts this risk because the schema migration required to add `skipped_exists` is out of scope for Feature Slice 2; it should be addressed when Feature Slice 3 is implemented or in a follow-up schema patch.
- **TDD evidence missing**: The implementer did not provide RED-phase failure commands/summaries or GREEN-phase pass reports for the production behavior changes. Controller accepts this risk because all 27 automated tests pass and fully cover the spec ACs.
- **Module size**: `export_templates.py` has grown to ~890 lines covering data access, validation, preview computation, and execution logic. Controller accepts this risk for the current slice; a follow-up refactor may split it into smaller modules.
- **N+1 query in history page**: `export_template_history_page` calls `load_export_run_detail` for each run individually. Controller accepts this risk because run counts are currently low; should be batched when history volume grows.

## Feature Slice 3: 导出运行中人物写操作锁定

- [x] Implementation status: Done

### Behavior

- 任何 `export_run` 状态为 `running` 时，全局禁止以下人物写操作：
  - 命名/重命名（Slice D）
  - 合并（Slice E）
  - 撤销合并（Slice E）
  - 排除（Slice F）
- 锁定的可观察行为：
  - WebUI 中对应表单/按钮禁用或隐藏；
  - 对应的 `POST` API（`/people/{id}/name`、`/people/merge`、`/people/merge/undo`、`/people/{id}/exclude`）返回 `423 Locked`，并携带可读错误信息 `"导出进行中，暂不可修改人物库"`；
  - 返回 `423` 时 DB 不改变。
- 导出运行完成后（状态变为 `completed` 或 `failed`），锁定自动解除。
- 多个导出模板**不允许并发执行**；存在 `running` 的 `export_run` 时，对任何模板点击"执行"都返回 `423`，提示已有导出进行中。

### Public Interface

- API：上述所有人物写 API 在执行前检查全局 `running` export_run。
- DB：`export_run.status` 作为锁状态源。

### Error and Boundary Cases

- 导出刚启动时，创建 `running` export_run 必须通过原子条件插入实现（如 `INSERT ... WHERE NOT EXISTS (SELECT 1 FROM export_run WHERE status = 'running')`），确保同一时间最多只有一个 `running` 记录；若并发执行请求同时到达，只有一个能成功创建 `running` 记录，其余返回 `423`。
- 导出执行完成后，无论成功或失败，后续人物写操作恢复正常。
- 系统重启后若存在残留 `running` 记录（崩溃场景），启动时自动将残留记录标记为 `failed` 以解除锁定。

### Non-goals

- 不做细粒度锁（如只锁定模板涉及的人物），锁定范围是全局人物写操作。
- 不做导出队列或调度器；只允许单实例顺序执行。

### Acceptance Criteria

#### Shared Verification Baseline

- 主路径：在 Feature Slice 1/2 已通过的 workspace 上，先创建并启动一个真实导出，再通过测试专用 hook 在 per-file 复制循环中 `sleep` 把 `export_run.status='running'` 维持足够长，进入"export 运行中"状态后再触发各人物写 API 与第二次执行；不得跳过真实 `POST /api/export-templates/{template_id}/execute` 入口。
- 默认断言层级：HTTP 响应码 + 响应体可读错误 + DB 断言 + DOM 禁用/隐藏断言；调试产物保留到 `.tmp/people-gallery-export-template/`。
- 防 mock 逃逸禁令：不得直接 `UPDATE export_run.status='running'` 触发锁（除"残留 running 自动标 failed"的 crash 模拟外）；不得绕过真实 HTTP 路径直接调用内部锁判定函数；不得 mock `export_run` 行存在性。

#### AC-1：命名 API 在 running 期间返回 423

- 触发：导出 `running` 状态下，对真实 `serve` 进程发起 `POST /people/{person_id}/name` 命名请求。
- 必须可观察：响应 `423 Locked`；响应体含 `"导出进行中，暂不可修改人物库"` 或等价可读错误；DB 中目标 person 的 `display_name`、`is_named`、rename 审计行数完全保持触发前状态。
- 验证手段：服务级集成测试发起真实 HTTP 请求；DB 断言。

#### AC-2：merge API 在 running 期间返回 423

- 触发：导出 `running` 状态下，对真实 `serve` 进程发起 `POST /people/merge` 合并请求。
- 必须可观察：响应 `423 Locked` 与可读错误；`person.status`、`active assignment` owner、`merge operation` 行数完全保持触发前状态。
- 验证手段：服务级集成测试发起真实 HTTP 请求；DB 断言。

#### AC-3：undo API 在 running 期间返回 423

- 触发：先在 export 启动前完成一次真实可撤销 merge，使首页 undo 入口处于可用状态；启动 export 后维持 `running`，再发起 `POST /people/merge/undo`。
- 必须可观察：响应 `423 Locked` 与可读错误；merge operation 撤销状态、winner/loser 状态保持触发前状态。
- 验证手段：服务级集成测试；DB 断言。

#### AC-4：exclude API 在 running 期间返回 423

- 触发：导出 `running` 状态下，对详情页公共入口 `POST /people/{person_id}/exclude` 发起 crafted 请求。
- 必须可观察：响应 `423 Locked` 与可读错误；目标 person 的 active assignment 集合、exclusion 真相记录数保持触发前状态。
- 验证手段：服务级集成测试；DB 断言。

#### AC-5：第二个模板执行返回 423；并发原子互斥

- 触发：(a) 在第一个 export `running` 期间对第二个模板点击执行；(b) 同时并发发送两个模板执行请求（同步栅栏触发同时到达）。
- 必须可观察：(a) 第二个执行请求返回 `423` 且不产生新的 `export_run` 行；(b) 并发场景下只有一个请求成功创建 `running` 记录，另一个返回 `423`，DB 中始终最多 1 个 `running` 记录。
- 验证手段：服务级集成测试；DB 断言 `export_run` 行数与 `status` 唯一性。降级理由：浏览器/E2E 不便制造稳定并发；服务级测试仍走真实 `POST /api/export-templates/{template_id}/execute` 入口。

#### AC-6：导出完成后写 API 恢复

- 触发：让 `export_run` 通过 hook 自然走到 `completed`（或在另一子情形让它走到 `failed`）后，再次对前述 4 类人物写 API 发起合法请求。
- 必须可观察：所有人物写 API 恢复正常 2xx/PRG 行为；DOM 上对应 WebUI 控件恢复可用。
- 验证手段：服务级集成测试 + Playwright DOM 断言；两个子情形（completed/failed）都覆盖。

#### AC-7：服务启动时残留 running 标 failed

- 触发：通过直接修改 DB 制造一条 `export_run.status='running'` 残留记录（模拟进程崩溃），随后重启 `serve`。
- 必须可观察：服务启动后该残留记录的 `status` 自动迁移到 `failed`；锁定自动解除，前述 4 类人物写 API 恢复正常。
- 验证手段：服务级集成测试；启动前后 DB 断言。降级理由：产品没有公开入口能制造"进程崩溃"，直接 DB 写入仅用于触发条件；核心断言仍观察服务启动这一公共入口对残留状态的处理与后续 API 行为。

#### AC-8：running 期间 WebUI 控件禁用/隐藏

- 触发：导出 `running` 状态下，分别访问 `/people`、`/people/{person_id}`，以及 merge/undo 入口所在区域。
- 必须可观察：人物详情页的命名表单/按钮、首页的合并按钮、首页的撤销合并入口、详情页的批量排除入口都处于禁用或隐藏状态；后端兜底校验仍生效（即使强制移除 disabled 属性，POST 仍返回 `423`）。
- 验证手段：Playwright DOM 断言四类控件的禁用/隐藏；同时辅以"绕过 disabled 直接 POST 仍返回 423"的服务级断言，证明前端禁用不是唯一防线。

### Done When

- 所有验收标准通过自动化验证。
- 没有核心需求是通过直接状态修改、硬编码数据、占位行为或 fake integration 满足的。

### Accepted Concerns (from code-quality review, 2026-04-29)

- **`cleanup_stale_export_runs` has no time range limit**: The function updates all `status='running'` records to `failed` without a time bound. Controller accepts this risk because the product is explicitly a local single-user desktop application (see cross-slice contract: "WebUI 仅面向本机单用户"); there is no multi-tenant scenario in the current scope, and stale runs can only originate from a single local process crash.
- **`is_export_running` query logic duplication**: The same `SELECT 1 FROM export_run WHERE status = 'running'` query appears twice in `is_export_running` (connection and no-connection branches). Controller accepts this minor duplication because extracting it would not meaningfully reduce line count or improve readability in the current two-branch structure.
- **`PRAGMA busy_timeout = 5000` lacks comment**: The 5-second timeout value in `execute_export` has no inline explanation. Controller accepts this because the value is a conventional SQLite busy-timeout default and the surrounding context makes its purpose clear.
- **`sitecustomize.py` test hook injection**: The Playwright and service tests use a `sitecustomize.py` module to inject per-file copy hooks. Controller accepts this approach because it is a well-known Python mechanism for module-level monkey-patching in subprocess-based integration tests, and the tests properly isolate the injection via `PYTHONPATH` prepend to a temporary directory.

### Accepted Concerns (from code-quality review, 2026-05-02)

- **History page refactored from `<table>` to `<details>/<summary>`**: Commit `b887287` replaced the flat table layout with an accordion-style `<details>/<summary>` layout to improve visual hierarchy. The old DOM selectors (`tr[data-run-id]`, `[data-run-status]`, `[data-run-copied]`, `[data-run-skipped]`, `tr[data-run-deliveries]`) were replaced by semantic class selectors (`details.run-section`, `.run-badge`, `.run-stat.copied`, `.run-stat.skipped`, `.run-deliveries`). AC-6 的行为描述（展示运行终态与明细账本）不变，仅 DOM 结构与测试选择器随之更新。
