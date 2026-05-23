# 既有导出目录重复文件清理脚本 Spec

## Parent Spec

- Parent Spec: [2026-05-23-burst-pick-export-dedupe-spec.md](./2026-05-23-burst-pick-export-dedupe-spec.md)
- Current Approval Scope: 本 child spec 负责父 spec 中的安全清理补充路径：在 [Child A](./2026-05-23-burst-pick-export-dedupe-child-a-template-burst-pick-spec.md) 已写入工作区级全局放弃导出标记后，提供一个默认 dry-run 的维护脚本，精确定位既有导出目录中这些已放弃 asset 对应的静态图和已导出的 Live Photo 配对 MOV，并在显式 `--apply` 时移动到隔离目录。

## Global Constraints

- Shared constraints: 继承父 spec 的共享约束，见 [2026-05-23-burst-pick-export-dedupe-spec.md](./2026-05-23-burst-pick-export-dedupe-spec.md) 的 `Global Constraints`。
- 本 child spec 依赖 [Child A](./2026-05-23-burst-pick-export-dedupe-child-a-template-burst-pick-spec.md) 的全局放弃导出标记事实；脚本不得自己计算相似照片、不得新增放弃标记、不得修改放弃标记。
- 脚本只能移动导出目录中的既有目标文件；不得移动、删除或改写源图库文件、workspace asset、`assets.absolute_path` 指向的文件、人脸样本或任何 DB 行。
- 脚本的定位依据只能是 workspace DB 中的全局放弃导出标记、`export_delivery`、`export_plan` 和 `export_template.output_root`；不得按文件名、目录遍历结果、相似度、EXIF 或 Live Photo 命名约定猜测额外清理对象。

## Feature Slice 1: Dry-run 生成既有导出重复文件清理计划

- [ ] Implementation status: Not done

### Behavior

- 新增维护脚本 `scripts/cleanup_abandoned_exports.py`，形态参考 `scripts/fix_live_photo_matches.py`：脚本文件可被直接执行，也暴露可导入的核心函数用于测试。
- 用户指定 workspace 或 library DB、一个既有导出根目录和一个隔离根目录后，脚本扫描 DB 中已全局放弃导出的 asset，并为当前导出根目录生成清理计划。
- 脚本支持 DB 中实际存在的 `export_plan` 证据，但不要求 [Child A](./2026-05-23-burst-pick-export-dedupe-child-a-template-burst-pick-spec.md) 在提交放弃标记后保留旧 plan 行；如果某个实现选择立即清理旧 `export_plan`，脚本仍必须能基于 `export_delivery` 证据完成已导出文件清理。
- 默认模式是 dry-run：只打印将要移动的文件、统计信息和问题列表，不创建隔离目录、不移动文件、不修改 DB。
- 每个待处理静态图目标只能由以下两种 DB 证据之一产生：
  - `export_delivery.target_path`：该 abandoned asset 曾经被某个导出运行处理，且目标路径位于本次 `--export-root` 下。
  - `export_plan` + `export_template.output_root`：该 abandoned asset 在 DB 中仍有既存计划行，计划所属模板的 `output_root` 与本次 `--export-root` 归一化后相同，且可由 `output_root / bucket / month / file_name` 精确得到目标路径。
- 对同一个真实目标路径，如果多条 `export_delivery` 或 `export_plan` 证据指向它，清理计划必须去重为一次移动，并保留所有相关 `asset_id`、`template_id`、`run_id`、`delivery_id`、`plan_id` 作为 provenance，供 dry-run 输出和测试断言。
- 对 Live Photo 配对 MOV 的定位只能使用 `export_plan.mov_file_name` 与静态图目标目录拼接得到；不得从静态图文件名换后缀、扫描同目录 MOV、读取源图库 MOV 或使用 `assets.live_photo_mov_path` 推断导出目标 MOV 文件名。
- 当静态图目标存在且对应的 `mov_file_name` 目标也存在时，dry-run 必须把静态图和 MOV 作为同一个 asset 目标组的两个移动项展示；当其中一方缺失时，存在的一方仍可以进入清理计划，缺失的一方计入诊断统计。
- 目标文件相对于 `--export-root` 的相对路径必须被原样保留到隔离目录，例如 `--export-root/only/2026-02/IMG_0001.JPG` 移动到 `--quarantine-root/only/2026-02/IMG_0001.JPG`。
- 清理计划按 `relative_path` 字典序稳定输出；同一 relative path 下静态图排在 MOV 前。

