# 模板连拍多特征召回增强 Spec

## Parent Spec

- Parent Spec: [2026-05-23-burst-pick-export-dedupe-spec.md](./2026-05-23-burst-pick-export-dedupe-spec.md)
- Current Approval Scope: 本 child spec 负责提升 [Child A](./2026-05-23-burst-pick-export-dedupe-child-a-template-burst-pick-spec.md) 已交付的模板连拍挑选分组召回率；交付后替换 Child A 的 `visual_fingerprint_v1` 和旧 `groups[].match_evidence.edges[]` API evidence 合同，但不改变异步任务、持久化分组、每组独立提交、全局放弃导出标记和原图幻灯片的产品交互语义。

## Global Constraints

- Shared constraints: 继承父 spec 的共享约束，见 [2026-05-23-burst-pick-export-dedupe-spec.md](./2026-05-23-burst-pick-export-dedupe-spec.md) 的 `Global Constraints`。
- 本 child spec 的验收测试不得使用真实 workspace、`.hikbox/test_workspace`、用户本机照片库或其中的 asset id；真实 workspace 中发现的漏召回案例只能作为调研背景，不能作为自动化验收输入。
- 本 child spec 只交付多特征规则增强；不得引入 Vision FeaturePrint、Core ML、DINO、CLIP、ONNX 图像 embedding、ANN 索引或需要下载的新图像模型。
- 本 child spec 必须扩展 DB 持久化结构来保存新 edge evidence schema；可以选择新增 edge 表、扩展现有 edge 表，或新增视觉特征缓存表。任何 DB schema 修改都必须新增 migration 并同步更新 `docs/db_schema.md`。
- 本 child spec 允许直接升级 `GET /api/export-templates/{template_id}/burst-pick` 的 `match_evidence` schema；已有测试应改为断言新 schema，不要求保留 [Child A](./2026-05-23-burst-pick-export-dedupe-child-a-template-burst-pick-spec.md) 的旧 edge 字段或兼容映射。
- 本 child spec 与 [Child A](./2026-05-23-burst-pick-export-dedupe-child-a-template-burst-pick-spec.md) 的关系是 evidence 合同替换，不是双 schema 兼容；Child C 交付后的最终 API evidence 以 `visual_fingerprint_v2_multifeature_recall`、`strong_edges[]` 和可选 `weak_edges[]` 为准。

## Feature Slice 1: 多特征规则分组提高召回

- [x] Implementation status: Done

### Behavior

