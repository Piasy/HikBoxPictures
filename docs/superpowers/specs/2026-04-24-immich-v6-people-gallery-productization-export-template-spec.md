# Immich v6 人物图库产品化 — 导出模板与执行 Spec

## Goal

用户可通过 WebUI 创建并保存基于已命名人物的导出模板，预览导出结果的分桶与月份分布，执行导出到真实文件系统（包含 Live MOV 配对复制和跳过已存在文件），并查看导出历史；导出运行期间全局锁定人物写操作。

## Global Constraints

- 产品语义以父 spec `docs/superpowers/specs/2026-04-24-immich-v6-people-gallery-productization-spec.md` 和已写子 spec（Slice 0-F）的 cross-slice contracts 为准。
- 核心行为必须通过公共入口验证；mock/stub/no-op 路径不得满足验收。
- 任何数据库 schema 修改都必须同步更新 `docs/db_schema.md`。
- 模板保存后不可编辑；支持多次执行，但执行时模板配置不可变。
- 导出分桶算法由本 spec Feature Slice 2 Behavior 定义；不存在独立的原型导出算法源文件。

## Feature Slice 1: 导出模板创建与保存

- [ ] Implementation status: Not done

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

- AC-1：创建模板选择已命名人物 Alex 和 Blair、指定绝对路径 output_root 后，DB 中 `export_template` 和 `export_template_person` 有对应记录，且 `template_id` 稳定。
- AC-2：创建模板时人物选择器不包含匿名 person 或 `inactive` person。
- AC-3：只选 0/1 个人物、未指定 output_root、`output_root` 为相对路径、或 `output_root` 不可创建时，API 返回 `400`，页面展示可读错误，DB 无新增记录。
- AC-4：模板创建后把所选人物的 `person_id` 快照存入关联表，而不是 `display_name`。
- AC-5：已保存模板在列表页展示名称、创建时间、人物数、绝对路径 output_root 和 `status`。
- AC-6：重复提交相同配置（如快速双击保存）不产生第二条 `export_template` 记录；空 `name` 或全空白 `name` 返回 `400`，不创建模板。
- AC-7：将模板中某个人物作为 loser 被合并（使其变为 `inactive`）后，列表 API 返回该模板的 `status = invalid`。

### Automated Verification

- Playwright 从人物首页导航到导出模板列表，点击新建，在人物选择器中断言只展示已命名且 active 的人物；选择人物、填写绝对路径 output_root、提交，断言页面回到列表且新模板出现。
- 直接 `POST /api/export-templates` 验证非法输入的边界（无人物、只选 1 人、匿名人物、相对路径、不可创建目录、空 name）返回 `400`，且 DB 不改变。
- DB 断言验证 `export_template_person` 存的是 `person_id` 而非 `display_name`。
- 快速连续发送两次相同 `POST /api/export-templates` 请求，断言只产生一条 `export_template` 记录。
- 创建模板后通过 WebUI 合并模板中的某个人物，再调用 `GET /api/export-templates`，断言该模板的 `status = invalid`。

### Done When

- 所有验收标准通过自动化验证。
- 没有核心需求是通过直接状态修改、硬编码数据、占位行为或 fake integration 满足的。

## Feature Slice 2: 导出模板预览与执行

- [ ] Implementation status: Not done

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
- **导出历史页**展示每次运行记录：
  - 模板名称、执行时间、状态（running/completed/failed）、实际导出文件数（仅统计静态图，MOV 不计入）、跳过文件数（仅统计静态图）；
  - 明细列表包含每个被处理的 asset、目标路径、结果（`copied` 或 `skipped_exists`）、MOV 复制结果（`copied`/`skipped_missing`/`not_applicable`）。

### Public Interface

- Web 页面：`/exports/{template_id}/preview`（预览）、`/exports/{template_id}/execute`（执行确认页）、`/exports/{template_id}/history`（历史）。
- API：`GET /api/export-templates/{template_id}/preview`（预览数据）、`POST /api/export-templates/{template_id}/execute`（执行）、`GET /api/export-templates/{template_id}/runs`（该模板的历史运行列表）、`GET /api/export-runs/{run_id}`（单条运行详情）。
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

