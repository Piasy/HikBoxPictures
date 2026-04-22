# 群照人物算法演进总结（v1~v5）

这篇文档用于对外分享我们的相册图库人物系统演进，不追踪细节调参，只讲主导思路如何一步步变化。

## v1：参考脸驱动的照片检索

### 核心目标

给定少量目标人物参考图，快速从候选照片里找出“包含这些人”的照片。

### 主流程

1. 为每个目标人物准备参考脸表示。
2. 对候选照片做人脸检测与 embedding 提取。
3. 候选脸与参考脸做相似度匹配，判断目标人物是否出现。
4. 按规则输出命中照片，并做简单分桶（如双人图/多人图）。

### 优劣分析

优点是上手快、心智简单，适合“临时找图”的短流程场景。  
真正的问题是它本质上是一次性检索：

- 强依赖参考图质量，参考图一旦偏，结果整体偏。
- 结果难沉淀为长期可维护的人物资产。
- 纠错基本是“本次任务修补”，无法自然反哺后续全库能力。

这直接引出下一版：系统需要从“找图工具”升级为“人物归属系统”。

## v2：人物原型驱动的全库归属

### 核心目标

把图库里每张人脸 observation 持续归属到“人物记录”，形成可维护、可纠错、可复用的人物库。

### 主流程

1. 照片入库并做人脸检测，生成 observation。
2. 提取 embedding，面向人物原型做候选召回与精排。
3. 对每张脸做自动归属、待复核或新人物候选判定。
4. 通过人工纠错回写归属，并更新人物原型。
5. 下游再基于归属结果做检索、导出等业务。

### 优劣分析

相较 v1，v2 完成了架构升级：从“临时检索”变成“可持续的人物系统”。  
但核心痛点也暴露出来：

- 自动归属仍偏激进，容易把错误样本写入人物。
- 原型更新过度依赖“现有归属全量样本”，误差会被放大。
- 用户需要持续做“排除式纠错”，长期成本高。

换句话说，v2 的方向对了，但“原型数据源”和“自动归属保守性”不够稳。  
这引出下一版：要先控制样本可信度，再让人物结构增长。

## v3：高信任样本池驱动的人物生长

### 核心目标

以“高信任样本池”为人物定义基础，降低误归属污染；同时把新人物发现纳入主流程，让人物库更稳地自动增长。

### 主流程

1. 先把“样本质量”与“身份相似度”分开建模。
2. 对未稳定归属样本先做聚类发现，形成可成长的人物候选。
3. 人物原型主要由高信任正样本构建，而非全量自动归属样本。
4. 自动归属更保守，人工确认更多承担“加法确认”而不是“减法清洗”。

### 优劣分析

v3 解决的是“系统可持续性”：  
宁可前期保守，也要避免错误样本进入原型后持续扩散。

这条路线的价值是：

- 人物定义更干净，系统越跑越稳。
- 新人物发现从辅助能力变为主流程能力。
- 用户工作流从“反复排错”转向“持续确认”。

真正暴露出来的主要问题，不是“这条路代价高”，而是它对质量评估调参的依赖度非常高，而且全流程联动下很难调稳：

- 质量门控决定了谁能进入高信任样本池，而质量分本身是多因子组合（面积、清晰度、姿态等），单个因子口径变化就会影响整体排序。
- 原始调参里，面积口径就经历过“面积占比 vs 绝对面积”的反复权衡：在超高清图库里，仅看占比会误伤不少可用小脸。
- 清晰度口径也不稳定：如果锐度计算过于直接，容易被 JPEG 噪声或背景纹理抬高，导致“看起来清晰分很高、实际不适合建模”的样本混入。
- 分位点归一化窗口（如 p10/p90 或更宽窗口）对不同批次数据很敏感，换一批图库后阈值可迁移性不强。
- 质量阈值一旦变动，会连锁影响后续发现、归属、原型更新，单点调优很容易“前面变好、后面变差”。
- 在完整链路里验证一轮调参成本高、反馈慢，导致参数收敛周期长，工程迭代效率不理想。
- 多轮调参实验后，对于高质量样本池的大小和内容，还是很不满意。