- 连拍挑选后台任务使用新的算法版本，例如 `visual_fingerprint_v2_multifeature_recall`；算法版本必须不同于 [Child A](./2026-05-23-burst-pick-export-dedupe-child-a-template-burst-pick-spec.md) 已实现版本，避免复用旧 run 的低召回分组结果。
- 识别范围仍是单个导出模板当前可导出候选集：已全局放弃的 asset 不参与候选、不参与特征计算、不参与分组；候选 only/group、人物命中和月份语义继承 [Child A](./2026-05-23-burst-pick-export-dedupe-child-a-template-burst-pick-spec.md)。
- 单图视觉特征升级为多特征组合，图像预处理固定继承 [Child A](./2026-05-23-burst-pick-export-dedupe-child-a-template-burst-pick-spec.md)：用 Pillow/pillow-heif 解码源图片，先应用 `ImageOps.exif_transpose`，再 `convert("RGB")`，所有哈希输入均从该 RGB 图派生；需要亮度图时固定使用 `convert("L")`。具体特征固定使用以下确定性算法：
  - `global_dhash`：继承当前 128-bit dHash，用于近重复和连续拍摄的全局结构变化判断。
  - `global_phash`：对方向修正后的亮度图计算 63-bit DCT pHash。亮度图用 Pillow `Image.Resampling.BILINEAR` 缩放到 32x32，像素按 row-major 转为 `float64`，取值范围保持 `0..255`，不做均值中心化。二维 DCT-II 固定公式为 `C[u,v] = alpha(u,N) * alpha(v,N) * sum(y=0..N-1) sum(x=0..N-1) pixel[y,x] * cos(pi * (2x + 1) * u / (2N)) * cos(pi * (2y + 1) * v / (2N))`，其中 `N=32`，`alpha(0,N)=sqrt(1/N)`，`alpha(k,N)=sqrt(2/N)`。取左上 8x8 系数并排除 DC 系数，用剩余 63 个系数的中位数生成 63 个 bit；系数 `>` 中位数记 1，`<=` 中位数记 0。
  - `center_phash`：取方向修正后图像中心 75% 区域，使用与 `global_phash` 相同的 63-bit DCT pHash 算法，用于轻裁剪、边框变化和边缘干扰。中心裁剪固定为：`crop_width=max(1, round(width * 0.75))`、`crop_height=max(1, round(height * 0.75))`、`left=(width - crop_width) // 2`、`top=(height - crop_height) // 2`、`right=left + crop_width`、`bottom=top + crop_height`。
  - `block_phash`：将方向修正后的图像按宽高各 4 等分切成 4x4 区域；第 `col,row` 个 block 使用 `left=floor(col * width / 4)`、`right=floor((col + 1) * width / 4)`、`top=floor(row * height / 4)`、`bottom=floor((row + 1) * height / 4)`，并保证 `right > left`、`bottom > top`。每个区域用 Pillow `Image.Resampling.BILINEAR` 缩放到 16x16，按 `global_phash` 的 DCT-II 公式计算但 `N=16`，取左上 4x4 系数并排除 DC 系数，用剩余 15 个系数的中位数生成 15-bit block hash；系数 `>` 中位数记 1，`<=` 中位数记 0。pair 比较时先计算每个 block 到另一张图全部 16 个 block 的最小 Hamming 距离；单向 match ratio 为最小距离 `<= 4` 的 block 数量除以 16，最终 `block_match_ratio` 为两个方向单向 ratio 的较小值。
  - 基础元数据：归一化设备厂商和型号、图像宽高、文件大小，以及固定规则选择出的 `event_time`。元数据只能作为正向辅助信号，不能替代视觉内容相似。
- `event_time` 是所有时间窗口和 `capture_time_delta_seconds` 的唯一时间来源，固定按以下优先级选择：
  - 首选 EXIF `DateTimeOriginal`，其次 `DateTimeDigitized`，再次 `DateTime`；支持格式固定为 `%Y:%m:%d %H:%M:%S` 和 `%Y-%m-%d %H:%M:%S`。
  - EXIF 时间按相机本地墙上时间处理，不做时区换算；pair delta 为两个 naive datetime 的绝对秒差。
  - 如果三个 EXIF 时间都不存在或不可解析，则使用源文件 `st_mtime` 转成 UTC naive datetime 作为 fallback；测试需要控制 fallback 时间时必须通过 `os.utime` 或等价文件系统方式设置 mtime。
  - `capture_time_delta_seconds` 固定为两个 asset 的 `event_time` 绝对秒差；只有任一 asset 的源文件缺失或不可读取，导致 `event_time` 无法获得时才为 `null`。
- 感知哈希和 block 匹配必须在测试侧可独立重算；实现可以使用 NumPy 或等价代码计算 DCT，但不得换成未在本 spec 中定义的哈希位宽、中心裁剪比例或 block 匹配规则。
- 候选 pair 生成默认在当前模板候选集中做全量轻量 pair 扫描；当候选规模超过实现设定的性能阈值时，可以使用时间、哈希桶或特征前缀 blocking，但 blocking 至少包含视觉特征派生入口，不能只依赖文件名、目录、扫描顺序、EXIF、时间或设备。
- 每个候选 pair 必须分类为以下之一：
  - `exact_duplicate`：同一图片的直接副本、重编码但像素视觉基本不变的版本，或压缩重复版本；这类 pair 必须满足最严格的全局和中心哈希门槛。
  - `edited_duplicate`：轻微调色、明显重存差异、轻裁剪、边框变化或局部遮挡后的重复版本；这类 pair 不要求达到 `exact_duplicate` 的最严格门槛。
  - `burst_duplicate`：短时间连续拍摄或同一拍摄动作中的相似照片。
  - `same_scene_similar`：同场景相似但不够确定属于可清理重复或连拍的弱相似。
  - `different`：不相似或证据不足。