- AC-1：按 manifest `expected_exports` 中 `selected_people` 长度 >= 2 的条目创建模板后，预览结果与预期一致（候选照片、only/group 分桶、月份目录）。manifest 结构由 Slice 0 定义，至少包含 `selected_people`（人物标签数组）、`only`（月份 -> 文件名列表）、`group`（月份 -> 文件名列表）。
- AC-2：预览页展示 context 样本网格一行 6 个，每个 asset 显示所选人物中 `person_id` 最小者的 context，并显示预计文件总数统计。
- AC-3：执行导出后，真实文件树包含预期静态图和 HEIC/HEIF 配对 MOV；JPG/PNG 不导出 MOV。
- AC-4：预先在输出目录放置同名文件后执行导出，原文件未被覆盖，账本记录 `skipped_exists`。
- AC-5：模板 invalid 时预览和执行都被拒绝，返回可读错误。
- AC-6：执行完成后历史页展示 run 状态、导出数量、跳过数量和明细账本。
- AC-7：执行导出期间，即使资产库发生变化（如另一进程直接改 DB 模拟新扫描完成），当前执行不中断，仍按启动时刻快照完成。
- AC-8：`output_root` 在执行时不可写（如通过权限控制模拟），运行状态最终记为 `failed`，已复制文件保留，未复制文件不产生。
- AC-9：执行导出后，导出的静态图文件保留与源文件一致的 EXIF 元数据；文件系统创建时间/修改时间与源文件一致（或在允许的文件系统精度误差内）。

### Automated Verification

- Playwright 完成模板创建 -> 预览：在预览页断言候选 asset 集合、only/group 分桶、月份目录与 manifest `expected_exports` 一致；断言 context 网格通过 CSS 或 DOM 结构稳定呈现为一行 6 个；对每个展示的 context 样本断言其 `data-person-id` 属性等于该 asset 中模板所选人物里 `person_id` 最小者。
- Playwright 或 API 测试从预览页进入执行：断言执行后真实文件树、DB 账本与 manifest `expected_exports` 一致。
- 通过预置同名文件验证 `skipped_exists` 行为。
- 通过将模板中某个人物作为 loser 被合并（变为 `inactive`）来验证 invalid 模板拒绝执行。
- DB 断言验证 `export_run` 状态和 `export_delivery` 明细。
- AC-7 通过测试专用 hook 在执行中途注入人物库变更（如插入新 assignment），断言导出仍按原快照完成且不包含新变更。使用测试 hook 而非公共入口触发变更，是因为公共入口（如 `scan start`）在导出运行期间被锁定无法执行；该 hook 仅用于制造变更条件，核心断言仍观察公共执行入口的终态产物。
- AC-8 通过将 `output_root` 设为不可写目录（如只读挂载或权限控制）触发导出失败，断言 `export_run.status = failed` 且已复制文件数少于总数。
- AC-9 通过读取导出后的静态图文件，使用 PIL/ExifTool 等工具断言完整 EXIF 段（或全部 EXIF 标签）与源文件一致；断言文件系统 `st_mtime` 与源文件一致或在秒级精度内相等；在支持 `st_birthtime` 的平台上同时断言创建时间与源文件一致。

### Done When

- 所有验收标准通过自动化验证。
- 没有核心需求是通过直接状态修改、硬编码数据、占位行为或 fake integration 满足的。

## Feature Slice 3: 导出运行中人物写操作锁定

- [ ] Implementation status: Not done

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

- AC-1：存在 `running` export_run 时，`POST /people/{id}/name` 返回 `423`，DB 中 person 名称不变。
- AC-2：存在 `running` export_run 时，`POST /people/merge` 返回 `423`，无 merge 发生。
- AC-3：存在 `running` export_run 时，`POST /people/merge/undo` 返回 `423`，无 undo 发生。
- AC-4：存在 `running` export_run 时，`POST /people/{id}/exclude` 返回 `423`，无 exclusion 发生。
- AC-5：存在 `running` export_run 时，对第二个模板点击执行返回 `423`，不产生新的 export_run。
- AC-6：导出完成后（completed），上述人物写 API 恢复正常工作。
- AC-7：系统启动时将所有残留 `running` export_run 自动标记为 `failed`。
- AC-8：存在 `running` export_run 时，WebUI 中人物详情页的命名表单/按钮、首页的合并按钮和排除入口处于禁用或隐藏状态。

### Automated Verification

- Playwright 或 API 集成测试：创建并启动一个导出任务，通过在真实执行路径注入可控延迟（如测试专用 hook 在复制每文件前 sleep）使 `export_run` 保持 `running` 状态足够长；在导出 `running` 期间并发调用各人物写 API 和第二个模板的执行 API，断言全部返回 `423` 且 DB 不变；同时 Playwright 断言人物详情页命名按钮/首页合并按钮处于禁用或隐藏状态。
- 导出完成后再次调用人物写 API，断言恢复正常；Playwright 断言对应按钮恢复可用。
- 通过直接修改 DB 制造残留 `running` 记录后重启服务，断言记录被自动标记为 `failed`。直接修改 DB 是为了模拟系统崩溃后无法通过正常流程达到的状态；核心验证的是服务启动这一公共入口对残留状态的处理。
- 通过并发发送两个模板执行请求，断言只有一个成功创建 `running` 记录，另一个返回 `423`。

### Done When

- 所有验收标准通过自动化验证。
- 没有核心需求是通过直接状态修改、硬编码数据、占位行为或 fake integration 满足的。
