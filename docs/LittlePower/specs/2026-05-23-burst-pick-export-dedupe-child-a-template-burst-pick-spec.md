# 模板连拍挑选与全局放弃标记 Spec

## Parent Spec

- Parent Spec: [2026-05-23-burst-pick-export-dedupe-spec.md](./2026-05-23-burst-pick-export-dedupe-spec.md)
- Current Approval Scope: 本 child spec 负责父 spec 中的产品主路径：从单个导出模板候选照片中识别相似连拍组，让用户选择每组要保留的照片，并把未保留照片写入全局放弃导出标记，使后续所有模板预览和导出跳过这些 asset。

## Global Constraints

- Shared constraints: 继承父 spec 的共享约束，见 [2026-05-23-burst-pick-export-dedupe-spec.md](./2026-05-23-burst-pick-export-dedupe-spec.md) 的 `Global Constraints`。
- 本 child spec 不提供撤销入口；实现不得暗中保留“可撤销”状态机或恢复按钮作为验收路径。
- 本 child spec 不新增独立的全工作区整理页；入口必须从导出模板出发，识别范围必须是该模板当前可导出候选集。

## Feature Slice 1: 模板候选集相似连拍分组

- [x] Implementation status: Done

### Behavior

- `/exports` 模板列表每个模板行新增“连拍挑选”操作入口。
- 进入某个模板的连拍挑选页时，系统只在该模板当前候选照片集合内查找相似连拍组；已全局放弃的 asset 不参与候选、不参与分组、不在页面展示。
- 候选集合与现有导出预览语义一致：只包含该模板当前会命中的 only/group 静态图 asset，且不改变现有月份、only/group 和人物命中规则。
- 相似分组先使用“拍摄时间 + 设备信息”作为候选比较和阈值辅助信号，再用图像内容相似度形成最终相似边：
  - 拍摄时间和设备信息只作为正向证据；缺失、不一致或被重保存改写时不能阻止图像内容相似的照片入组。
  - 图像内容相似度固定使用 `visual_fingerprint_v1`，不得留给实现阶段自由替换：用 Pillow/pillow-heif 解码源图片，应用 EXIF orientation 后转为 RGB；基于解码后的像素生成 128-bit dHash、16x16 亮度缩略向量和 4x4x4 RGB 归一化颜色直方图。
  - `visual_fingerprint_v1` 的图像预处理固定为：Pillow `ImageOps.exif_transpose` 应用方向，`convert("RGB")` 转 RGB，`convert("L")` 得到亮度图；所有缩放使用 `Image.Resampling.BILINEAR`。dHash 由两部分拼接：亮度图缩放到 9x8 后逐行比较相邻像素，左像素值 `>` 右像素值记 1，否则记 0，按 row-major 顺序得到 64-bit 横向 dHash；亮度图缩放到 8x9 后逐列比较相邻像素，上像素值 `>` 下像素值记 1，否则记 0，按 row-major 顺序得到 64-bit 纵向 dHash。16x16 亮度缩略向量来自亮度图缩放到 16x16 后的 256 个像素值，必须做均值中心化和 L2 归一化；若范数为 0，则保留零向量，两个零向量的 cosine 定义为 1.0，单边零向量 cosine 定义为 0.0。RGB 颜色直方图使用方向修正后的完整 RGB 像素，每通道按 `[0,64)`, `[64,128)`, `[128,192)`, `[192,256)` 分 4 个等宽 bin，形成 64 维直方图并整体 L1 归一化。
  - 两张照片之间的内容相似边必须满足以下任一视觉门槛：(a) `strict`：dHash Hamming 距离 `<= 10` 且颜色直方图交集 `>= 0.88`；(b) `resave_or_light_edit`：dHash Hamming 距离 `<= 18`、16x16 亮度向量 cosine `>= 0.96` 且颜色直方图交集 `>= 0.80`；(c) `metadata_assisted`：两张照片有相同归一化设备信息且拍摄时间差 `<= 10` 秒时，dHash Hamming 距离 `<= 24`、16x16 亮度向量 cosine `>= 0.94` 且颜色直方图交集 `>= 0.72`。颜色直方图交集定义为两个 L1 归一化直方图逐 bin 最小值之和；16x16 亮度向量 cosine 定义为两个 L2 归一化向量的点积；归一化设备信息至少包含去空白并大小写归一后的设备厂商和型号。
  - 拍摄时间和设备信息只能选择更宽松的元数据辅助视觉门槛，不能单独产生相似边；没有可靠拍摄时间/设备时，严格门槛或重保存/轻微修图门槛仍必须能让内容高度相似的照片入组。
  - 相似组由内容相似边的连通分量形成，只展示包含至少 2 个 asset 的连通分量。实现可以为性能做候选 pair blocking，但 blocking 至少要包含一个内容指纹派生入口，不能只依赖拍摄时间、设备、文件名、路径、文件大小或 EXIF。API 必须为每个相似组返回形成该连通分量的相似边证据。
  - 不得把文件名、路径、拍摄设备、拍摄时间、文件大小或 EXIF 作为最终内容相似的替代品。
  - 不使用文件名序号、同目录文件名相邻或文件系统遍历顺序作为分组信号。
  - 即使没有可靠的拍摄时间/设备种子组，只要图像内容高度相似，也必须能形成相似组。
  - 策略偏召回：宁可多展示少量可疑相似组，也不要漏掉明显重保存、修图或转存后的相似照片；误报由用户在保留选择中处理。