- pair 分类必须至少满足以下公开规则；实现可以在不降低验收召回的前提下增加更严格的误聚保护，但不得让未满足任何强边规则的 pair 进入可提交组：
  - `exact_duplicate` 强边：满足 `phash_hamming <= 4`、`dhash_hamming <= 8`、`center_phash_hamming <= 6`，或满足 `phash_hamming <= 3` 且 `block_match_ratio >= 0.875`。`confidence` 必须 `>= 0.95`。
  - `edited_duplicate` 强边：满足以下任一组合，且 `confidence` 必须 `>= 0.85`：(a) `phash_hamming <= 12`、`center_phash_hamming <= 14` 且 `block_match_ratio >= 0.50`；(b) `center_phash_hamming <= 10`、`block_match_ratio >= 0.50` 且 `dhash_hamming <= 32`；(c) `block_match_ratio >= 0.625` 且满足 `phash_hamming <= 18` 或 `center_phash_hamming <= 18`。
  - `burst_duplicate` 强连拍窗口：可用时间差 `<= 10` 秒，且满足 `dhash_hamming <= 30`、`phash_hamming <= 28`、`center_phash_hamming <= 26`、`block_match_ratio >= 0.3125` 中至少两个视觉条件。`confidence` 必须 `>= 0.78`；设备一致可以提高置信度，但设备缺失不能把满足视觉条件的 pair 降为 `different`。
  - `burst_duplicate` 强边连续场景窗口：可用时间差 `> 10` 秒且 `<= 60` 秒，并满足 `phash_hamming <= 16` 或 `center_phash_hamming <= 14` 或 `block_match_ratio >= 0.50` 中至少一个强视觉条件，同时满足 `dhash_hamming <= 30` 或 `block_match_ratio >= 0.50` 中至少一个辅助条件。`confidence` 必须 `>= 0.82`。
  - `same_scene_similar` 弱边：可用时间差 `<= 300` 秒，且满足 `phash_hamming <= 32`、`center_phash_hamming <= 30`、`block_match_ratio >= 0.25` 中至少一个条件，但不满足任何强边规则。`confidence` 必须 `< 0.78`，不得参与强边聚类。
  - `different`：不满足以上规则，或虽然时间/设备接近但视觉条件不足。
- 分类优先级固定为 `exact_duplicate` > `edited_duplicate` > `burst_duplicate` > `same_scene_similar` > `different`。如果一个 pair 同时满足多个规则，必须选择优先级最高的类型；测试中用于验证 `edited_duplicate` 或 `burst_duplicate` 的样本必须构造成不满足更高优先级规则。
- 默认策略偏召回：对明显相似、轻裁剪、局部变化和短时间连续拍摄的照片，应优先展示给用户挑选；误报由用户在每组内选择全部保留或不提交该组处理。
- 只有 `exact_duplicate`、`edited_duplicate` 和 `burst_duplicate` 作为强边进入相似组聚类；`same_scene_similar` 只能作为诊断或弱相似提示，不得单独形成可提交的放弃组。
- `burst_duplicate` 的时间窗口固定为：
  - 强连拍窗口：拍摄时间差或可用替代时间差 `<= 10` 秒。此窗口内，视觉多特征达到连拍门槛即可形成 `burst_duplicate` 强边；设备一致是增强置信度的正向证据，但设备缺失不能单独阻止视觉证据足够强的 pair 入组。
  - 连续场景窗口：时间差 `> 10` 秒且 `<= 60` 秒。此窗口内必须满足比强连拍窗口更强的视觉门槛，才能形成 `burst_duplicate` 强边；否则最多分类为 `same_scene_similar`。
  - 超出连续窗口：时间差 `> 60` 秒时，不得只凭连续拍摄语义形成 `burst_duplicate`；只有满足 `exact_duplicate` 或 `edited_duplicate` 这类与时间无关的强重复规则时，才能形成强边。