这轮复盘的结论是：  
v3 的方向没错，但要把“先把质量门调到非常完美”作为前置条件并不现实。  
这也是我们继续推进到 v4 的直接原因：先用两阶段聚类拿到更稳的结构，再在此基础上迭代质量与归属策略。

## v4：两阶段聚类驱动的人物归并

### 核心目标

v4 的目标是把人物系统稳定在一条可产品化、可持续迭代的主链路上：  
`检测/对齐 -> embedding -> 聚类 -> 命名纠错 -> 增量并入`。

这一版的关键变化不是“再堆一个更复杂的单阶段聚类”，而是明确采用“两阶段聚类”：

1. 先拿高纯微簇（保 precision）。
2. 再做人物级归并（补 recall）。

### 主流程

当前工程验证主链路是 `detect -> embed -> cluster`，其中 `cluster` 内部分成两段：`HDBSCAN 微簇 -> AHC 人物归并`。

1. 检测、对齐与样本落盘  
每张脸产出局部脸图、上下文图、对齐脸图，并保留基础信号：`det_confidence`、`face_area_ratio`。  
当前口径：`pad_ratio=0.25`、`preview_max_side=480`。

2. embedding 与质量分  
对齐脸输入 MagFace，得到 `f` 与归一化向量 `e=f/||f||2`，并计算：  
`quality_score = magface_quality * max(0.05, det_confidence) * sqrt(max(face_area_ratio, 1e-9))`。  
这一步把“模型质量 + 检测可信度 + 可见面积”合成后续排序依据。

3. 第一阶段微簇（HDBSCAN）  
默认 `min_cluster_size=3`、`min_samples=2`，标签 `-1` 视作噪声；样本量小于 `max(2, min_cluster_size, min_samples)` 时整批按噪声处理。  
工程下限常量 `2` 主要用于规避单样本边界异常。

4. 微簇代表构建  
每个簇按质量分取 top-k（`person_rep_top_k=3`）构建代表向量：  
`r = normalize(sum(w_i * e_i))`，其中 `w_i=max(quality_score_i, 1e-3)`。  
作用是把 face-level 离散点压缩成 cluster-level 稳定节点。

5. 候选边与约束  
基于簇代表余弦距离建边，再叠加近邻约束（`person_knn_k=8`）和可选同图冲突约束。  
不满足约束的簇对直接置大距离，避免进入可合并候选。

6. 第二阶段人物归并（AHC）  
当前主用 `single linkage`，按 `person_merge_threshold` 切树。  
全量常用档位是 `single + 0.26`，代码默认 `single + 0.24`。  
输出层级是“人物 -> 微簇 -> 人脸”，进入命名、拆并、忽略、增量并入闭环。

### 优劣分析

v4 的价值是把“先保纯、再归并”落实成稳定工程路径，避免回到单阶段方案反复拉扯。  
它把问题拆成了可持续优化的两层：第一层守住纯度，第二层负责召回。

但这一版的代价也很明确：

- 同人拆分和跨人误并仍是长期博弈。
- `linkage` 与阈值对结果敏感，参数迁移成本不低。
- 质量信号仍需随着图库分布持续校准。

总体看，v4 不是终点，但它给后续版本提供了可迭代的稳定骨架。

### v4 优化记录：`min_samples=1 + person-consensus` 噪声回挂

这部分只记录一轮召回优化，不改上面的 v4 主流程定义。

#### 调整内容

1. 第一阶段保持 `min_cluster_size=3`，仅把 `min_samples: 2 -> 1`，释放一批“小而干净但原先判噪声”的样本。
2. 在 HDBSCAN 后新增 `person-consensus` 噪声回挂：
- 仅处理 `cluster_label=-1` 的噪声脸。
- 基于已有微簇代表做 person 候选比较。
- 同时满足距离阈值与次优 margin 时，把噪声脸挂回已有微簇。
- 不创建新 cluster，只改挂现有 label。