- 页面只展示形成相似组的候选照片；未成组的普通候选照片不展示。
- 分组结果必须稳定排序：相似组按组内最小 `asset_id` 升序展示；组内照片按 `asset_id` 升序展示。

### Public Interface

- Web 页面：`GET /exports/{template_id}/burst-pick`，展示该模板的相似连拍组。
- API：`GET /api/export-templates/{template_id}/burst-pick`，返回相同分组数据，至少包含 `template_id`、`groups[]`、每组稳定且 form-safe 的 `group_key`、每张照片的 `asset_id`、`file_name`、`bucket`、`month`、`context_url`、`is_live`、`match_evidence` 和 `diagnostics.skipped_missing_or_unreadable_count`。`group_key` 必须是非空 ASCII 字符串，只使用字母、数字、`-`、`_`，不能包含空白、斜杠、括号、`&`、`=` 或其他会破坏表单字段名的字符。`groups[].match_evidence` 固定为对象：`{"algorithm":"visual_fingerprint_v1","edges":[...]}`。`edges[]` 按 `asset_ids[0]`、`asset_ids[1]` 升序排序；每个 edge 固定包含 `asset_ids`（长度为 2 的升序整数数组）、`threshold`（`strict`、`resave_or_light_edit` 或 `metadata_assisted`）、`metadata_assisted`（布尔值）、`dhash_hamming`（整数）、`luminance_cosine`（数字）、`color_histogram_intersection`（数字）、`capture_time_delta_seconds`（数字或 `null`）和 `normalized_device_match`（布尔值或 `null`）。`edges[]` 必须至少包含足以连通组内所有 asset 的相似边；可以包含额外满足门槛的相似边，但不得包含未满足视觉门槛的 pair。
- Web 表单提交：`POST /exports/{template_id}/burst-pick`，提交页面当前相似组的保留选择。表单字段固定为：重复隐藏字段 `group_key=<group_key>` 表示页面提交时包含的每个当前相似组；每组被选中保留的照片用重复字段 `keep_asset_id__<group_key>=<asset_id>` 表示。服务端必须重新加载当前分组并校验请求中的组和 asset 都属于当前分组。
- API 提交：`POST /api/export-templates/{template_id}/burst-pick`，请求体为 JSON：
  ```json
  {
    "groups": [
      {"group_key": "stable-group-key", "keep_asset_ids": [123, 456]}
    ]
  }
  ```
  成功时返回 JSON，至少包含 `abandoned_asset_ids`、`kept_asset_ids`、`created_count` 和 `already_abandoned_count`；校验失败时返回 `400` 和可读错误。
- Web 页面入口：`/exports` 列表页每行新增“连拍挑选”链接，指向 `/exports/{template_id}/burst-pick`。
- DB：允许新增视觉指纹缓存表和全局放弃导出标记表；具体表名由实现确定，但必须表达以下事实：
  - 每个被放弃 asset 至多有一条全局放弃导出标记；
  - 标记记录能追溯到触发它的模板、相似组和创建时间；
  - 视觉指纹缓存如果存在，必须按 asset 维度可复用，且不能改变源图库或人脸归属真相。

### Error and Boundary Cases