### Public Interface

- CLI：
  ```bash
  python scripts/cleanup_abandoned_exports.py \
    --workspace /path/to/workspace \
    --export-root /path/to/export-output \
    --quarantine-root /path/to/quarantine
  ```
- `--workspace` 与 `--library-db` 二选一，语义与 `scripts/fix_live_photo_matches.py` 一致；`--workspace` 解析为 `<workspace>/.hikbox/library.db`。
- `--export-root` 必填，表示本次只清理这一个导出根目录；脚本不得自动跨所有模板或所有历史输出目录执行移动。
- `--quarantine-root` 必填，表示移动目标根目录；它归一化后不能等于 `--export-root`，也不能位于 `--export-root` 内部。
- `--apply` 可选；未传时固定为 dry-run。
- `--max-print` 可选，默认 `200`，只限制控制台明细打印数量，不影响核心函数返回的完整计划。
- CLI dry-run 输出至少包含：模式、library DB、导出根目录、隔离根目录、待移动静态图数量、待移动 MOV 数量、缺失静态图数量、缺失 MOV 数量、被去重的 DB 证据数量、被拒绝的目标路径数量、目标冲突数量、错误数量，以及最多 `--max-print` 条待移动明细。
- 可导入核心函数返回结构化结果，至少包含：
  - `mode`：`dry_run` 或 `apply`。
  - `moves[]`：每项包含 `asset_id`、`kind`（`still` 或 `mov`）、`source_path`、`relative_path`、`quarantine_path`、`evidence[]`。
  - `evidence[]`：每项包含 `source`（`export_delivery` 或 `export_plan`）、`template_id`、`plan_id`，以及可选 `run_id`、`delivery_id`。
  - `missing[]`：记录 DB 能定位但文件系统中不存在的静态图或 MOV。
  - `blocked[]`：记录路径越界、隔离目标冲突、移动失败或 apply 被运行中导出拒绝等不能执行的目标。
  - `summary`：与 CLI 统计一致的计数。
- 退出码：无错误和无阻塞时返回 `0`；存在路径校验错误、目标冲突、移动失败、DB 读取错误或运行中导出导致 apply 被拒绝时返回 `1`；用户中断时返回 `130` 并打印已生成的部分计划，且不修改 DB。

### Error and Boundary Cases

- workspace 未初始化、`library.db` 不存在、DB schema 缺少 [Child A](./2026-05-23-burst-pick-export-dedupe-child-a-template-burst-pick-spec.md) 的全局放弃导出标记或导出账本表时，脚本返回可读错误，不创建目录、不移动文件。
- DB 无法打开、读取失败或查询执行失败时，脚本返回可读错误和非零退出码，不创建目录、不移动文件。
- `--export-root` 不存在或不是目录时，脚本返回可读错误；dry-run 不应因为目录为空而失败。
- `--quarantine-root` 与 `--export-root` 相同、位于 `--export-root` 内部，或某个待移动目标的隔离路径已存在时，脚本必须拒绝 apply；dry-run 必须把这些问题列入 `blocked[]` 或目标冲突统计。
- DB 证据中的目标路径归一化后不在 `--export-root` 内时，该目标必须被拒绝并计入路径越界；脚本不得移动它，也不得为了让它入组而改用文件名猜测。
- DB 指向的静态图或 MOV 不存在时，脚本不得报 500 式异常；缺失项计入诊断统计，存在的同组文件仍按计划处理，除非存在目标冲突或路径越界。
- 如果 workspace 中存在 `export_run.status='running'`，dry-run 可以生成计划但必须打印运行中导出警告；`--apply` 必须拒绝执行并保持文件系统和 DB 不变。
- 多个 abandoned asset 或多条历史 delivery 指向同一个导出目标路径时，脚本只能计划移动一次，并在 provenance 中保留所有证据；不得重复移动、不得因为重复证据覆盖目标。
- apply 过程中任何单个目标移动失败时，脚本必须停止后续移动，返回非零退出码，报告已经移动和未移动的项目；无论成功或失败，都不得修改 DB。
- 用户中断时，脚本必须返回 `130`；dry-run 或计划生成阶段中断不得移动文件，apply 阶段中断必须停止后续移动并报告已经移动和未移动的项目；所有中断路径都不得修改 DB。

### Non-goals

