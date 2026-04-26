# Immich v6 人物图库产品化 Slice 0：真实验收小图库 Spec

## Goal

由 agent 直接生成并入库一套固定真实小图库和 `manifest.json`，并提供一个 `tests/` 下的验收测试，作为扫描、人物归属、WebUI、合并、排除和导出的共同验收基线。

## Global Constraints

- 本 spec 是父 spec `docs/superpowers/specs/2026-04-24-immich-v6-people-gallery-productization-spec.md` 的 Slice 0，只负责入库验收图库、`manifest.json` 和验收测试，不实现扫描、人物归属、WebUI 或导出功能。
- 小图库必须直接入库到 `tests/fixtures/people_gallery_scan/`；不提供下载脚本、生成脚本或运行时生成路径。
- 图片和 `manifest.json` 由 agent 直接生成并提交；生成过程需要保证同一人物跨多张照片的一致性、不同人物之间可区分、合照中人物可见，并在 manifest 中记录生成说明和 checksum。
- 验收只通过一个测试用例完成：`tests/people_gallery/test_people_gallery_fixture.py`。该测试读取入库 fixture 和 manifest，执行 schema、文件、checksum、解码、类别数量、Live MOV 配对和真实 InsightFace 探针检查。
- manifest 只能作为测试断言数据，不得作为产品逻辑输入。
- 验收必须使用真实图片解码、真实文件系统、真实 manifest 校验和真实 InsightFace 可用性探针；mock/stub/no-op 路径不得满足验收。
- 真实 InsightFace 探针必须与产品扫描检测语义对齐，固定使用 `buffalo_l det_10g.onnx` 且 `det_thresh=0.7`；不得再用更低阈值单独放宽 Slice 0 基线。
- 所有临时探针报告和中间检查产物必须放在 `.tmp/people-gallery-test-gallery/` 下；不得在仓库根目录创建其它临时目录。

## Feature Slice 1: 入库真实验收小图库

- [x] Implementation status: Done

### Behavior

- 仓库固定包含 `tests/fixtures/people_gallery_scan/manifest.json` 和对应媒体文件；后续 Slice B-G 只复用这一份 fixture，不另建缩水图库。
- 小图库精确包含 50 张支持扫描的照片文件，文件名按稳定前缀和序号组织，确保 discover 阶段在不同平台和重复运行中排序稳定。
- 50 张照片的内容类别精确为：30 张单目标人物照片、8 张目标人物合照、4 张非目标人物照片、4 张无脸场景/物体照片、2 张 HEIC/HEIF Live Photo 正例照片、2 张 JPG/PNG Live Photo 反例照片。
- 30 张单目标人物照片精确覆盖 3 个目标人物，每人 10 张；每个目标人物的 10 张照片需要覆盖正脸、轻微侧脸、不同光照、不同背景和不同拍摄月份。
- 8 张目标人物合照精确包含 6 张双人合照和 2 张三人合照；每张合照都只包含 3 个目标人物中的两个或三个。
- 4 张非目标人物照片包含真实人脸，但 manifest 标记为不形成目标人物、也不参与 `expected_person_groups`。
- 4 张无脸场景/物体照片不包含可检测人脸，manifest 标记为无脸样本。
- 2 张 HEIC/HEIF Live Photo 正例照片各自带同目录隐藏 MOV：第 1 组使用大写 `.HEIC` 和大写 `.MOV`，第 2 组使用小写 `.heif` 和小写 `.mov`，用于覆盖大小写不敏感匹配。
- 2 张 JPG/PNG Live Photo 反例照片各自带同目录相似 MOV，但 manifest 明确标记为不得形成 Live Photo 配对。
- fixture 额外包含 1 个非支持后缀文件和 1 张损坏或不可解码图片；这两个文件不计入 50 张支持扫描照片。
- `manifest.json` 记录 `people`、`assets`、`expected_person_groups`、`expected_exports`、`tolerances`、`checksums` 和 `provenance`；字段语义必须足够支撑所有后续子 spec 的自动化断言。
- manifest 可以声明少量 `tolerances`，用于标记真实模型可能不稳定的边界照片或边界 face；3 个目标人物、30 张单目标人物照片和 8 张合照不得落入 tolerance 后导致核心验收无法证明。

### Public Interface