- 模板不存在或已 invalid：页面/API 返回现有导出预览一致的可读错误，不写入任何标记。
- 模板候选集中没有相似组：页面展示空状态，API 返回空 `groups`，不写入任何标记。
- 某些 asset 源文件缺失或不可解码：该 asset 不得导致整个页面 500；它应被跳过并在 API 的诊断摘要中计数，除非数据库本身不可读。
- 视觉特征准备或缓存写入失败：页面/API 返回可读错误，不写入部分分组结果或放弃标记。
- 视觉指纹算法版本不匹配或缓存内容不可解码：必须重新计算 `visual_fingerprint_v1`；如果重新计算失败，按视觉特征准备失败处理。
- 同一 asset 已被全局放弃：它不再展示；重复提交也不能创建第二条放弃记录。
- 提交请求缺少某个当前相似组、某组没有保留 asset、`keep_asset_ids` 包含不属于该组的 asset、或 `group_key` 已过期：页面/API 必须拒绝并保持 DB 不变。

### Non-goals

- 不自动推荐最佳保留照片。
- 不做全工作区相似照片整理。
- 不处理源图库删除、移动或清理。
- 不用文件名相邻、序号连续或路径排序推断连拍。
- 不提供相似阈值 UI，也不提供宽松/严格模式切换。

### Acceptance Criteria

#### Shared Verification Baseline

- 主路径：复用 `copy_scanned_workspace(tmp_path)` 得到已扫描主基线 workspace，通过现有命名 helper 创建包含至少 2 个已命名人物的导出模板，再从 `/exports` 或对应 API 进入连拍挑选。
- 自定义变体例外：验证“重保存/修图/元数据缺失仍能靠图像内容入组”“文件名相邻但内容不同不能成组”“源文件缺失或不可解码时跳过并计数”时，允许在 `.tmp/burst-pick-export-dedupe/` 或测试 `tmp_path` 下从 `tests/fixtures/people_gallery_scan/` 复制少量图片并生成重编码、轻微亮度变化、EXIF 缺失、相邻序号文件名或删除临时源文件的临时 source，然后对 `copy_scanned_workspace(tmp_path)` 副本执行真实 `source add -> scan start`。例外理由：这些 AC 验证 `visual_fingerprint_v1` 的真实图片内容相似、文件名信号禁用、缺失源文件处理和增量扫描后的候选归属，不能只依赖预扫描主基线，也不得直接改 DB 构造候选。
- 默认断言层级：Playwright DOM + HTTP API + SQLite DB + 文件系统导出结果；调试产物保留到 `.tmp/burst-pick-export-dedupe/`。
- 视觉算法断言：所有验证 `visual_fingerprint_v1` 的测试必须使用测试侧独立参考计算 helper。该 helper 可以复用 Pillow/NumPy 依赖，但不得导入产品视觉指纹实现、不得读取产品视觉指纹缓存、不得信任 API 返回的指标再反推结论。对被断言的相似边，测试必须用源图片重新计算 dHash、16x16 亮度向量 cosine 和颜色直方图交集；`dhash_hamming` 必须精确相等，浮点指标与 API 返回值误差不得超过 `1e-6`，并独立判断该 edge 是否满足 spec 阈值。
- 防 mock 逃逸禁令：不得直接 `INSERT` 放弃标记来满足页面保存 AC；不得用硬编码 fixture 文件名伪造相似组；不得绕过真实 HTTP/WebUI 入口调用内部保存函数；不得 mock `visual_fingerprint_v1` 结果；不得通过直接删除 `export_plan` 来替代真实标记提交。
- 测试运行遵循仓库逐文件策略，优先逐个运行受影响的 backend 测试文件和 `tests/people_gallery/test_webui_*_playwright.py` 文件。

#### AC-1: 模板列表展示连拍挑选入口

- 触发：基于已扫描 workspace 创建一个 active 导出模板后访问 `/exports`。
- 必须可观察：该模板行除现有“预览”“历史”“删除”外，还显示“连拍挑选”入口；链接目标为 `/exports/{template_id}/burst-pick`；invalid 模板仍显示入口但进入后返回可读错误，或入口呈禁用状态并说明不可处理。
- 验证手段：Playwright DOM 断言模板行操作区和链接地址；invalid 子情形通过真实 merge 路径使模板失效后验证。