- 不删除文件；只移动到隔离目录。
- 不清理源图库、不移动 `assets.absolute_path`、不读取或修改源图库 Live Photo MOV。
- 不修改 `export_plan`、`export_delivery`、`export_run`、全局放弃导出标记或任何人脸/人物表。
- 不重新计算导出 preview，不执行导出，不启动 Web 服务。
- 不提供 WebUI 清理入口。
- 不按文件名或目录扫描主动发现“看起来重复”的文件。
- 不合并多个导出根目录为一次 apply；一次脚本运行只处理一个 `--export-root`。

### Acceptance Criteria

#### Shared Verification Baseline

- 主路径：复用 `copy_scanned_workspace(tmp_path)` 得到已扫描主基线 workspace，通过现有命名 helper 创建包含至少 2 个已命名人物的导出模板；先通过现有 preview/execute 公共入口生成既有 `export_plan`、`export_delivery` 和真实导出文件，再通过 [Child A](./2026-05-23-burst-pick-export-dedupe-child-a-template-burst-pick-spec.md) 的真实公共入口把其中一批已导出 asset 写入全局放弃导出标记，最后运行清理脚本处理这些既有导出文件。
- 脚本入口：自动化验收必须覆盖可导入核心函数和真实 CLI 入口；真实 CLI 入口使用 `.venv/bin/python scripts/cleanup_abandoned_exports.py ...` 或等价 Python 解释器运行，不通过 shell fake 输出满足验收。
- 文件系统副作用例外：本 child spec 的目标就是验证既有导出目录移动副作用，因此允许在测试 `tmp_path` 下创建导出根目录、隔离根目录、缺失文件、目标冲突文件和计划内但未 delivery 的目标文件；这些产物必须位于测试临时目录或 `.tmp/burst-pick-export-dedupe/`。
- DB 准备边界：主路径 abandoned 标记必须在既有导出文件和账本已经产生后，由 [Child A](./2026-05-23-burst-pick-export-dedupe-child-a-template-burst-pick-spec.md) 的真实 Web/API 提交入口产生，不得直接 `INSERT` 放弃标记来满足清理成功 AC。仅在制造内部故障、历史账本异常、AC-3 的遗留 `mov_file_name` 证据状态，或 AC-4 的遗留 `export_plan`-only 证据状态时，允许用 SQL 调整 `export_delivery.target_path`、制造 `running` export_run、构造目标冲突所需的账本状态，或为已通过真实入口标记为 abandoned 的 asset 构造 `export_plan` 行及 `mov_file_name`；这些 SQL 不得伪造 abandoned 标记、不得绕过脚本公共入口、不得直接移动文件，也不得把 DB 中没有精确证据的文件加入成功计划。
- 默认断言层级：CLI stdout/stderr + 核心函数结构化结果 + SQLite DB + 文件系统树；调试产物保留到 `.tmp/burst-pick-export-dedupe/`。
- 防 mock 逃逸禁令：不得 mock 文件移动成功来满足 apply AC；不得通过直接删除导出文件替代脚本 apply；不得用硬编码 fixture 文件名绕过 DB 查询；不得通过目录遍历结果把 DB 中没有证据的文件加入清理计划。
- 测试运行遵循仓库逐文件策略，优先逐个运行受影响的脚本测试文件和导出模板集成测试文件。

#### AC-1: CLI dry-run 只生成计划且不移动文件

- 触发：在 fresh workspace 中先通过现有 preview/execute 公共入口生成一批既有导出文件和 `export_delivery` 账本，再通过 [Child A](./2026-05-23-burst-pick-export-dedupe-child-a-template-burst-pick-spec.md) 公共入口把其中一批已导出 asset 标记为 abandoned，确认导出根目录中仍存在对应静态图；然后运行 `python scripts/cleanup_abandoned_exports.py --workspace <workspace> --export-root <output_root> --quarantine-root <quarantine_root>`，不传 `--apply`。
- 必须可观察：CLI 输出模式为 dry-run；输出包含待移动静态图数量、待移动 MOV 数量、缺失数量、去重证据数量和待移动明细；导出根目录中的文件仍存在；隔离目录不存在或为空；DB 中全局放弃标记、`export_plan`、`export_delivery`、`export_run` 均不变化。
- 验证手段：服务级集成测试先通过真实 HTTP/API 准备既有导出账本和文件，再通过真实 [Child A](./2026-05-23-burst-pick-export-dedupe-child-a-template-burst-pick-spec.md) 入口准备 abandoned 标记，随后用真实 CLI 运行脚本；断言退出码、输出关键行、文件系统 hash/存在性和 DB 快照。