- 固定 fixture 目录：`tests/fixtures/people_gallery_scan/`。
- manifest 文件：`tests/fixtures/people_gallery_scan/manifest.json`。
- 唯一自动化验收入口：`tests/people_gallery/test_people_gallery_fixture.py`。
- 验收命令：`pytest tests/people_gallery/test_people_gallery_fixture.py`。

### Error and Boundary Cases

- fixture 缺文件、checksum 不匹配、manifest JSON schema 不合法、文件名排序不稳定、任一类别数量不等于规定值、Live MOV 正反例缺失时，验收测试必须失败并指出具体文件或字段。
- HEIC/HEIF 解码依赖缺失时，验收测试必须失败并提示缺少依赖，不得自动跳过 HEIC/HEIF 样本。
- InsightFace 无法在核心目标人物照片中检测到足够人脸时，验收测试必须失败；允许 tolerance 中声明的边界图片不计入核心通过条件。
- 验收测试重复运行不得修改 fixture 文件；只能在 `.tmp/people-gallery-test-gallery/` 写入报告。

### Non-goals

- 不实现 `hikbox scan start`、人物聚类、WebUI、合并、排除或导出。
- 不提供下载脚本、生成脚本、远程素材同步或运行时 fixture 生成流程。
- 不要求图库覆盖所有真实相册格式；首版只覆盖本 spec 指定的固定验收矩阵。
- 不把 manifest 暴露为产品配置、产品 API 或运行时业务输入。
- 不要求 MOV 内容参与视频识别；MOV 只用于 Live Photo 文件配对验收。

### Acceptance Criteria

- AC-1：执行 `pytest tests/people_gallery/test_people_gallery_fixture.py` 后，测试读取入库 `manifest.json` 并确认 fixture 精确包含 50 张支持扫描照片、1 个非支持后缀文件和 1 张损坏或不可解码图片。
- AC-2：验收测试确认 50 张支持扫描照片的类别精确等于：30 张单目标人物照片、8 张目标人物合照、4 张非目标人物照片、4 张无脸场景/物体照片、2 张 HEIC/HEIF Live Photo 正例照片、2 张 JPG/PNG Live Photo 反例照片。
- AC-3：验收测试确认 3 个目标人物各有 10 张单目标人物照片；8 张目标人物合照精确包含 6 张双人合照和 2 张三人合照；`expected_person_groups` 和 `expected_exports` 能引用对应 asset。
- AC-4：验收测试确认 manifest 中每个 `asset` 都能关联真实文件、checksum、拍摄月份、人物标签、Live MOV 正反例标记、无脸/损坏/非支持后缀标记和 tolerance 标记。
- AC-5：验收测试确认 2 个 HEIC/HEIF + 隐藏 MOV 正例和 2 个 JPG/PNG + 相似 MOV 反例都真实存在，正例和反例在 manifest 中有明确标记，并可被后续扫描测试复用。
- AC-6：验收测试使用真实 InsightFace 探针以 `det_thresh=0.7` 对核心目标人物照片运行后，3 个目标人物各有 10 张单目标人物照片可检测到人脸，8 张合照都可检测到两个或更多目标人物 face；探针结果写入 `.tmp/people-gallery-test-gallery/` 报告。
- AC-7：验收测试重复运行不会修改 fixture 文件，且根据 manifest checksum 能发现任何意外文件内容变更。

### Automated Verification

- 本 slice 只有一个自动化测试用例：执行 `pytest tests/people_gallery/test_people_gallery_fixture.py`。
- 该测试必须一次性覆盖 AC-1 到 AC-7，并通过公共文件路径读取 `tests/fixtures/people_gallery_scan/` 与 `manifest.json`。
- 该测试必须执行真实文件存在性检查、checksum 校验、图片解码、HEIC/HEIF 解码、损坏图片不可解码检查、类别数量检查、Live MOV 正反例检查，以及与产品扫描一致的 `det_thresh=0.7` 真实 InsightFace 探针。
- 该测试不得 import 产品扫描、人物归属、WebUI 或导出内部函数；不得用硬编码成功、空目录、fake detector、跳过 HEIC/HEIF 或直接修改 manifest 的方式通过。

### Done When

- `pytest tests/people_gallery/test_people_gallery_fixture.py` 通过，且输出报告位于 `.tmp/people-gallery-test-gallery/`。
- 后续 Slice B-G 能引用同一份入库 fixture 和 manifest，而不需要另建缩水图库。
- 没有核心需求通过硬编码 manifest、占位图片、fake detector、直接状态修改或不可复现随机生成满足。