#### AC-2: 图像内容可以无元数据成组且元数据不是排除门槛

- 触发：覆盖两个子情形：(a) 从主基线某张会命中目标模板的照片生成两张重编码/轻微亮度变化且缺失或改写拍摄时间/设备信息的临时图片，作为新 source 增量扫描；(b) 构造一个已有拍摄时间/设备种子组，并额外加入一张内容相似但设备或拍摄时间不同的重保存照片；随后访问 `GET /api/export-templates/{template_id}/burst-pick`。
- 必须可观察：(a) API 返回至少一个相似组，组内包含原 asset 和这些内容相似的新增 asset；`match_evidence.algorithm` 为 `visual_fingerprint_v1`，并至少有一条连接原 asset 与新增内容相似 asset 的 edge，edge 的 `threshold` 为 `strict` 或 `resave_or_light_edit`，且 dHash Hamming、16x16 亮度向量 cosine、颜色直方图交集满足对应门槛。(b) 内容相似但元数据不同的照片仍被挂到对应相似组；没有任何响应字段或页面文案把设备/时间不一致作为拒绝入组原因，且连接该照片的 edge 不能仅依赖 `metadata_assisted` 门槛。
- 验证手段：服务级集成测试走真实 `source add -> scan start -> GET API`，DB 断言新增 asset 已进入模板候选集，API 断言分组成员集合、`match_evidence.edges[]` schema 和门槛名；测试侧独立参考 helper 对被断言 edge 的源图片重新计算 `visual_fingerprint_v1` 指标，并与 API 指标和阈值做交叉断言；必要时用图像文件 EXIF 差异作为前置断言。

#### AC-3: 连拍挑选只展示当前模板可处理相似组且 GET schema 稳定

- 触发：构造一个模板候选集，其中至少包含一个相似组、一个同模板命中但未成组的普通候选 asset；再通过真实提交入口把某个相似组内的部分 asset 标记为全局放弃；随后访问同模板和另一个会命中该 asset 的模板的 `GET /api/export-templates/{template_id}/burst-pick` 与页面。
- 必须可观察：API 和页面只展示形成相似组的 asset，不展示未成组普通候选；所有展示 asset 都属于该模板当前 preview 候选；已放弃 asset 不参与候选、不参与分组、不在页面展示；如果某个相似组因移除已放弃 asset 后小于成组阈值，该组不再展示；相似组按组内最小 `asset_id` 升序排列，组内照片按 `asset_id` 升序排列；GET API 响应包含并稳定返回 `template_id`、`groups[]`、每组 form-safe `group_key`、每张照片的 `asset_id`、`file_name`、`bucket`、`month`、`context_url`、`is_live`、`groups[].match_evidence.algorithm="visual_fingerprint_v1"`、`groups[].match_evidence.edges[]` 的固定字段和 `diagnostics.skipped_missing_or_unreadable_count`；页面表单为每个当前组渲染重复隐藏字段 `group_key=<group_key>`，且每张候选照片的保留控件使用 `keep_asset_id__<group_key>=<asset_id>`。
- 验证手段：服务级集成测试对 API 分组结果、schema 字段、`group_key` 字符集、`match_evidence.edges[]` 字段类型/排序/连通性和 preview 候选集合做交叉断言；Playwright DOM 断言页面分组、成员集合、顺序和实际表单字段名/值；DB 断言放弃标记存在。

#### AC-4: 不使用文件名相邻或序号连续作为分组依据

- 触发：用临时 source 构造两组对照候选并执行真实增量扫描后访问连拍挑选 API：(a) 两张文件名序号相邻或同目录相邻、但图像内容明显不同且都命中同一模板的照片；(b) 两张图像内容高度相似、但文件名不相邻、不同目录或排序上不相邻且都命中同一模板的照片。
- 必须可观察：(a) 两个内容明显不同的相邻文件不得出现在同一个相似组，即使它们的文件名、目录位置或扫描顺序相邻；测试侧独立参考 helper 重新计算后也显示它们不满足任何 `visual_fingerprint_v1` 门槛。(b) 两个内容高度相似但文件名不相邻的文件必须能出现在同一个相似组，且 `match_evidence.algorithm` 为 `visual_fingerprint_v1`，连接它们的 edge 触发 `strict`、`resave_or_light_edit` 或 `metadata_assisted` 门槛之一；对应组的 `match_evidence` 只能作为辅助诊断，不能替代组成员集合断言。
- 验证手段：服务级集成测试走真实 `source add -> scan start -> GET API`；用 API 响应中的组成员集合分别断言负向和正向对照；测试侧独立参考 helper 对负向和正向 pair 分别重新计算 `visual_fingerprint_v1` 指标，断言负向 pair 不满足任何门槛，正向 edge 的 API 指标与参考指标一致且满足 spec 阈值；不把 `match_evidence` 文案作为唯一通过条件。