- 聚类使用强边连通分量，并在输出前做后验校验，防止链式误聚：
  - 组类型：按组内强边数量最多的 `edge_type` 作为主类型；数量相同时按 `exact_duplicate`、`edited_duplicate`、`burst_duplicate` 的顺序取更严格类型。
  - 中心图校验：medoid 固定为组内“直接强边数量最多”的 asset；数量相同时取强边 `confidence` 总和最高者，再相同取最小 `asset_id`。主类型为 `exact_duplicate` 或 `edited_duplicate` 时，每个非 medoid 成员必须与 medoid 满足 `phash_hamming <= 18` 或 `center_phash_hamming <= 18` 或 `block_match_ratio >= 0.50`。主类型为 `burst_duplicate` 时，每个非 medoid 成员必须与 medoid 满足 `phash_hamming <= 32` 或 `center_phash_hamming <= 30` 或 `block_match_ratio >= 0.25`，且如果双方都有可用时间，二者时间差必须 `<= 300` 秒。
  - 时间跨度校验：主要由 `burst_duplicate` 边组成的组，整体时间跨度默认不得超过 5 分钟；超过时必须存在覆盖所有超跨度成员的 `exact_duplicate` 或 `edited_duplicate` 强边证据，否则拆组。
  - 边密度校验：`strong_edge_density = strong_edges / (n * (n - 1) / 2)`；`n <= 5` 时必须 `>= 0.40`，`n > 5` 时必须 `>= 0.25`。不满足时删除最低 `confidence` 强边后重新取连通分量并重新校验；若仍不满足，丢弃不满足校验的组件。
- 分组结果仍稳定排序：组按组内最小 `asset_id` 升序展示，组内照片按 `asset_id` 升序展示，除非后续 spec 明确改为按拍摄时间或 medoid 排序。

### Public Interface

- Web 页面入口仍为 `/exports/{template_id}/burst-pick`；页面继续展示后台处理状态，完成后展示原图幻灯片，仍按每个相似组独立提交保留选择。
- API 入口仍为 `GET /api/export-templates/{template_id}/burst-pick`；`status="completed"` 后从 DB 持久化结果读取新算法分组，不在 GET 请求线程内重新计算。
- API `groups[].match_evidence` 使用新 schema；不要求保留 [Child A](./2026-05-23-burst-pick-export-dedupe-child-a-template-burst-pick-spec.md) 旧 edge 字段。新 schema 至少包含：
  ```json
  {
    "algorithm": "visual_fingerprint_v2_multifeature_recall",
    "strong_edges": [
      {
        "asset_ids": [123, 456],
        "edge_type": "burst_duplicate",
        "confidence": 0.91,
        "phash_hamming": 14,
        "dhash_hamming": 22,
        "center_phash_hamming": 11,
        "block_match_ratio": 0.5,
        "capture_time_delta_seconds": 8.0,
        "normalized_device_match": true
      }
    ]
  }
  ```
- `strong_edges[]` 只能包含实际参与组连通和后验校验的强边；不得包含 `different` 边。`same_scene_similar` 弱边如需返回，必须放在 `weak_edges[]` 或诊断字段中，且不得让弱边端点成为可提交组成员。
- `strong_edges[]` 固定按 `asset_ids[0]` 升序、`asset_ids[1]` 升序、`edge_type` 字典序排序；每个 `asset_ids` 都必须是长度为 2 的升序整数数组。实现可以不返回 `weak_edges[]`；若返回，`weak_edges[]` 使用与 `strong_edges[]` 相同的字段和排序规则，但 `edge_type` 固定为 `same_scene_similar`。
- 每条强边必须包含足以解释分类结果的特征指标：`asset_ids`、`edge_type`、`confidence`、`phash_hamming`、`dhash_hamming`、`center_phash_hamming`、`block_match_ratio`、`capture_time_delta_seconds`、`normalized_device_match`。实现可以增加字段，但不得省略这些字段。
- `edge_type` 只能取 `exact_duplicate`、`edited_duplicate`、`burst_duplicate`；`confidence` 为 `0.0` 到 `1.0` 的数字，排序和展示不依赖它，但测试可用于断言强边置信度高于弱边或 rejected edge。
- `capture_time_delta_seconds` 在双方都有 `event_time` 时为非负数字；任一方因源文件缺失或不可读取导致 `event_time` 无法获得时为 `null`。`normalized_device_match` 在双方都有归一化设备信息时为布尔值；任一方缺少设备信息时为 `null`。
- DB 持久化必须记录新算法版本、组成员和强边指标，至少能完整还原 API `strong_edges[]` 的必填字段。若新增视觉特征缓存表，缓存至少要记录 asset id、算法版本、源文件校验信息和特征 payload；算法版本不匹配、源文件校验变化或 payload 不可解码时，不得使用旧缓存。
- `POST /api/export-templates/{template_id}/burst-pick` 和 `POST /exports/{template_id}/burst-pick` 的提交契约继承 [Child A](./2026-05-23-burst-pick-export-dedupe-child-a-template-burst-pick-spec.md)：每次只提交一个相似组，服务端基于 DB 持久化结果校验 `group_key` 和 `keep_asset_ids`。