#### AC-2: apply 按相对结构移动静态图且不改 DB

- 触发：复用 AC-1 同类准备，但传入 `--apply`。
- 必须可观察：每个存在的 abandoned 静态图从 `--export-root/<relative_path>` 移动到 `--quarantine-root/<relative_path>`；相对路径中的 `only/group`、月份目录和冲突消解后的文件名保持不变；非 abandoned asset 的导出文件仍留在原导出根目录；源图库文件仍存在且内容不变；DB 中全局放弃标记、`assets`、`export_plan`、`export_delivery`、`export_run` 均不变化；CLI 返回 `0` 并报告实际移动数量。
- 验证手段：服务级集成测试运行真实 CLI apply，比较移动前后导出根目录和隔离根目录文件树、源文件 hash、DB 快照和 CLI 统计。

#### AC-3: Live Photo 配对 MOV 与静态图一起进入计划和隔离目录

- 触发：先通过现有 preview/execute 公共入口准备一个已导出的 Live Photo asset 和导出目录中的真实 MOV，再通过 [Child A](./2026-05-23-burst-pick-export-dedupe-child-a-template-burst-pick-spec.md) 公共入口把该 asset 标记为全局放弃，并确保 DB 中存在可解析的 `export_plan.mov_file_name` 指向该既有导出 MOV；运行 dry-run 后再在 fresh state 中运行 apply。如果 [Child A](./2026-05-23-burst-pick-export-dedupe-child-a-template-burst-pick-spec.md) 的实现会在提交放弃标记时清理旧 `export_plan`，测试可只为这个已 abandoned asset 构造遗留 `export_plan.mov_file_name` 证据。
- 必须可观察：dry-run 的同一 asset 目标组中同时列出静态图和 MOV，MOV 的 `source_path` 等于静态图目标目录加 `export_plan.mov_file_name`；apply 后静态图和 MOV 都按同一相对目录移动到隔离目录；脚本没有从静态图文件名换后缀、目录扫描或 `assets.live_photo_mov_path` 推断 MOV；如果 MOV 缺失，静态图仍可移动且缺失 MOV 计入诊断；如果 DB 中没有可解析的 `mov_file_name` 证据，脚本不得为了移动 MOV 而猜测文件名。
- 验证手段：集成测试先沿用现有导出 Live Photo 测试的方式制造 HEIC/JPG + MOV 导出文件，再通过真实 [Child A](./2026-05-23-burst-pick-export-dedupe-child-a-template-burst-pick-spec.md) Web/API 入口生成 abandoned 标记；必要时允许用 SQL 仅补足该已 abandoned asset 的遗留 `export_plan.mov_file_name` 证据。通过真实 CLI dry-run/apply 断言结构化结果、文件树和缺失 MOV 子情形；必要时把源 `assets.live_photo_mov_path` 改成不同文件名作为负向前置，确认导出目标 MOV 仍来自 `export_plan.mov_file_name`。
- 降级理由：Child A 合法实现可以清理旧 `export_plan`，导致历史 Live Photo MOV 目标名无法稳定从公共路径保留；直接 SQL 只用于制造精确的遗留 MOV 目标名证据，不能伪造 abandoned 标记、不能伪造脚本输出、不能直接移动文件。

#### AC-4: `export_delivery` 和既存 `export_plan` 都能作为精确定位证据