#### AC-5: 页面默认不保留且每组必须至少选择一张保留

- 触发：进入 `/exports/{template_id}/burst-pick`，不选择任何保留照片直接提交。
- 必须可观察：页面中每组所有照片默认都不是“保留”选中态；提交被拒绝，显示每个相似组至少保留 1 张的可读错误；DB 中全局放弃标记和 `export_plan` 均不变化。
- 验证手段：Playwright 检查表单初始状态、提交结果和错误文案；DB 行数前后对比。

#### AC-6: 提交保留选择后写入全局放弃标记且返回稳定 schema

- 触发：使用两个彼此独立的 fresh workspace/state 子情形验证成功路径：(a) 在页面中每个相似组至少选择 1 张要保留的照片并提交页面级表单；(b) 在另一个 fresh state 中用同等语义的 JSON 请求提交 `POST /api/export-templates/{template_id}/burst-pick`。
- 必须可观察：每个组中未被选中保留的 asset 都写入全局放弃导出标记；选中保留的 asset 不写入；页面返回成功反馈或跳回连拍挑选页且已放弃 asset 不再作为可选项出现；API 成功响应稳定包含 `abandoned_asset_ids`、`kept_asset_ids`、`created_count` 和 `already_abandoned_count`，其中 `created_count` 与本次新写入的 DB 唯一标记数量一致，fresh state 成功提交时 `already_abandoned_count` 为 0。
- 验证手段：Playwright 通过真实页面表单提交并断言 DB 全局放弃表的 asset 集合、唯一性和追溯字段；服务级集成测试在独立 fresh state 中通过真实 API endpoint 提交同等 JSON，并断言响应 schema、计数和 DB 状态；旧页面表单或旧 API payload 的重放不属于成功路径，按 AC-7 的 stale/过期提交验收。

#### AC-7: API 和 Web 表单提交契约校验 stale 和非法选择

- 触发：分别对 `POST /api/export-templates/{template_id}/burst-pick` 和 `POST /exports/{template_id}/burst-pick` 发起三类请求：(a) 缺少某个当前相似组；(b) 某组 `keep_asset_ids` 或 `keep_asset_id__<group_key>` 为空；(c) 某组包含不属于该组、属于另一组、已经不在当前候选中或使用过期 `group_key` 的 asset。
- 必须可观察：API 每类请求都返回 `400` 和可读错误；Web 表单每类 forged/stale 提交都返回可读错误页面或带错误的原页面，且不会写入成功反馈；全局放弃导出标记、`export_plan` 和源图库均不变化。
- 验证手段：服务级集成测试走真实 API endpoint，Playwright 或测试客户端走真实 Web POST endpoint 构造 forged/stale 表单；提交前后做 DB 行数和关键字段断言。

#### AC-8: 后续所有模板预览和 export_plan 跳过已放弃 asset

- 触发：通过 AC-6 的真实提交入口标记一批 asset 后，分别访问原模板和另一个也会命中这些 asset 的模板的 `GET /api/export-templates/{template_id}/preview`。
- 必须可观察：两个模板的 preview JSON、页面 DOM 和 `export_plan` 都不包含已放弃 asset；preview 总数、only/group 数量同步减少；未被放弃的同组保留 asset 仍可出现。
- 验证手段：服务级集成测试 + Playwright DOM + DB `export_plan` 断言。

#### AC-9: 旧 export_plan 不能让已放弃 asset 再次导出

- 触发：先对模板访问 preview 写入 `export_plan`，确认计划中包含某个待放弃 asset；随后通过连拍挑选提交把该 asset 标记为全局放弃；不再次访问 preview，直接调用 `POST /api/export-templates/{template_id}/execute`。
- 必须可观察：导出运行完成后，目标文件系统和 `export_delivery` 都不包含该已放弃 asset；如果实现选择在提交标记时清理旧 `export_plan`，DB 中旧计划行也应消失；如果旧计划行暂时存在，execute 必须防御性跳过。
- 验证手段：服务级集成测试走真实 preview、真实连拍提交、真实 execute；断言文件系统、`export_delivery` 和 `export_plan` 状态。