#### 本轮参数

- 微簇：`min_cluster_size=3`、`min_samples=1`
- 噪声回挂：
- `person_consensus_distance_threshold=0.24`
- `person_consensus_margin_threshold=0.04`
- `person_consensus_rep_top_k=3`

#### 本轮效果

- 基线批次：
- `noise_count=1056`
- `person_count=96`
- `重点人物 A=489`
- `重点人物 B=340`
- 优化批次：
- `noise_count=746`
- `person_count=113`
- `重点人物 A=613`
- `重点人物 B=459`
- `person_consensus_attach_count=147`

核心收益：

- 噪声数：`1056 -> 746`
- `重点人物 A`：`+124`
- `重点人物 B`：`+119`
- 多轮 diff review 显示 precision 基本未受影响

补充：

- 文中的“重点人物 A/B”仅用于本轮复盘描述，不是稳定人物 ID。
- 这轮收益本质是“先放宽一阶段成簇，再把明显接近已有 person 的噪声挂回”。

#### 追加实验 A：`min_cluster_size=2` 放开 pair 微簇

目标是进一步清理高质量噪声，允许 pair 直接成簇。

- 参数：
- `min_cluster_size=2`
- `min_samples=1`
- `person_consensus_distance_threshold=0.24`
- `person_consensus_margin_threshold=0.04`
- `person_consensus_rep_top_k=3`
- `person_merge_threshold=0.26`

相对上一轮方案：

- `noise_count: 746 -> 521`（-225）
- `cluster_count: 272 -> 523`（+251）
- `person_count: 113 -> 246`（+133）
- 噪声里（互斥区间）：
- `Q<0.55: 300 -> 216`
- `0.55<=Q<1.0: 163 -> 112`
- `1.0<=Q<2.0: 164 -> 104`
- `Q>=2.0: 119 -> 89`

副作用：

- 新增 `235` 个 `size=2` 微簇，其中 `141` 个来自“原 baseline 双噪声样本配对成簇”。
- `person_consensus_attach_count: 147 -> 112`，收益主要来自“一阶段放开成簇”，不是“更多挂回”。
- 重点人物 A：`613 -> 638`，重点人物 B：`459 -> 464`。

结论：噪声降得快，但系统明显变碎，需要补“低质量小簇回退”闸门。

#### 追加实验 B：低质量微簇回退闸门

针对一批“超小脸 + 低质量分”的误回捞样本，在 HDBSCAN 后、person-consensus 前新增回退规则：

- 仅检查：`cluster_size <= low_quality_micro_cluster_max_size`
- 质量证据：`quality_evidence = top1_quality + low_quality_micro_cluster_top2_weight * top2_quality`
- 若 `quality_evidence < low_quality_micro_cluster_min_quality_evidence`，整簇回退 `noise`

新增参数（该轮实验时默认关闭；当前代码默认已启用质量回退阈值）：

- `--low-quality-micro-cluster-max-size`（默认 `3`）
- `--low-quality-micro-cluster-top2-weight`（默认 `0.5`）
- `--low-quality-micro-cluster-min-quality-evidence`（当时默认 `None`，当前代码默认 `0.72`）

验证参数：

- `max_size=3`
- `top2_weight=0.5`
- `min_quality_evidence=0.65`

相对实验 A：

- `cluster_count: 523 -> 449`
- `noise_count: 521 -> 683`
- `person_count: 246 -> 175`
- 回退统计：`demoted_clusters=74`、`demoted_faces=164`
- noise 质量分布（互斥区间）：
- `Q<0.55: 216 -> 378`
- `0.55<=Q<1.0: 112 -> 112`
- `1.0<=Q<2.0: 104 -> 104`
- `Q>=2.0: 89 -> 89`
- 目标类型的低质量小簇均被回退到噪声
- 两位重点人物保持不变（A=`638`，B=`464`）

结论：该闸门主要清理低质量小簇，对当前重点人物主干召回影响较小。

