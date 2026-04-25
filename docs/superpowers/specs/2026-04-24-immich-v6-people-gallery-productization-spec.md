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

当前已写入并进入 review gate 的子 spec 包括 Slice A 和 Slice B。Slice C-G 属于后续候选拆分，必须在各自子 spec 写入并通过 review 后，才能加入本节的 checkbox 进度追踪。

### Slice A：工作区与源目录

- [ ] Implementation status: Not done
- Spec: `docs/superpowers/specs/2026-04-24-immich-v6-people-gallery-productization-workspace-source-spec.md`
- Scope: 提供从零建库的本地 workspace/external_root 基础设施；公共入口是 `hikbox init`、`hikbox source add`、`hikbox source list`。
- Acceptance summary: 用户能通过 CLI 初始化真实工作区、创建 `config.json`/`library.db`/`embedding.db`/artifact/log 目录，登记一个或多个真实 source，并通过 JSON 输出稳定列出 source。

### Slice B：可恢复扫描与人脸产物

- [ ] Implementation status: Not done
- Spec: `docs/superpowers/specs/2026-04-24-immich-v6-people-gallery-productization-scan-artifacts-spec.md`
- Scope: 在 Slice A 基础上扫描源目录照片，生成 asset、metadata、face observation、embedding、crop/context，并支持批次级恢复；公共入口是 `hikbox scan start --workspace <path>`。
- Acceptance summary: 真实小图库扫描后可在 DB、embedding 库、产物目录和日志中观察完整结果；失败重跑不重复处理已提交批次。

## Candidate Future Split Specs

以下候选拆分只记录产品化路线，不代表已批准或可实现的 spec。每一项都必须单独补写 `docs/superpowers/specs/2026-04-24-immich-v6-people-gallery-productization-<slice>-spec.md`，包含完整行为、验收标准和自动化验证，并通过 reviewer 后，才能移动到 `Split Specs`。

### Candidate C：v6 在线人物归属

- Planned spec path: `docs/superpowers/specs/2026-04-24-immich-v6-people-gallery-productization-online-assignment-spec.md`
- Scope: 在扫描入库后按 Immich v6 在线语义创建匿名人物和 active assignment；公共入口仍是 `hikbox scan start --workspace <path>`。
- Acceptance summary: manifest 期望成组的人物形成匿名 person；低于阈值的 face 不进入人物库；重复扫描保持 person/assignment 幂等。

### Candidate D：人物库 WebUI 浏览与命名

- Planned spec path: `docs/superpowers/specs/2026-04-24-immich-v6-people-gallery-productization-webui-naming-spec.md`
- Scope: 通过 `hikbox serve --workspace <path>` 提供 localhost WebUI，展示已命名/匿名人物，并支持人物命名和重命名。
- Acceptance summary: Playwright 可通过真实页面观察人物首页、详情页、命名表单、DB 姓名变化和审计记录。

### Candidate E：人物合并与最近一次撤销

- Planned spec path: `docs/superpowers/specs/2026-04-24-immich-v6-people-gallery-productization-merge-undo-spec.md`
- Scope: 在人物首页批量合并人物，并支持撤销全局最近一次合并；公共入口是 WebUI/API。
- Acceptance summary: 合并后 assignment 迁移到目标人物，源人物失效；撤销最近一次合并后人物和 assignment 恢复到合并前可观察状态。

### Candidate F：误归属排除

- Planned spec path: `docs/superpowers/specs/2026-04-24-immich-v6-people-gallery-productization-exclusion-spec.md`
- Scope: 在人物详情页排除单个或多个误归属样本，且后续扫描不得自动归回同一人物；公共入口是 WebUI/API。
- Acceptance summary: 排除后 active assignment 失效、exclusion 记录落库、详情页样本移除；重扫后被排除 face 不回到原人物。

### Candidate G：导出模板与执行

- Planned spec path: `docs/superpowers/specs/2026-04-24-immich-v6-people-gallery-productization-export-template-spec.md`
- Scope: 基于已命名人物创建导出模板，预览并导出同时包含指定人物的照片，按 only/group 与月份分桶；公共入口是 WebUI/API 和导出文件树。
- Acceptance summary: 模板预览与 manifest `expected_exports` 一致；执行后真实文件树、Live MOV 配对复制和导出账本可验证。

## Cross-Slice Contracts

- CLI 只负责 `init`、`source`、`scan start`、`serve`；人物维护和导出只通过 WebUI/API 操作。
- 所有 CLI 命令都必须显式传入 `--workspace <path>`。
- WebUI 仅面向本机单用户、`localhost` 使用；首版不做账号系统、多用户协作、远程访问和多标签页一致性保障。
- WebUI 使用 FastAPI + Jinja2 服务端渲染，可用少量原生 JS 增强表单提交、局部刷新和确认弹窗；首版不引入 React/Vue 等前端框架。
- 扫描运行期间禁止启动 WebUI；存在 `running|aborting` 扫描会话时，`hikbox serve --workspace <path>` 必须失败退出且不监听端口。
- 导出运行中禁止命名、合并、撤销合并、排除等人物归属写操作。
- 页面视觉验收使用仓库现有 Playwright 入口；截图、JSON 报告和服务日志保存到 `.tmp/<task-name>/`。

## 验收集 Manifest Contract

后续端到端验收使用用户提供的小图库和人工标注 manifest。manifest 是验收输入契约，至少包含：

- `people`：人物标签、期望显示名、是否期望扫描后自动形成匿名人物。
- `assets`：照片文件名、拍摄月份、应包含的人物标签、是否存在 Live MOV 配对。
- `expected_person_groups`：扫描后哪些照片或 face 应归到同一个人物标签。
- `expected_exports`：模板选择哪些人物时，哪些文件应导出到 `only/YYYY-MM` 或 `group/YYYY-MM`。
- `tolerances`：允许不计入自动断言的边界照片或边界 face，避免真实模型偶发差异阻塞核心流程验收。

manifest 不得作为实现逻辑的输入，只能作为测试断言数据。人物命名必须通过 WebUI 完成；测试可以先根据 `expected_person_groups` 在扫描结果中定位自动形成的匿名 person，再通过 Playwright 打开人物详情页提交命名表单。