### Error and Boundary Cases

- 模板不存在、模板 invalid、模板未关联人物、候选集为空、没有相似组、源图片缺失或不可解码的行为继承 [Child A](./2026-05-23-burst-pick-export-dedupe-child-a-template-burst-pick-spec.md)。
- 多特征计算失败、特征缓存写入失败或分组结果持久化失败时，任务必须进入 failed 或返回可读错误；不得写入部分 group、部分 edge 或全局放弃导出标记。
- 特征缓存版本不匹配、源文件校验变化、缓存 payload 不可解码时，必须重新计算；如果重新计算失败，按特征计算失败处理。
- 文件名相邻、目录相邻、扫描顺序相邻、文件大小接近、设备一致或时间接近都不能单独产生相似边。
- `same_scene_similar` 弱边不能形成可提交组；如果页面或 API 展示弱相似诊断，必须与可提交强边组区分，避免用户误以为提交会处理弱边端点。
- 组内某些 asset 已被全局放弃后，后续新 run 必须重新过滤候选；若剩余成员无法满足强边聚类和后验校验，该组不再展示。

### Non-goals

- 不引入图像 embedding、Vision FeaturePrint、Core ML、DINO、CLIP、ONNX 图像模型或 ANN/HNSW 图像索引。
- 不自动推荐最佳保留照片，不计算图像质量分、美学分或人脸表情分。
- 不新增阈值 UI、宽松/严格模式切换或用户自定义算法配置。
- 不新增全工作区相似照片整理入口；识别范围仍从单个导出模板出发。
- 不改变全局放弃导出标记的含义，不删除源图库文件，不移动源文件。
- 不实现 Child B 的既有导出目录清理脚本。

### Next Optimization Direction

- 后续可以单独新增 child spec 引入图像 embedding 或 Vision FeaturePrint，作为复杂裁剪、构图变化、局部遮挡和同场景相似的补充召回入口。
- embedding 方向必须单独定义模型来源、缓存格式、阈值标定方法、跨平台可用性、性能预算、误聚控制和验收样本矩阵。
- 本 child spec 不为 embedding 写入不可验证的占位字段、空表、no-op 代码路径或 UI 暗入口。

### Acceptance Criteria

#### Shared Verification Baseline

- 主路径：复用 `copy_scanned_workspace(tmp_path)` 得到已扫描主基线 workspace，通过现有 helper 创建 active 导出模板，再从 `/exports` 或对应 API 进入连拍挑选。
- 自定义变体例外：验证重存、轻编辑、轻裁剪、边缘变化、局部遮挡、短时间连续拍摄、文件名相邻负例和链式误聚时，允许在测试 `tmp_path` 或 `.tmp/burst-pick-export-dedupe/` 下从 `tests/fixtures/people_gallery_scan/` 复制少量图片并生成临时 source，然后对 `copy_scanned_workspace(tmp_path)` 副本执行真实 `source add -> scan start`。例外理由：这些 AC 需要真实图片内容、真实扫描和真实模板候选集来验证召回，不得用真实 workspace 或直接改 DB 伪造候选。
- 禁止验收输入：不得读取 `.hikbox/test_workspace`、用户本机照片目录或真实 workspace DB；不得把调研中发现的真实 asset id 写入测试断言。
- 默认断言层级：服务级 HTTP API + SQLite DB + Playwright DOM；算法边界用纯函数单元测试覆盖，但不能替代至少一个通过真实 API 观察到新 schema 和新分组的集成测试。
- 独立参考 helper：验证多特征指标的测试必须用测试侧独立 helper 从源图片重新计算 pHash/dHash/center pHash/block match，不得导入产品多特征实现、不得读取产品缓存后反推结论、不得只相信 API 返回值。
- 防 mock 逃逸禁令：不得 mock 成功分组、不得直接 `INSERT` group/edge 满足 API AC、不得用硬编码文件名或 asset id 伪造相似组、不得通过直接写全局放弃标记绕过真实提交入口。
- 测试运行遵循仓库逐文件策略，优先逐个运行受影响的 backend 测试文件和 `tests/people_gallery/test_webui_*_playwright.py` 文件。