## v5：两阶段骨架上的召回增强与质量门控

### 核心目标

当前代码里的 v5，是在 v4 的两阶段骨架上继续补召回，并把低质量污染控制住。
这一版的实际目标是：

- 在不明显牺牲主干 precision 的前提下，继续补噪声脸与小微簇的 recall
- 把 `flip` 多视角补充、face 级质量门控、微簇质量回退收敛到默认链路
- 保持当前工程仍然可回放、可调参、可做 HTML review

### 主流程

当前工程主链路仍是 `detect -> embed -> cluster`，但 v5 在 v4 基础上新增了几项默认增强：

0. 输出人物结构：当前仍以运行内 `person_label/person_key` 为主，并额外输出一个按当前 member 集合派生的 `person_uuid`，主要用于 manifest 展示与 diff 复核。

1. 主 embedding + flip 补充：每张脸保留 `embedding_main`，可选计算 `embedding_flip`；`quality_score` 仍使用 `magface_quality * max(0.05, det_confidence) * sqrt(max(face_area_ratio, 1e-9))`。

2. 第一阶段高召回微簇  
默认 `min_cluster_size=2`、`min_samples=1`，先尽量放开 pair 微簇；噪声继续保留为 `-1`，后续通过 `person-consensus` 单独处理。

3. 前置质量门控（硬排除）  
先做 face 级硬排除，再对小微簇做 `quality_evidence = top1_quality + w * top2_quality` 回退，避免低质量样本进入后续自动归属。

4. 微簇代表构建：当前使用按质量分加权的 top-k 平均代表向量。

5. 第二阶段人物归并：先把非噪声微簇用 AHC 做人物级合并；默认仍是 `single linkage + knn` 约束，并支持可选同图 `cannot-link`。

6. 噪声回挂：对 noise 脸使用 `person-consensus` 做回挂；主通道未过阈值时，再用 `flip` 做晚融合补充，不改主向量空间。

7. 非噪声微簇 recall：对小 person / 小微簇再做 `cluster->person` 召回，按 `votes + distance + margin + size gate` 规则把高置信微簇并到大 person，默认最多迭代 `2` 轮。

8. 输出与复核：输出 `persons/clusters/person_cluster_recall_events`、质量门控计数、回挂统计和 HTML review 页面，供人工复核与参数回放。

### 优劣分析

这一版真正落地的价值，是在不推翻 v4 主体工程的前提下，把几项高收益增强变成默认链路：

1. `flip` 晚融合补充只增加召回证据，不改主 embedding 空间，风险可控。
2. `face_min` 与 `micro_evidence` 让低质量样本更早退出自动归属。
3. `person-consensus + person_cluster_recall` 分别补噪声脸和小微簇召回，收益路径清晰。
4. 整体仍沿用 v4 的两阶段结构，调参与 diff review 成本可控。

代价也很明确：

- 链路由多段规则串联，参数联动仍然较强。
- `noise` 回挂与非 `noise` 微簇 recall 分别调参，整体调优面较宽。
- 召回收益主要依赖规则阈值和人工 review 反复校准。
- 当前输出更偏离线批处理与 review 工作流。

### v5 优化记录

#### 首轮（v5 主流程初版）

对比 v4 最新可用基线（`min_cluster_size=2/min_samples=1/person-consensus=0.24`）：

- 重点人物 A：`638 -> 673`（`+35`, `+5.5%`）
- 重点人物 B：`464 -> 480`（`+16`, `+3.4%`）

#### 追加实验：仅启用“第 1 层强证据 recall”

目标：不改主流程结构，只放开高置信可回收微簇，优先补重点人物 A/B 召回。

实验说明：基线与候选使用独立缓存副本，便于并行 review 与回放。

本轮仅调整强证据 recall 参数：

- `person_cluster_recall_distance_threshold: 0.30 -> 0.32`
- `person_cluster_recall_source_max_cluster_size: 3 -> 20`
- 其余保持不变（`margin=0.04/top_n=5/min_votes=3/max_rounds=2`）