#### AC-10: 空状态、缺失文件和 invalid 模板边界

- 触发：(a) 对没有相似组的 active 模板访问连拍挑选页面/API；(b) 对 invalid 或不存在模板访问页面/API；(c) 对临时 source 扫描完成后删除其中一个候选源文件，再访问连拍挑选 API。
- 必须可观察：(a) 页面展示空状态且 API 返回空 `groups`，不写入标记；(b) 返回可读错误且不写入标记；(c) 缺失源文件对应 asset 被跳过，响应诊断摘要计入跳过数量，其他可处理 asset 仍可正常分组，整个请求不返回 500。
- 验证手段：Playwright 覆盖空状态和 invalid 页面；服务级集成测试覆盖不存在模板和缺失源文件 API 诊断。

#### AC-11: 视觉指纹缓存版本和坏缓存不会污染分组

- 触发：通过真实 HTTP/API 入口访问一次连拍挑选，随后根据实现形态覆盖缓存边界：(a) 若实现新增视觉指纹缓存表，则在测试 workspace 中把一个当前候选 asset 的缓存算法版本改成非 `visual_fingerprint_v1`，并把另一个当前候选 asset 的缓存内容改成不可解码值，再次访问 `GET /api/export-templates/{template_id}/burst-pick`；(b) 若实现不使用视觉指纹缓存表，则连续两次访问同一 API。
- 必须可观察：(a) 版本不匹配或坏缓存不会被当成有效视觉指纹使用；系统从源文件重新计算 `visual_fingerprint_v1`，分组结果仍与独立参考 helper 判断一致，API `match_evidence.edges[]` 指标与重新计算结果一致；可恢复的坏缓存不会写入放弃标记或 `export_plan` 变更。(b) 无缓存实现仍返回与独立参考 helper 一致的分组和 `match_evidence.edges[]` 指标，不要求制造缓存损坏。
- 验证手段：服务级集成测试走真实 HTTP/API endpoint；缓存表存在时允许用 SQL 直接制造“旧版本/坏缓存”内部故障条件，但成功结果必须通过真实 API 重新计算产生；测试侧独立参考 helper 重新计算相关 edge 指标并与 API 响应交叉断言；DB 断言全局放弃标记和 `export_plan` 不变化。
- 降级理由：缓存版本不匹配和缓存内容损坏属于内部持久化故障，普通用户输入难以稳定触发；直接修改测试 workspace 的缓存行只用于制造失败条件，不得伪造成功分组、成功指标或绕过公共请求路径。

#### AC-12: 视觉特征准备失败不产生部分写入

- 触发：通过真实 HTTP/API 入口访问连拍挑选，同时让视觉特征计算、视觉特征缓存写入或相关 DB 事务稳定失败。
- 必须可观察：请求返回可读错误；没有写入部分视觉特征缓存结果、全局放弃导出标记或 `export_plan` 变更；后续恢复故障后重新访问可以正常计算。
- 验证手段：服务级集成测试使用临时 workspace 的受控 DB 故障或测试注入制造视觉特征准备失败，但请求仍必须走真实 HTTP/API 入口，并用 DB 断言原子性。
- 降级理由：视觉特征准备失败属于内部计算或持久化故障，难以通过普通用户输入稳定触发；受控故障注入只用于制造失败条件，不得伪造成功结果或绕过公共请求路径。

#### AC-13: 源图库与历史账本保持不变

- 触发：提交全局放弃标记后检查 workspace 和源图片目录。
- 必须可观察：源图片文件仍存在且内容未变；`assets`、`face_observations`、`person_face_assignments` 和既有 `export_delivery` 历史行不被删除；只有全局放弃标记及必要的 `export_plan` 过滤结果发生变化。
- 验证手段：文件 hash/mtime 对比 + DB 表行数和关键字段断言。

### Done When

- 所有 Acceptance Criteria 都通过对应的“验证手段”完成自动化验证。
- 没有核心需求是通过直接状态修改、硬编码数据、占位行为或 fake integration 满足的。