#### AC-1: 新算法版本启动新 run 并持久化新 evidence schema

- 触发：在 fresh `copy_scanned_workspace(tmp_path)` 中创建 active 导出模板，首次访问 `GET /api/export-templates/{template_id}/burst-pick`，等待后台任务完成；随后再次访问同一 API。
- 必须可观察：首次访问立即返回或展示 `running` 状态并启动新 run；完成后 DB 中 `export_burst_pick_run.algorithm_version` 为新算法版本，且不等于旧版本；完成后的 API 从 DB 返回 `status="completed"`、`run_id` 和 `match_evidence.algorithm="visual_fingerprint_v2_multifeature_recall"`（如果当前 fixture 恰好有相似组）；第二次访问复用同版本 completed run，不重新创建相同版本的新 run。非空 `groups[]`、新 `strong_edges[]` schema 和 edge 持久化由 AC-2、AC-3、AC-4、AC-8 的稳定自定义变体样本验收。
- 验证手段：服务级集成测试走真实 HTTP API，轮询完成；SQLite 断言 run 版本、run 数量和复用语义；不依赖主基线 fixture 偶然产生相似组。

#### AC-2: 直接副本和视觉基本不变的重编码通过 `exact_duplicate` 入组

- 触发：从主基线 fixture 中选择一张会命中模板的照片，在临时 source 中生成直接副本和视觉基本不变的重编码版本，执行真实 `source add -> scan start` 后访问连拍挑选 API。
- 必须可观察：原图、直接副本和视觉基本不变的重编码版本都出现在同一个相似组；连接原图与直接副本、原图与重编码版本的强边均为 `edge_type="exact_duplicate"`，`confidence >= 0.95`，并满足 `exact_duplicate` 公开规则；该组可被页面展示为可提交组。
- 验证手段：服务级集成测试断言 API 组成员和强边类型；测试侧独立参考 helper 重算多特征指标并与 API 返回值交叉验证；Playwright 可在 AC-8 统一覆盖页面展示。

#### AC-3: 轻编辑、轻裁剪、边缘变化或局部遮挡通过 `edited_duplicate` 入组

- 触发：从同一张 fixture 照片派生四类临时 source 样本并真实扫描：轻微亮度或颜色变化版本、轻裁剪版本、增加边缘干扰版本、局部遮挡版本。
- 必须可观察：四类派生图都分别与原图或同源派生图出现在相似组中；连接它们的强边都为 `edge_type="edited_duplicate"`，`confidence >= 0.85`，满足 `edited_duplicate` 公开规则，并且测试侧独立 helper 确认这些 pair 不满足 `exact_duplicate` 规则；证据中 `center_phash_hamming` 或 `block_match_ratio` 是触发入组的主要解释字段，不能只靠时间、设备或文件名。
- 验证手段：服务级集成测试断言分组和强边；测试侧独立 helper 重算 center pHash 和 block match；负向检查响应中没有把文件名、目录或扫描顺序作为 edge 证据。

#### AC-4: 短时间连续拍摄窗口按 10 秒和 60 秒分档