- 触发：覆盖两个子情形：(a) 先通过真实 execute 生成 `export_delivery.target_path` 和既有导出文件，再通过 [Child A](./2026-05-23-burst-pick-export-dedupe-child-a-template-burst-pick-spec.md) 公共入口把该 asset 标记为 abandoned，随后运行脚本；(b) 对已通过真实 [Child A](./2026-05-23-burst-pick-export-dedupe-child-a-template-burst-pick-spec.md) 公共入口标记为 abandoned 的 asset，在 DB 中存在 `export_plan` 行但没有 delivery，测试在对应 `output_root / bucket / month / file_name` 放置一个既有文件后运行脚本。子情形 (b) 表示遗留或异常 DB 中仍保留的 plan-only 证据；它不得要求 [Child A](./2026-05-23-burst-pick-export-dedupe-child-a-template-burst-pick-spec.md) 在提交放弃标记后必须保留旧 `export_plan` 行。
- 必须可观察：(a) 清理计划从 delivery 定位目标路径，并在 provenance 中包含 `source="export_delivery"`、`run_id`、`delivery_id`、`plan_id`、`template_id`。(b) 当 DB 中确实存在 plan-only 证据时，清理计划从 plan 定位目标路径，并在 provenance 中包含 `source="export_plan"`、`plan_id`、`template_id`，即使没有 `run_id` 或 `delivery_id` 也能移动该既有文件。两种子情形都只处理已全局放弃 asset；如果 DB 中不存在 `export_plan` 行，脚本不得为满足 plan-only 场景按文件名或目录猜测目标。
- 验证手段：服务级集成测试对子情形 (a) 使用真实 preview/execute 生成 delivery 路径和既有导出文件，再通过真实 [Child A](./2026-05-23-burst-pick-export-dedupe-child-a-template-burst-pick-spec.md) Web/API 入口生成 abandoned 标记；子情形 (b) 允许用 SQL 仅为已通过真实 [Child A](./2026-05-23-burst-pick-export-dedupe-child-a-template-burst-pick-spec.md) 入口标记的 abandoned asset 构造遗留 `export_plan` 行和对应既有文件。随后调用核心函数与真实 CLI 交叉断言 provenance、移动结果和 DB 不变。
- 降级理由：Child A 合法实现可以在提交放弃标记时清理旧 `export_plan` 行，因此 plan-only abandoned 状态不能稳定依赖公共路径准备；直接 SQL 只用于制造这种遗留 DB 输入状态，不能伪造 abandoned 标记、不能伪造脚本输出、不能直接移动文件。

#### AC-5: 不按文件名或目录扫描扩大清理范围

- 触发：在导出根目录中放置三个干扰文件：(a) 文件名与 abandoned asset 源文件名相似但 DB 没有 `export_delivery`/`export_plan` 目标证据；(b) 位于同月目录但 asset 未被全局放弃；(c) 看起来像 Live Photo MOV 但没有 `export_plan.mov_file_name` 证据。运行 dry-run 和 apply。
- 必须可观察：三个干扰文件都不出现在 dry-run `moves[]` 中，apply 后仍留在原位置；CLI 统计不把它们计入待移动或缺失；只有 DB 同时满足 abandoned 标记和导出账本证据的目标被处理。
- 验证手段：文件系统集成测试在真实导出根目录追加干扰文件，用核心函数结构化结果和 apply 后文件树做负向断言。

#### AC-6: 重复账本证据去重且 provenance 完整

- 触发：构造同一个 abandoned asset 或多个 abandoned asset 的多条历史 delivery/plan 证据指向同一个 `--export-root` 下的真实目标路径，然后运行 dry-run 和 apply。
- 必须可观察：dry-run 只产生一个对应 source path 的移动项；该项的 `evidence[]` 包含所有相关 delivery/plan 证据；summary 记录被去重的 DB 证据数量；apply 只移动一次且不因第二条证据报错。
- 验证手段：服务级集成测试在真实 execute 结果基础上用 SQL 复制历史 delivery 或构造第二个 plan 证据来制造重复账本条件；用核心函数和真实 CLI 输出断言去重、provenance 和单次移动。
- 降级理由：重复 delivery/plan 证据属于历史账本异常或多模板复用输出根的内部状态，普通用户操作难以稳定触发；SQL 只用于制造重复证据，成功移动仍必须由真实脚本根据文件系统执行。

#### AC-7: 路径越界、隔离目标冲突和非法隔离根会阻止 apply

- 触发：分别制造三类失败条件：(a) DB 证据中的目标路径归一化后不在 `--export-root` 下；(b) `--quarantine-root/<relative_path>` 已存在文件；(c) `--quarantine-root` 等于或位于 `--export-root` 内。运行 dry-run 和 apply。
- 必须可观察：dry-run 返回结构化 `blocked[]` 或冲突统计并打印可读原因；apply 返回非零退出码，原导出文件、隔离目录和 DB 均不变化；脚本不会通过改写 relative path、覆盖隔离目标或退回文件名猜测继续执行。
- 验证手段：脚本集成测试用真实临时文件系统和必要 SQL 制造三类失败条件，断言退出码、输出、核心函数 blocked 结果、文件 hash/存在性和 DB 快照。
- 降级理由：路径越界账本属于内部异常，普通 UI 不会自然产生；SQL 只用于制造失败条件，不用于伪造成功路径。

