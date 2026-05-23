# 导出连拍挑选与重复导出清理 Spec

## Goal

用户可以在某个导出模板的候选照片范围内识别内容相似的连续拍摄照片，手动选择每组要保留的照片，并把未保留照片写入工作区级的全局放弃导出标记；后续所有模板预览和导出都跳过这些标记照片，同时提供一个只依据 DB 导出账本清理既有导出目录中重复文件的安全脚本。

## Global Constraints

- 与用户的交流、spec、测试计划和新增注释均使用中文；固定英文术语、路径、API 字段名可保留英文。
- 核心行为必须通过公共入口验证；mock/stub/no-op 路径不得满足验收。
- 运行测试时遵循仓库 `AGENTS.md` 的逐文件策略；不要参考 `README.md` 的测试运行策略，不使用 `scripts/run_tests.sh`。
- 测试默认复用 `scanned_workspace` 或 `copy_scanned_workspace(tmp_path)`；只有验证自定义图片变体、真实增量扫描或脚本文件系统副作用时，才允许按 spec 明确理由使用临时 source 或真实文件系统准备链。
- 所有临时测试文件、调试产物、JSON 报告、截图、脚本 dry-run 输出和日志必须放到 `.tmp/<task-name>/`。
- 任何数据库 schema 修改必须走 `hikbox_pictures/product/db/sql/` 下的新 migration SQL，并同步更新 `docs/db_schema.md`。
- 全局放弃导出标记不删除源图库文件、不删除 `assets`、不删除人脸样本、不改写历史 `export_delivery` 账本。
- 已放弃 asset 对所有导出模板全局生效；不能因为旧 `export_plan`、旧页面状态或直接执行入口再次导出。
- 连拍识别只用于辅助用户挑选，不自动决定删除或放弃；最终放弃标记只能由用户提交保留选择后产生。
- 既有导出目录清理脚本只能依据工作区 DB 的放弃标记和导出账本定位文件；不得按文件名、目录扫描或相似度猜测额外清理对象。

## Split Specs

### Child Spec A: 模板连拍挑选与全局放弃标记

- [ ] Implementation status: Not done
- Spec: [2026-05-23-burst-pick-export-dedupe-child-a-template-burst-pick-spec.md](./2026-05-23-burst-pick-export-dedupe-child-a-template-burst-pick-spec.md)
- Scope: 在 `/exports` 的单个导出模板候选集内识别相似连拍组，提供 WebUI 保留选择和提交入口，写入全局放弃导出标记，并让后续预览、`export_plan` 和执行导出跳过这些 asset。
- Acceptance summary: 用户从模板列表进入“连拍挑选”，每个相似组至少选择 1 张保留照片后提交；未保留 asset 被持久标记为全局放弃，所有模板后续预览和导出都不再包含它们。

### Child Spec B: 既有导出目录重复文件清理脚本

- [ ] Implementation status: Not done
- Spec: [2026-05-23-burst-pick-export-dedupe-child-b-export-cleanup-script-spec.md](./2026-05-23-burst-pick-export-dedupe-child-b-export-cleanup-script-spec.md)
- Scope: 新增默认 dry-run 的维护脚本，只依据全局放弃标记与 `export_delivery`/`export_plan` 精确定位某个既有导出根目录下应清理的静态图和已导出的 Live Photo 配对 MOV，并在显式 `--apply` 时移动到隔离目录。
- Acceptance summary: 用户对指定导出根目录运行脚本时，dry-run 能展示完整移动计划且不改文件；加 `--apply` 后仅把 DB 精确定位到的 abandoned 导出文件按原相对结构移动到隔离目录，不删除文件、不改 DB、不按文件名猜测。

## Candidate Future Split Specs

- 自动推荐保留照片：在相似组内自动推荐最佳保留项，但仍需用户确认后才写入放弃标记。
- 放弃标记撤销或管理页：提供查看、撤销或批量管理全局放弃标记的入口。
- 全工作区相似照片整理：脱离导出模板候选集，面向整个 workspace 做相似照片整理。
- 源图库删除或移动：对源图库文件执行删除、移动到回收目录或生成删除清单。
