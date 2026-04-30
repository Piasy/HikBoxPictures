# Immich v6 人物图库产品化 Spec

## Goal

把 `hikbox_pictures/immich_face_single_file.py` 中已验证的 Immich v6 风格在线人物归属原型，产品化为本地单机智能相册人物库：用户可以初始化本地工作区、登记源目录、可恢复扫描照片、按 v6 在线语义形成人物库、通过 WebUI 维护人物，并导出同时包含指定已命名人物的照片。

## Global Constraints

- 本文件是父 spec，只追踪整体目标、拆分边界、子 spec 链接和子 spec 进度；具体行为、验收标准和自动化验证写在对应子 spec 中。
- 产品语义以 `hikbox_pictures/immich_face_single_file.py` 和 `docs/group_pics_algo.md` 的 v6 章节为准。
- 每个子 spec 必须能独立 review、独立实现、独立验收；不得把产品/API/UI 行为留给后续实现计划发明。
- 核心行为必须通过公共入口验证，并观察真实 DB、文件、页面、日志或导出结果。
- 不允许用 mock、硬编码、no-op、直接改库或绕过公共入口的方式满足核心验收。
- 任何数据库 schema 修改都必须同步更新 `docs/db_schema.md`。

## Split Specs

当前已写入并进入追踪的子 spec 包括 Slice 0、Slice A、Slice B、Slice C、Slice D、Slice E、Slice F 和 Slice G。所有计划子 spec 已写入；后续如有新增候选拆分，补充到 `Candidate Future Split Specs`。

### Slice 0：真实验收小图库生成

- [x] Implementation status: Done
- Spec: `docs/LittlePower/specs/2026-04-24-immich-v6-people-gallery-productization-slice-0-test-gallery-spec.md`
- Scope: 生成并固化一套用于扫描、人物归属、WebUI、合并、排除和导出验收的真实小图库、manifest 和校验入口。
- Acceptance summary: 固定测试图库和 `manifest.json` 直接入库，具体照片数量、类别矩阵和验收规则只在 Slice 0 子 spec 中定义；manifest 明确 expected_person_groups 和 expected_exports，且不得作为产品逻辑输入。

### Slice A：工作区与源目录

- [x] Implementation status: Done
- Spec: `docs/LittlePower/specs/2026-04-24-immich-v6-people-gallery-productization-slice-a-workspace-source-spec.md`
- Scope: 提供从零建库的本地 workspace/external_root 基础设施；公共入口是 `hikbox-pictures init`、`hikbox-pictures source add`、`hikbox-pictures source list`。
- Acceptance summary: 用户能通过 CLI 初始化真实工作区、创建 `config.json`/`library.db`/`embedding.db`/artifact/log 目录，登记一个或多个真实 source，并通过 JSON 输出稳定列出 source。

### Slice B：可恢复扫描与人脸产物

- [x] Implementation status: Done
- Spec: `docs/LittlePower/specs/2026-04-24-immich-v6-people-gallery-productization-slice-b-scan-artifacts-spec.md`
- Scope: 在 Slice A 基础上扫描源目录照片，生成 asset、metadata、face observation、main embedding、crop/context，并支持批次级恢复；公共入口是 `hikbox-pictures scan start --workspace <path> [--batch-size <n>]`。
- Acceptance summary: 使用 Slice 0 定义的固定入库测试图库以 `--batch-size 10` 扫描后，可在 DB、embedding 库、产物目录和日志中观察完整结果；信号中断后重跑不重复处理已提交批次。

### Slice C：v6 在线人物归属

- [x] Implementation status: Done
- Spec: `docs/LittlePower/specs/2026-04-24-immich-v6-people-gallery-productization-slice-c-online-assignment-spec.md`
- Scope: 在扫描入库后按 Immich v6 在线语义创建匿名人物和 active assignment；公共入口仍是 `hikbox-pictures scan start --workspace <path> [--batch-size <n>]`。
- Acceptance summary: manifest 期望成组的人物形成匿名 person；低于阈值的 face 不进入人物库；重复扫描保持 person/assignment 幂等，并记录 `immich_v6_online_v1` 参数快照和归属摘要。

### Slice D：人物库 WebUI 浏览与命名

