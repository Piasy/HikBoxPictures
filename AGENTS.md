# 仓库协作约定

## 语言要求

- 与用户的所有交流一律使用中文。
- 新增或修改的文档一律使用中文，除非用户明确要求保留原文或第三方内容必须使用英文。
- 新增或修改的代码注释一律使用中文；如果引用的是外部协议、标准、接口字段或库 API 的固定英文术语，可在必要时保留英文原文，但解释文字仍需使用中文。

## 执行要求

- 在开始实现、规划、评审或说明时，先检查并遵循本文件。
- 运行仓库内的 Python、测试或脚本命令时，优先使用仓库根目录的 `.venv` 环境；如果 `.venv` 不存在，先执行 `./scripts/install.sh` 完成环境安装，再继续后续操作。
- 运行命令说明查看 `README.md`。
- 任何涉及数据库 schema 的修改，包括 migration、建表、字段、索引、约束调整，都必须同步更新 `docs/db_schema/README.md`，保证文档与最新 migration 链一致。
- 如果发现仓库中的既有内容与本约定冲突，后续修改时应优先向中文约定收敛；涉及大规模历史内容时，可分步骤整理，但新内容必须立即遵循本约定。
- 所有临时测试文件、调试产物、截图、JSON 报告、临时 runner、临时日志等，一律放到仓库根目录 `.tmp/` 下，按任务创建子目录，不要散落在根目录其他位置。
- 不要在仓库根目录新建 `.tmp-*`、`tmp-*` 等零散临时目录；如果工具支持输出目录参数，统一显式指向 `.tmp/<task-name>/`。
- `.gitignore` 对这类临时测试产物只保留 `.tmp/` 这一条忽略规则；新增临时用途时不要继续追加新的 `.tmp-*` 忽略模式。

## Playwright 使用约定

- 需要做页面视觉检查、截图留档或交互调试时，优先复用仓库已有的 Playwright 入口，不要临时起一套新的杂散命令。
- 当前前端验收范围以 macOS Safari 桌面浏览器为准；移动端兼容性暂不作为本阶段要求，也不作为阻塞项。
- 当前阶段不保留手机端专用兼容分支；如发现已有移动端适配代码，优先删除或收敛为桌面实现。
- 现有入口分两类：
  - `tests/people_gallery/test_webui_*_playwright.py`：走 Python Playwright + pytest，适合纳入测试。
  - `tools/*_playwright_check.py` + `tools/*_playwright_capture.cjs`：走 Node Playwright runner，适合本地调试、截图和一次性排查。
- 运行 Python Playwright 用例前，优先按 README 执行：
  - `source .venv/bin/activate`
  - `./scripts/setup_playwright_zh_fonts.sh`
  - `python3 -m playwright install chromium`
- 做页面调试时，优先使用 `tools/*_playwright_check.py` 这套模式：
  - 脚本负责启动本地服务、准备 node runner、输出截图和 JSON 报告。
  - 常用参数：`--workspace`、`--output-dir`、`--runner-dir`、`--install-browser`。
  - 如仓库里已有对应页面脚本，优先直接复用；没有时，按同样结构在 `tools/` 下补 `*_playwright_check.py` 和 `*_playwright_capture.cjs`，不要绕开现有 runner 约定。
- 当前做 Playwright 调试时，优先覆盖桌面视口和 macOS Safari 近似布局；若移动端截图或交互异常，但桌面 Safari 目标满足，可先记录而不扩展修复范围。
- 调试过程中，至少保留以下产物，便于复盘：
  - 页面截图
  - JSON 指标报告
  - 本地服务日志