结果（基线 -> 候选）：

- `person_cluster_recall_attach_count: 12 -> 24`（`+12`）
- `person_count: 163 -> 152`
- `cluster_count: 448 -> 448`（不变）
- `noise_count: 687 -> 687`（不变）
- 重点人物 A：`673 -> 734`（`+61`, `+9.1%`）
- 重点人物 B：`480 -> 480`（不变）

复核结论：

- 重点人物 A 新增归属准确率：`100%`
- 该参数档位可作为“强证据召回”安全基线

#### 追加实验：flip 多视角 embedding 晚融合补充

目标：在不放宽主阈值的前提下，补充侧脸/姿态变化样本的召回。

做法：

- 主向量保持不变；仅在 `person-consensus` 回挂时，若主通道未过阈值，再用 `max(sim_main, sim_flip)` 作为补充证据。
- 若主通道已通过，直接沿用主通道结果，不因 `flip` 降级。
- `flip` 只补充召回证据，不改主空间拓扑。

效果（相对同一无 flip 基线）：

- 重点人物 A：`734 -> 750`（`+16`）
- 重点人物 B：`480 -> 487`（`+7`）
- 全量归属口径：新增 `24`、移除 `0`、净增 `+24`
- `noise_count: 687 -> 663`
- `person_consensus_attach_count: 114 -> 138`
- 新增样本均来自基线 `noise`，并通过补充证据回挂

结论：

- 晚融合补充增益更保守，但具备“单调补召回、不打掉原召回”的稳定性，更适合作为默认生产策略。

#### 追加实验：质量门控参数落地（`face_min=0.25` + `micro_evidence=0.72`）

目标：把 review 结论固化到默认参数，清理“低质量成员混入小微簇”的长尾样本，同时控制人物主干回撤。

实验口径：

- 基线：flip 多视角 embedding 晚融合补充
- 候选：在基线上叠加 `face_min=0.25` 与 `micro_evidence=0.72`
- 对比方式：同口径 `cluster diff` + 关键簇人工复核

结果（基线 -> 候选）：

- `person_count: 152 -> 146`（`-6`）
- `cluster_count: 448 -> 441`（`-7`）
- `noise_count: 663 -> 690`（`+27`）
- `face_quality_excluded_count: 26 -> 200`（`+174`）
- `low_quality_micro_cluster_demoted_cluster_count: 73 -> 55`（`-18`）
- `low_quality_micro_cluster_demoted_face_count: 159 -> 91`（`-68`）
- `person_consensus_attach_count: 138 -> 136`（`-2`）
- `person_cluster_recall_attach_count: 25 -> 25`（不变）

聚焦样本复核：

- `cluster_90`：在候选中被整体回退（其证据分约 `0.702`，低于 `0.72`）
- `cluster_107`：`3 -> 2`，低质量尾样本被 `face_min=0.25` 过滤
- `cluster_128`：`3 -> 1`，两张低质量尾样本被 `face_min=0.25` 过滤

结论：

- `micro_evidence=0.72` 可稳定压掉这批“低质量混入”微簇，且总体回撤可控。
- 因此将 `--low-quality-micro-cluster-min-quality-evidence` 默认值更新为 `0.72`，作为当前 v5 默认档位。

#### 追加实验：`det_size` 从 `640` 提升到 `1280`（全库复测，2026-04-21）

目标：验证高分辨率检测是否能带来有效人物归属收益。

实验口径：

- 基线：`det_size=640`
- 候选：`det_size=1280`
- 运行说明：detect 阶段按子进程分批续跑，`--detect-restart-interval=50`
- 对比方式：同口径 `cluster diff` + 聚焦人物人工复核

全量指标（基线 -> 候选）：

- `face_count: 2380 -> 3441`（`+1061`, `+44.6%`）
- `person_count: 146 -> 164`（`+18`）
- `cluster_count: 441 -> 453`（`+12`）
- `noise_count: 690 -> 1710`（`+1020`）
- `face_quality_excluded_count: 200 -> 978`（`+778`）
- `person_consensus_attach_count: 136 -> 131`（`-5`）
- `person_cluster_recall_attach_count: 25 -> 19`（`-6`）