- [x] Implementation status: Done
- Spec: `docs/LittlePower/specs/2026-04-24-immich-v6-people-gallery-productization-slice-d-webui-naming-spec.md`
- Scope: 通过 `hikbox-pictures serve --workspace <path> [--port <port>] [--person-detail-page-size <n>]` 提供本机 WebUI，展示已命名/匿名人物和人物详情，并支持命名、重命名和 rename 审计。
- Acceptance summary: Playwright 通过真实页面验证首页分区与空状态、详情分页 `7 + 7 + 4`、桌面一行 6 个 context 样本、Live 标记、命名/重命名、rename 审计落库，以及扫描运行中 `serve` 失败。
- Accepted concern (2026-04-26, code-quality review, Feature Slice 2 / AC-16): `idx_person_unique_active_display_name` 当前只约束原始 `display_name`，没有在数据库层直接表达“trim 后唯一”。Controller 接受该风险，因为本 slice 唯一公共写入口 `POST /people/{person_id}/name` 已先执行 trim 并做应用层重名校验；若后续新增其它命名写路径或批量修复脚本，应补充归一化唯一约束或等价迁移。
- Accepted concern (2026-04-26, code-quality review, Feature Slice 2 / AC-16): PRG 成功反馈当前通过全局 cookie 传递 outcome，未绑定 `person_id`。Controller 接受该风险，因为当前 Cross-Slice Contract 已明确首版不保证多标签页一致性，当前单用户主路径行为符合 spec；若后续扩展多标签页或更复杂导航，应把反馈状态收敛到 `person_id` 或请求作用域。

### Slice E：人物合并与最近一次撤销

- [x] Implementation status: Done
- Spec: `docs/LittlePower/specs/2026-04-24-immich-v6-people-gallery-productization-slice-e-merge-undo-spec.md`
- Scope: 在人物首页执行 two-person merge，并支持撤销最近一次仍可撤销的合并；公共入口是 WebUI/API。
- Acceptance summary: two-person merge 后 loser 的 active assignment 真实迁移到 winner、loser 失效；后续新增 loser-like 样本继续归到 winner；若合并后尚未发生新的人物相关写入，则只允许撤销最近一次合并并恢复合并前可观察状态。
- Accepted concern (2026-04-26, code-quality review, Feature Slice 1): `submit_people_merge()` 在 merge 成功时会更新 winner 的 `person.updated_at`。Controller 接受该风险，因为当前 Slice E / Feature Slice 1 尚未实现 undo eligibility 判定，这个字段不会影响当前合并主路径；到后续实现“merge 后是否发生新人物相关写入”的撤销资格判断时，不得把 `person.updated_at` 当作唯一依据，应改用独立账本或等价判据。
- Accepted concern (2026-04-26, code-quality review, Feature Slice 1): 首页 merge 表单当前依赖前端脚本把 checkbox 选择同步成隐藏 `person_id` 字段，checkbox 本身没有直接携带 `name=\"person_id\"`。Controller 接受该风险，因为当前首版 WebUI 明确是本机单用户、少量原生 JS 增强路径，真实公共入口与服务端校验已通过验收；若后续增强无 JS 可用性或需要降低模板维护复杂度，应把 checkbox 提交语义收敛为原生 `name=\"person_id\"`，脚本只做增强而不承载核心提交流程。

### Slice F：误归属排除

- [x] Implementation status: Done
- Spec: `docs/LittlePower/specs/2026-04-24-immich-v6-people-gallery-productization-slice-f-exclusion-spec.md`
- Scope: 在人物详情页批量排除当前 person 下的误归属样本，持久化 exclusion 真相，并在后续 `scan start` 中阻止这些 face 回到被排除的 person；公共入口是 WebUI/API 和真实 `scan start`。
- Acceptance summary: 批量排除后 active assignment 失效、exclusion 记录落库、详情页样本移除；仅重扫 `tests/fixtures/people_gallery_scan/` 时被排除 face 保持未归属；在”命名 alex -> merge alex/blair -> 排除所有旧 blair -> 加入 `tests/fixtures/people_gallery_scan_2/`”这条路径里，旧 blair face 与新增 blair face 会重新形成 active 匿名 blair person，而不会回到 alex winner。

