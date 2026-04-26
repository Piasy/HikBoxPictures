# 仓库协作约定

## 语言要求

- 与用户的所有交流一律使用中文。
- 新增或修改的文档一律使用中文，除非用户明确要求保留原文或第三方内容必须使用英文。
- 新增或修改的代码注释一律使用中文；如果引用的是外部协议、标准、接口字段或库 API 的固定英文术语，可在必要时保留英文原文，但解释文字仍需使用中文。

## 执行要求

- 在开始实现、规划、评审或说明时，先检查并遵循本文件。
- 运行仓库内的 Python、测试或脚本命令时，优先使用仓库根目录的 `.venv` 环境；如果 `.venv` 不存在，先执行 `./scripts/install.sh` 完成环境安装，再继续后续操作。
- 常用安装、测试和 CLI 命令说明查看 `README.md`；如果 README 与本文件冲突，以本文件为准。
- 任何涉及数据库 schema 的修改，包括 migration、建表、字段、索引、约束调整，都必须同步更新 `docs/db_schema.md`，保证文档与最新 migration 链一致。
- 如果发现仓库中的既有内容与本约定冲突，后续修改时应优先向中文约定收敛；涉及大规模历史内容时，可分步骤整理，但新内容必须立即遵循本约定。
- 所有临时测试文件、调试产物、截图、JSON 报告、临时 runner、临时日志等，一律放到仓库根目录 `.tmp/` 下，按任务创建子目录，不要散落在根目录其他位置。
- 不要在仓库根目录新建 `.tmp-*`、`tmp-*` 等零散临时目录；如果工具支持输出目录参数，统一显式指向 `.tmp/<task-name>/`。
- `.gitignore` 对这类临时测试产物只保留 `.tmp/` 这一条忽略规则；新增临时用途时不要继续追加新的 `.tmp-*` 忽略模式。

## 自动化验收约定

- 完成测试用例执行起来耗时较久（10 分钟左右），如果需要执行全量用例，需要耐心等待；所以运行测试时，可以按需执行修改涉及到的用例。
- 前端和 WebUI 的真正验收以 `tests/people_gallery/test_webui_*_playwright.py` 这类 Python Playwright + pytest 测试为主；测试应进入 CI 或至少能被 `./scripts/run_tests.sh` 直接执行。
- WebUI 主路径验收必须尽量走真实公共入口：真实 `hikbox` CLI、真实 HTTP 服务、真实页面交互、真实 SQLite、真实图片 artifact；不要用 mock/stub/no-op、直接改库或模板函数调用替代核心行为。
- CLI 启动失败、端口占用、schema 缺失、扫描运行中拒绝服务等边界，不必强行用浏览器覆盖；优先用服务级集成测试验证退出码、stderr 和端口状态。
- 当前前端验收范围以 Chromium 桌面浏览器为准；移动端兼容性不作为本阶段要求，也不作为阻塞项。
- Playwright 交互优先使用 role/name 等语义定位，并结合页面暴露的稳定 `data-*` 标识与 DB/artifact 对齐；不要把截图识别或脆弱 CSS selector 作为主断言。
- 运行 Python Playwright 用例前，优先按 README 执行：
  - `source .venv/bin/activate`
  - `python3 -m playwright install chromium`
- 只有在需要截图留档、做页面视觉检查，或排查中文渲染问题时，才额外执行 `./scripts/setup_playwright_zh_fonts.sh`。

## Playwright 调试约定

- 需要做页面视觉检查、截图留档或交互调试时，优先复用仓库已有的 Playwright 入口，不要临时起一套新的杂散命令。
- Playwright 入口统一使用 `tests/people_gallery/test_webui_*_playwright.py` 这类 Python Playwright + pytest 测试；不要新增独立的并行脚本入口。
- 做页面调试时，优先通过 pytest 运行指定 WebUI Playwright 用例，并按需用环境变量或 pytest 参数控制本地服务、输出目录、浏览器安装和截图留存；如果现有测试缺少必要调试能力，优先在 Python pytest 入口内补 helper 或 fixture。
- 当前做 Playwright 调试时，只覆盖桌面视口和 Chromium 布局。
- 截图不是默认必留产物。只有在 agent 判断视觉或布局存在不确定性、需要人工复核、用户明确要求保存视觉截图，或正在排查视觉回归时，才保存页面截图。
- 调试产物按需保留到 `.tmp/<task-name>/`：服务日志和 JSON 指标报告优先用于复盘自动化结果；截图只在上一条触发条件满足时保留。