聚焦人物（`重点人物 A/重点人物 B`）复核：

- 直接计数：`重点人物 A: 748 -> 751`（`+3`），`重点人物 B: 487 -> 478`（`-9`）
- 为规避 `face_id` 漂移，按“同 `photo_relpath` + `bbox IoU>=0.5` 一对一”做真实匹配：
  - `重点人物 A`：保留 `723`、流出 `21`、流入 `27`、基线未匹配丢失 `4`、候选未匹配新增 `1`
  - `重点人物 B`：保留 `477`、流出 `2`、流入 `0`、基线未匹配丢失 `8`、候选未匹配新增 `1`

结论：

- `det_size=1280` 的主要变化是“检测更多人脸”，但没有转化为可接受的归属收益；
- 噪声与低质量排除显著膨胀，重点人物净增益极小（`+3/-9` 量级）；
- 该方向在当前 v5 参数与流程下**基本无优化效果**，结论为：**不再考虑将默认 `det_size` 从 `640` 提升到 `1280`**。

### v5 当前代码默认档位（2026-04-21）：

- `min_cluster_size=2`
- `min_samples=1`
- `person_merge_threshold=0.26`
- `embedding_enable_flip=true`（默认走晚融合补充通道，可通过 `--no-embedding-enable-flip` 关闭）
- `person_consensus_distance_threshold=0.24`
- `person_cluster_recall_distance_threshold=0.32`
- `person_cluster_recall_source_max_cluster_size=20`
- `face_min_quality_for_assignment=0.25`
- `low_quality_micro_cluster_min_quality_evidence=0.72`

### v5 后续改进方向

以下内容在文档里曾作为 v5 目标被描述过，但截至 `2026-04-21` 仍未进入当前代码主链路，统一收口到这里：

1. 统一证据图与全局约束求解：把 `noise` 和非 `noise` 微簇统一进同一候选图，在同一轮里联合处理 `cluster->person`、`person->person` 合并和新人物创建，而不是继续依赖 `AHC + person-consensus + person_cluster_recall` 的分段规则。

2. 学习型边打分：为 `cluster->person` 与 `person->person` 建立可解释打分器（如 LightGBM），把多视角距离、margin、kNN 投票、同图冲突、质量证据等统一映射到概率分数。

3. 更完整的表示与候选建模：补 `embedding_context`、`r_center/r_medoid/r_exemplar` 多原型建模，以及第 2 层结构证据通道（如 dyad）；当前代码只有 `main + flip` 和单一加权代表向量。

4. 稳定 ID 与增量并入：引入真正的跨运行 `person_uuid`、`assignment_run_id`，并与历史结果做最大匹配（匈牙利或最大权匹配），把命名和纠错沉淀到跨轮数据模型里。

5. 三档决策与完整审计回放：补齐“自动通过 / 待复核 / 保持独立新人物候选”三档输出，以及 `assignment_events/merge_events/uncertain_queue/run_metrics` 这类完整事件流。

6. 更强的迭代求解：引入 EM 风格多轮“更新原型 -> 重算候选 -> 重跑优化”收敛流程；当前代码只有 `person_cluster_recall_max_rounds=2` 的局部 recall 迭代。

7. 尚未纳入当前默认实现的实验方向：`flip` 早融合替换做过离线对比，但会明显扰动主向量空间，因此当前代码只保留晚融合补充方案。

## 开源解决方案评估对比

用和 v4/v5 相同的图库做了扫描识别结果的对比。

photoprism/photoprism:
- 39,559 star, 高活跃
- 重点人物 A: 302
- 重点人物 B: 431
- person 2: 241，也是重点人物 A，但没合并为同一个人物

immich-app/immich:
- 98,278 star, 高活跃
- 重点人物 A: 864
- 重点人物 B: 556

上述结果，准确率都很好，召回率 immich 比 v5 略优。