### Slice G：导出模板与执行

- [x] Implementation status: Feature Slice 1 (创建与保存) Done; Feature Slice 2 (预览与执行) Done; Feature Slice 3 (运行中锁定) Done
- Spec: `docs/LittlePower/specs/2026-04-24-immich-v6-people-gallery-productization-slice-g-export-template-spec.md`
- Scope: 基于已命名人物创建导出模板，预览并导出同时包含指定人物的照片，按 only/group 与月份分桶，并定义可观察导出运行态及其对命名、合并、撤销合并、排除等人物写操作的锁定；公共入口是 WebUI/API 和导出文件树。
- Acceptance summary: 模板预览与 manifest `expected_exports` 一致；执行后真实文件树、Live MOV 配对复制和导出账本可验证；导出运行中人物写操作被公共入口拒绝。

## Candidate Future Split Specs

以下候选拆分只记录产品化路线，不代表已批准或可实现的 spec。每一项都必须单独补写 `docs/LittlePower/specs/2026-04-24-immich-v6-people-gallery-productization-slice-x-<slice name>-spec.md`，包含完整行为、验收标准和自动化验证，并通过 reviewer 后，才能移动到 `Split Specs`。候选项不使用 implementation checkbox。

当前所有计划子 spec 已写入；暂无新的候选拆分。

## Cross-Slice Contracts

- CLI 只负责 `init`、`source`、`scan start`、`serve`；人物维护和导出只通过 WebUI/API 操作。
- 所有 CLI 命令都必须显式传入 `--workspace <path>`。
- WebUI 仅面向本机单用户、`localhost` 使用；首版不做账号系统、多用户协作、远程访问和多标签页一致性保障。
- `hikbox-pictures serve` 固定监听 `localhost/127.0.0.1`；首版不提供 `--host` 配置。
- WebUI 使用 FastAPI + Jinja2 服务端渲染，可用少量原生 JS 增强表单提交、局部刷新和确认弹窗；首版不引入 React/Vue 等前端框架。
- 同一 workspace 上，`hikbox-pictures scan start` 与 `hikbox-pictures serve` 完全互斥：扫描运行期间禁止启动 WebUI，WebUI 运行期间也禁止启动新的 `scan start`；任何需要二次扫描的验收都必须先结束 `serve`，扫描完成后如仍需页面断言，再重新启动 `serve`。
- 导出运行中禁止命名、合并、撤销合并、排除等人物归属写操作；这把锁的可观察运行态和自动化验收由后续 Slice G 定义。
- 页面自动化验收以 Python Playwright + pytest 为主，截图不作为默认必留产物；只有在视觉或布局不确定、需要人工复核、用户明确要求，或正在排查视觉回归时才保存到 `.tmp/<task-name>/`。JSON 报告和服务日志按调试需要保存到 `.tmp/<task-name>/`。

## 验收集 Manifest Contract

后续端到端验收使用 Slice 0 定义的固定入库真实小图库和人工标注 manifest。测试图库的照片数量、类别矩阵、文件细节和 fixture 验收规则只在 `docs/LittlePower/specs/2026-04-24-immich-v6-people-gallery-productization-slice-0-test-gallery-spec.md` 中定义；其它子 spec 只引用该共同基线，不复述图库细节。

manifest 是验收输入契约，包含：

- `people`：人物标签、期望显示名、是否期望扫描后自动形成匿名人物。
- `assets`：照片文件名、拍摄月份、应包含的人物标签、是否存在 Live MOV 配对。
- `expected_person_groups`：扫描后哪些照片或 face 应归到同一个人物标签。
- `expected_exports`：模板选择哪些人物时，哪些文件应导出到 `only/YYYY-MM` 或 `group/YYYY-MM`。
- `tolerances`：允许不计入自动断言的边界照片或边界 face，避免真实模型偶发差异阻塞核心流程验收。

manifest 不得作为实现逻辑的输入，只能作为测试断言数据。人物命名必须通过 WebUI 完成；测试可以先根据 `expected_person_groups` 在扫描结果中定位自动形成的匿名 person，再通过 Playwright 打开人物详情页提交命名表单。
