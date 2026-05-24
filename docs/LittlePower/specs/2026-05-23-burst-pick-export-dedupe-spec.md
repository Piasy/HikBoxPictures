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

- [x] Implementation status: Done
- Spec: [2026-05-23-burst-pick-export-dedupe-child-a-template-burst-pick-spec.md](./2026-05-23-burst-pick-export-dedupe-child-a-template-burst-pick-spec.md)
- Scope: 在 `/exports` 的单个导出模板候选集内识别相似连拍组，提供 WebUI 保留选择和提交入口，写入全局放弃导出标记，并让后续预览、`export_plan` 和执行导出跳过这些 asset。
- Acceptance summary: 用户从模板列表进入“连拍挑选”，每个相似组至少选择 1 张保留照片后提交；未保留 asset 被持久标记为全局放弃，所有模板后续预览和导出都不再包含它们。
- Non-blocking concern: code-quality focused re-review 指出 Web 端 active 模板在运行中导出时返回 423 的路径没有单独 Web 回归测试；API 423 与 DB 原子性测试已覆盖核心竞态，当前实现路径稳定，本轮接受该测试覆盖缺口。后续若调整 Web 错误反馈或导出运行锁语义，应补对应 Web 回归测试。

### Child Spec B: 既有导出目录重复文件清理脚本

- [ ] Implementation status: Not done
- Spec: [2026-05-23-burst-pick-export-dedupe-child-b-export-cleanup-script-spec.md](./2026-05-23-burst-pick-export-dedupe-child-b-export-cleanup-script-spec.md)
- Scope: 新增默认 dry-run 的维护脚本，只依据全局放弃标记与 `export_delivery`/`export_plan` 精确定位某个既有导出根目录下应清理的静态图和已导出的 Live Photo 配对 MOV，并在显式 `--apply` 时移动到隔离目录。
- Acceptance summary: 用户对指定导出根目录运行脚本时，dry-run 能展示完整移动计划且不改文件；加 `--apply` 后仅把 DB 精确定位到的 abandoned 导出文件按原相对结构移动到隔离目录，不删除文件、不改 DB、不按文件名猜测。

### Child Spec C: 模板连拍多特征召回增强

- [ ] Implementation status: Not done
- Spec: [2026-05-23-burst-pick-export-dedupe-child-c-multifeature-recall-spec.md](./2026-05-23-burst-pick-export-dedupe-child-c-multifeature-recall-spec.md)
- Scope: 在 [Child A](./2026-05-23-burst-pick-export-dedupe-child-a-template-burst-pick-spec.md) 已交付的模板连拍挑选基础上，把相似分组算法升级为多特征规则召回，覆盖重存、轻编辑、轻裁剪、局部变化和短时间连续拍摄，同时用强边聚类与后验校验控制链式误聚。Child C 交付后，仅替换 Child A 中 `visual_fingerprint_v1` 和 `groups[].match_evidence.edges[]` 相关 API evidence 合同；异步任务、DB 持久化 run/group、原图幻灯片、每组独立提交、全局放弃导出标记、后续 preview/export 跳过等交互和数据生命周期语义继续继承 Child A。
- Acceptance summary: 用户刷新连拍挑选页后，新算法后台 run 能持久化包含 `global pHash`、`center pHash`、`block pHash` 等证据的 `strong_edges[]` 相似组；明显相似照片更容易被召回，文件名相邻或时间接近但内容不同的照片不会入组，页面仍可按单组提交保留选择。最终 `GET /api/export-templates/{template_id}/burst-pick` evidence schema 以 Child C 为准，不再要求同时满足 Child A 的 v1 `edges[]` schema。

## Candidate Future Split Specs

- 图像 embedding 召回增强：在多特征规则之后，引入 Vision FeaturePrint 或本地图像 embedding 作为复杂裁剪、构图变化和同场景相似的补充召回入口，并单独定义模型来源、缓存格式、阈值标定、性能预算和误聚控制。
- 自动推荐保留照片：在相似组内自动推荐最佳保留项，但仍需用户确认后才写入放弃标记。
- 放弃标记撤销或管理页：提供查看、撤销或批量管理全局放弃标记的入口。
- 全工作区相似照片整理：脱离导出模板候选集，面向整个 workspace 做相似照片整理。
- 源图库删除或移动：对源图库文件执行删除、移动到回收目录或生成删除清单。