- 触发：构造四类临时 source 样本并真实扫描：(a) 同设备或设备缺失、可用时间差 `<= 10` 秒、视觉多特征满足强连拍门槛；(b) 可用时间差 `> 10` 秒且 `<= 60` 秒，视觉证据满足连续场景窗口更强门槛；(c) 可用时间差 `> 10` 秒且 `<= 60` 秒，但视觉证据只满足强连拍窗口门槛、不满足连续场景窗口更强门槛；(d) 可用时间差 `> 60` 秒，视觉证据只达到连拍门槛但不达到 `exact_duplicate` 或 `edited_duplicate`。
- 必须可观察：(a) 返回 `burst_duplicate` 强边；(b) 返回 `burst_duplicate` 强边；(c) 不得形成 `burst_duplicate` 强边，最多作为 `same_scene_similar` 弱诊断；(d) 不得返回 `burst_duplicate` 强边，也不得仅因为时间或设备形成可提交组。
- 验证手段：算法级单元测试覆盖三档时间窗口和阈值分支；服务级集成测试通过真实 API 覆盖 (a)、(b)、(c)、(d) 四类公共行为，断言 edge 类型、组成员和 `capture_time_delta_seconds`。

#### AC-4.1: `event_time` 来源优先级和 null 边界稳定

- 触发：构造五类临时 source 样本并真实扫描：(a) 同一图片的两个派生文件同时带有相互冲突的 EXIF `DateTimeOriginal`、`DateTimeDigitized`、`DateTime` 和文件 mtime；(b) 只提供 `DateTimeDigitized`，不提供 `DateTimeOriginal`；(c) 只提供 `DateTime`，不提供 `DateTimeOriginal` 和 `DateTimeDigitized`；(d) EXIF 时间缺失或不可解析，但通过 `os.utime` 设置文件 mtime；(e) 扫描完成后删除其中一个候选源文件，再访问连拍挑选 API。
- 必须可观察：(a) `capture_time_delta_seconds` 来自 `DateTimeOriginal`，不是其他 EXIF 字段或 mtime；(b) `capture_time_delta_seconds` 来自 `DateTimeDigitized`；(c) `capture_time_delta_seconds` 来自 `DateTime`；(d) `capture_time_delta_seconds` 来自文件 mtime fallback；(e) 源文件缺失的 asset 被跳过或其相关 edge 的 `capture_time_delta_seconds` 为 `null`；无论采用哪种实现方式，都不得因为缺失源文件返回 500 或写入部分分组。
- 验证手段：服务级集成测试通过真实 `source add -> scan start -> GET API` 入口覆盖 (a)、(b)、(c)、(d)、(e)；测试侧读取派生文件 EXIF 和 mtime 作为前置断言，再断言 API edge 的 `capture_time_delta_seconds` 或诊断跳过数量。

#### AC-5: 文件名相邻、时间接近或设备一致但内容不同不得入组

- 触发：用临时 source 构造两张文件名序号相邻、同目录相邻、时间差 `<= 10` 秒且设备信息相同或缺失的图片，但图像内容明显不同；真实扫描后访问连拍挑选 API。
- 必须可观察：这两张图片不得出现在同一个相似组；API `strong_edges[]` 不包含这对 asset；如果实现返回弱相似或 rejected 诊断，该 pair 必须被分类为 `different` 或不满足强边门槛。
- 验证手段：服务级集成测试走真实 API；测试侧独立 helper 重算视觉特征，断言 pair 不满足 exact、edited 或 burst 强边规则；DB edge 表不包含该 pair 的强边。

#### AC-6: 链式弱相似不能把低密度长跨度样本误聚成大组

- 触发：构造 A/B/C 或 A/B/C/D 临时样本，使相邻 pair 具有弱相似或低置信连续场景证据，但首尾 pair 不满足中心图校验、时间跨度超过 burst 组限制或强边密度低于要求；真实扫描后访问连拍挑选 API。
- 必须可观察：系统不会输出包含整条链的一个大可提交组；结果要么拆成更小的强相似子组，要么只在诊断中记录弱相似，不让弱相似端点进入可提交组；任何输出组都满足中心图、时间跨度和边密度后验校验。
- 验证手段：算法级聚类单元测试覆盖弱链拆分和边密度；服务级集成测试覆盖至少一个真实图片派生链式负例，并用 API 组成员集合和 `strong_edges[]` 断言没有大组误聚。

#### AC-7: 特征缓存版本、源文件变化和坏缓存不会污染结果

