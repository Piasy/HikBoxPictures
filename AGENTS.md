# 仓库协作约定

## Agent 必读

- 本文件是仓库工作约束；涉及语言、执行、spec/测试计划、Playwright 调试或测试 fixture 时，必须遵循对应章节的详细要求；测试运行细则见 `README.md` 的「运行测试」。
- 写新需求 spec 或测试前，优先确认是否能复用 `people_gallery_scan/` 的已扫描图库基线；默认使用 `scanned_workspace` 或 `copy_scanned_workspace(tmp_path)`，避免重复执行耗时的 `init -> source add -> scan start`。

## 语言要求

- 与用户的所有交流一律使用中文。
- 新增或修改的文档一律使用中文，除非用户明确要求保留原文或第三方内容必须使用英文。
- 新增或修改的代码注释一律使用中文；如果引用的是外部协议、标准、接口字段或库 API 的固定英文术语，可在必要时保留英文原文，但解释文字仍需使用中文。

## 执行要求

- 在开始实现、规划、评审或说明时，先检查并遵循本文件。
- 运行仓库内的 Python、测试或脚本命令时，优先使用仓库根目录的 `.venv` 环境；如果 `.venv` 不存在，先执行 `./scripts/install.sh` 完成环境安装，再继续后续操作。
- 常用安装、测试和 CLI 命令说明查看 `README.md`。
- 任何涉及数据库 schema 的修改，都必须遵循 `docs/db_schema.md` 的「DB Migration 机制」，并同步更新该文档，保证文档与最新 migration 链一致。
- 所有临时测试文件、调试产物、截图、JSON 报告、临时 runner、临时日志等，一律放到仓库根目录 `.tmp/` 下，按任务创建子目录，不要散落在根目录其他位置；如果工具支持输出目录参数，统一显式指向 `.tmp/<task-name>/`。

## Spec 与已扫描基线约定

- 写 spec、任务拆分、验收说明或测试计划时，必须先选择“验收基线”；除非验证目标就是 CLI 初始化/扫描流程本身，否则默认复用已扫描图库基线。
- 需要“已完成主基线扫描的图库”、“已有 People Gallery 人物数据”或“WebUI 主路径的已扫描 workspace”时，验收准备写为使用 `scanned_workspace` 或 `copy_scanned_workspace(tmp_path)`；不要要求重新执行 `init -> source add tests/fixtures/people_gallery_scan -> scan start`。
- 只有验证 `init`、`source add/list`、首次扫描、重扫语义、扫描运行中拒绝服务、迁移启动路径、自定义图片子集，或必须观察真实增量 `source add -> scan start` 时，才能要求真实 CLI 准备链（只能使用 `tests/fixtures/people_gallery_scan/`），并必须写明例外理由。
- 需要增量样本时，写为“复用主基线预扫描 workspace 副本 -> `source add tests/fixtures/people_gallery_scan_2/` -> 新的 `scan start`”。
- 自动化验收入口和运行方式以 `README.md` 的「运行测试」为准；WebUI 验收文件必须命名为 `tests/people_gallery/test_webui_*_playwright.py`。

## Playwright 调试约定

- 需要做页面视觉检查、截图留档或交互调试时，优先复用仓库已有的 Playwright 入口，不要临时起一套新的杂散命令。
- Playwright 入口统一使用 `tests/people_gallery/test_webui_*_playwright.py` 这类 Python Playwright + pytest 测试；不要新增独立的并行脚本入口。
- 做页面调试时，优先通过 pytest 运行指定 WebUI Playwright 用例，并按需用环境变量或 pytest 参数控制本地服务、输出目录、浏览器安装和截图留存；如果现有测试缺少必要调试能力，优先在 Python pytest 入口内补 helper 或 fixture。
- 当前做 Playwright 调试时，只覆盖桌面视口和 Chromium 布局。
- 截图不是默认必留产物。只有在 agent 判断视觉或布局存在不确定性、需要人工复核、用户明确要求保存视觉截图，或正在排查视觉回归时，才保存页面截图。
- 调试产物按需保留到 `.tmp/<task-name>/`：服务日志和 JSON 指标报告优先用于复盘自动化结果；截图只在上一条触发条件满足时保留。

## 测试 Fixture 约定

仓库固定入库两套真实验收图片 fixture，分别位于 `tests/fixtures/people_gallery_scan/` 和 `tests/fixtures/people_gallery_scan_2/`，各自由 `manifest.json` 描述内容与预期断言数据。

- `people_gallery_scan/`（主基线）：精确包含 50 张支持扫描的照片，覆盖 3 个目标人物（每人 10 张单目标 + 合照）、非目标人物、无脸照片、HEIC/HEIF Live Photo 正反例、非支持后缀文件和损坏图片。后续所有需求开发默认复用此 fixture 作为全量基线。
- `people_gallery_scan_2/`（增量补充）：精确包含 15 张新增单目标人物照片，覆盖与主基线相同的 3 个目标人物（每人 5 张）。用于主基线扫描完成后再次 `source add -> scan start` 触发增量扫描的场景，验证新增人脸仍归属到既有人物。
- `people_gallery_scan_2/` 不替代 `people_gallery_scan/`，也不得和 `people_gallery_scan/` 一起作为新 workspace 的两个初始 source 一次性扫描；增量使用方式见「Spec 与已扫描基线约定」。
- 两套 fixture 的 `manifest.json` 只能作为测试断言数据，不得作为产品逻辑输入。
- 不要为新需求默认新增第三套 fixture；确实需要时，先在对应 spec 中说明既有两套 fixture 为什么不够，并明确新增 fixture 的内容矩阵和用途。