#### AC-8: 运行中导出时 apply 拒绝但 dry-run 可诊断

- 触发：在 workspace DB 中存在 `export_run.status='running'` 时，对同一个导出根目录运行 dry-run 和 apply。
- 必须可观察：dry-run 可以返回计划，但输出包含运行中导出警告；apply 返回非零退出码并说明已有导出运行中，未移动任何文件，未修改 DB。
- 验证手段：服务级或脚本集成测试通过受控 SQL 制造 running run，运行真实 CLI dry-run/apply 并断言输出、文件系统和 DB 快照。
- 降级理由：稳定挂起真实导出会让测试脆弱；这里的 SQL 只用于制造运行中锁状态，脚本行为仍通过真实 CLI 入口验证。

#### AC-9: 缺失文件被诊断且不影响其他可移动目标

- 触发：准备多个 abandoned 导出目标，其中至少一个静态图已从导出目录手动删除，另一个 Live Photo MOV 已缺失，另有正常存在的 abandoned 静态图。运行 dry-run 和 apply。
- 必须可观察：缺失静态图和缺失 MOV 分别计入诊断；存在的 abandoned 目标仍进入计划并在 apply 后移动；缺失项不会导致脚本异常退出；DB 不变化。
- 验证手段：文件系统集成测试删除指定目标文件后运行真实 CLI，断言 summary、`missing[]`、移动结果和 DB 快照。

#### AC-10: CLI 输入错误、输出截断和执行中断/移动失败都有可验证失败契约

- 触发：覆盖五类子情形：(a) 分别使用不存在或未初始化的 `--workspace`、不存在的 `--library-db`、缺少 Child A 全局放弃导出标记表的临时 `library.db`、不可读取或查询失败的 `library.db`、不存在或不是目录的 `--export-root` 运行真实 CLI；(b) 在至少 2 个待移动项的有效计划上使用 `--library-db <db>` 和 `--max-print 1` 运行 dry-run；(c) 在计划生成阶段或 dry-run 输出阶段触发用户中断；(d) 在 apply 已成功移动第一个目标后，让第二个目标移动抛出受控文件系统错误；(e) 在 apply 已成功移动第一个目标后，让第二个目标移动抛出 `KeyboardInterrupt`。
- 必须可观察：(a) 每个非法输入或 DB/schema 错误都返回退出码 `1` 和可读错误，且不创建隔离目录、不移动任何导出文件、不修改 DB。(b) CLI 仍按完整计划返回 summary 计数，结构化核心函数结果包含全部 `moves[]`，但控制台待移动明细最多打印 1 条，并提示还有未打印项目；使用 `--library-db` 时不要求 workspace 存在。(c) 脚本返回 `130`，打印已生成的部分计划或中断说明，不移动文件、不修改 DB。(d) 脚本返回退出码 `1`，不能返回 `130`，停止后续移动，并报告已经移动和未移动的项目；已完成移动的文件位于隔离目录，失败项和后续未移动项仍留在导出根目录；DB 全部保持不变。(e) 脚本返回 `130`，停止后续移动，并报告已经移动和未移动的项目；已完成移动的文件位于隔离目录，触发中断的项目和后续未移动项仍留在导出根目录；DB 全部保持不变。脚本不得吞掉普通移动失败或 `KeyboardInterrupt` 后继续移动后续目标，也不得把部分失败或用户中断伪装成成功。
- 验证手段：脚本集成测试用真实 CLI 覆盖非法输入、schema 缺失、DB 读取错误、`--library-db` 和 `--max-print`；用核心函数或 in-process `main(argv)` 的受控中断/移动失败注入覆盖 `KeyboardInterrupt` 和单项移动失败，并用文件树、退出码、stdout/stderr、结构化结果和 DB 快照断言。
- 降级理由：真实终端 `Ctrl-C` 和操作系统级移动失败在 CI 中不稳定；受控注入只用于制造中断或底层移动异常，不得伪造成功移动、不得绕过真实计划生成、不得修改返回结果来满足断言。

### Done When

- 所有 Acceptance Criteria 都通过对应的“验证手段”完成自动化验证。
- 没有核心需求是通过直接状态修改、硬编码数据、占位行为或 fake integration 满足的。