- 触发：如果实现新增视觉特征缓存表，先对模板 A 通过真实 API 完成一次 run，再在测试 DB 中把某个候选 asset 的缓存版本改旧、源文件校验改成不匹配或 payload 改成不可解码；随后通过真实模板创建入口创建选择同一人物集合的模板 B，并访问模板 B 的连拍挑选 API，以公共入口触发一个独立的新算法 run。不得通过直接修改模板 A 的 run 状态来制造新 run。如果实现不新增缓存表，则连续两次访问同一 API 并创建模板 B 访问一次新 run。
- 必须可观察：缓存存在时，版本不匹配、源文件校验变化或坏 payload 不会被当作有效特征使用；系统重新计算并返回与独立参考 helper 一致的分组，或在源文件无法重新计算时返回可读失败且不写入部分组。无缓存实现仍稳定返回新 schema 的分组和边证据。
- 验证手段：服务级集成测试走真实 API；缓存表存在时允许用 SQL 只制造内部故障条件，不得伪造成功分组或指标；DB 断言没有部分 group/edge 或全局放弃标记写入。
- 降级理由：缓存损坏属于内部持久化故障，普通用户输入难以稳定触发；SQL 只用于制造失败条件，成功结果必须由真实 API 重新计算产生。

#### AC-8: Web 页面使用新 schema 展示并保持单组提交

- 触发：准备至少两个由新多特征规则召回的可提交相似组，访问 `/exports/{template_id}/burst-pick`，等待后台任务完成，在页面中选择其中一个组的保留照片并提交该组。
- 必须可观察：页面不因旧 edge 字段缺失而报错；完成后展示原图幻灯片和新算法相似组；每组仍有独立 `group_key` 和保留控件；提交一个组后仅该组未保留 asset 写入全局放弃标记，其他未提交组仍可继续处理。
- 验证手段：Playwright 测试走真实 Web 页面和表单提交；SQLite 断言 `export_abandoned_asset`、`export_burst_pick_group.submitted_at` 和未提交组状态；测试不读取真实 workspace。

#### AC-9: DB migration 和 schema 文档同步

- 触发：从旧 schema workspace 启动或运行 migration 到最新版本。
- 必须可观察：用于持久化新 `strong_edges[]` evidence 的新增表、列或索引存在；`schema_meta` 升级到最新版本；旧 workspace 中已有 [Child A](./2026-05-23-burst-pick-export-dedupe-child-a-template-burst-pick-spec.md) run/group/edge 数据不被删除；新算法 run 能与旧版本 run 共存且查询最新同版本 run。
- 验证手段：`tests/test_db_migration.py` 增加逐文件测试覆盖 migration；`docs/db_schema.md` 同步记录新表/列和算法版本语义。

#### AC-10: 特征计算和新 evidence 持久化失败保持原子性

- 触发：通过真实 HTTP/API 入口访问连拍挑选，同时分别制造两类受控故障：(a) 多特征计算稳定失败；(b) 新 `strong_edges[]` evidence 写入或相关 DB 事务稳定失败。
- 必须可观察：对应 run 进入 `failed` 状态或 API 返回可读错误；DB 中不得留下部分 group、部分 group_asset、部分 strong edge、新全局放弃导出标记或 `export_plan` 变更；故障解除后重新通过公共入口触发新 run 可以正常完成。
- 验证手段：服务级集成测试使用测试环境变量、受控 DB 故障注入或临时 workspace 权限/连接故障制造失败，但请求必须走真实 HTTP/API 入口；用 SQLite 断言 run 状态、group/edge 原子性、全局放弃表和 `export_plan` 不变化。
- 降级理由：特征计算和 DB 事务失败属于内部故障，普通用户输入难以稳定触发；受控故障注入只用于制造失败条件，不得伪造成功结果或绕过公共请求路径。

### Done When

- 所有 Acceptance Criteria 都通过对应的“验证手段”完成自动化验证。
- 新算法分组通过公共 API 和 Web 页面可观察，且结果持久化到 DB。
- 没有核心需求是通过直接状态修改、硬编码数据、占位字段、no-op 代码、真实 workspace 数据或 fake integration 满足的。
