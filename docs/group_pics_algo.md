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
- `重点人物A=489`
- `重点人物B=340`
- 优化批次：
- `noise_count=746`
- `person_count=113`
- `重点人物A=613`
- `重点人物B=459`
- `person_consensus_attach_count=147`

核心收益：

- 噪声数：`1056 -> 746`
- `重点人物A`：`+124`
- `重点人物B`：`+119`
- 多轮 diff review 显示 precision 基本未受影响

补充：

- 文中的“重点人物A/B”仅用于本轮复盘描述，不是稳定人物 ID。
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
- 两位重点人物仅小幅增长（A：`613 -> 638`，B：`459 -> 464`）。

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

## v5：统一证据图驱动的全局人物归属

### 核心目标

v5 的目标是把 v4 的“分段规则决策”升级为“统一证据图 + 全局优化”，在同一轮求解里同时处理：

- 噪声样本归属
- 非噪声微簇归属
- person 与 person 的可合并关系
- 低置信新人物候选

核心诉求仍是同一个平衡：在不牺牲主干 precision 的前提下，继续拉高 recall，并提升流程可学习、可审计、可迭代能力。

### 主流程

v5 仍保留 v4 的“两阶段骨架”，但在第二阶段从“阈值切树”升级为“证据图 + 约束求解”。

0. 数据模型与稳定 ID  
引入 `person_uuid`（跨运行稳定人物 ID）和 `assignment_run_id`（归属版本），并明确“微簇完整性约束”：单簇不拆到多人。

1. 多视角 embedding  
每张脸同时保留 `embedding_main/embedding_flip/embedding_context(可选)`，连同 `magface_quality/det_confidence/face_area_ratio/quality_score` 进入后续决策。

2. 第一阶段高召回微簇  
建议档位 `min_cluster_size=2`、`min_samples=1`，并把噪声视作“单点微簇”统一进入后续流程，不再做噪声分叉逻辑。

3. 前置质量门控（硬排除）  
先做 face 级与微簇级硬排除，避免极低质量样本污染后续自动归属。  
其中微簇证据延续 v4：`quality_evidence = top1_quality + w * top2_quality`。

4. 微簇多原型建模  
每簇同时构建 `r_center/r_medoid/r_exemplar`，减少“离中心远但离同人样本近”的漏召回。

5. 统一候选图  
图节点为微簇与人物，边分两类：`cluster->person` 与 `person->person`。  
边特征覆盖 kNN 投票、多视角距离统计、margin、同图冲突、质量证据等。

6. 学习型边打分  
目标是输出 `P(cluster 属于 person)` 与 `P(personA 与 personB 应合并)`，优先可解释模型（如 LightGBM）。  
当前状态（2026-04-20）：主流程仍以规则阈值（votes/distance/margin/size gate）为主，训练型打分器尚未正式落地；现有两轮 review 标注可作为冷启动样本，但覆盖度不足。

7. 全局约束优化（核心）  
在同一优化里联合求解 `x_{c,p}`（簇归属）、`y_{c,new}`（是否新建人物）、`m_{p,q}`（人物合并），并施加：
- 单簇唯一去向约束
- 同图冲突硬约束
- 簇完整性约束
- 低质量硬排除约束

8. 三档输出  
自动通过 / 待复核 / 保持独立（新人物候选），并与质量门控联动。

9. 迭代收敛（EM 风格）  
按“更新人物原型 -> 重算候选与分数 -> 重跑优化”循环 2~4 轮，直到人物划分变化率低于阈值（如 `<0.5%`）或达到上限。

10. 稳定 ID 对齐与增量并入  
每轮与历史 `person_uuid` 做最大匹配（匈牙利或最大权匹配），实现跨轮命名与纠错沉淀。

11. 审计与回放  
每轮输出 `assignment_events/merge_events/uncertain_queue/run_metrics`，保证决策可追溯。

### 优劣分析

v5 相比 v4 的核心升级有四点：

1. 第二阶段从 `AHC` 阈值切树升级为“学习打分 + 约束优化”。
2. `noise` 与非噪声微簇统一建模，不再流程分叉。
3. 人物 ID 从运行内临时编号升级为跨运行稳定标识。
4. v4 的低质量回退补丁升级为 v5 的前置硬门控。

收益是结构上更一致、可解释性更完整，且给召回增长留下更大空间。  
代价是工程复杂度、标注数据需求和训练校准成本都会明显上升。

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
- 该参数档位可作为“强证据召回”安全基线，后续再叠加第 2 层和结构证据通道（dyad）补重点人物 B 的长尾召回

#### 追加实验：flip 多视角 embedding（两种接入方式）

目标：在不放宽主阈值的前提下，补充侧脸/姿态变化样本的召回。

方案 1：早融合替换（early fusion replacement）

- 做法：`embedding' = normalize(embedding_main + w * embedding_flip)`，并用 `embedding'` 直接替换主向量参与后续全部流程。
- 特点：会改写主向量空间，HDBSCAN 与后续归属边界会整体漂移。

方案 2：晚融合补充（late fusion supplement）

- 做法：主向量保持不变；仅在 `person-consensus` 回挂时，若主通道未过阈值，再用 `max(sim_main, sim_flip)` 作为补充证据。
- 约束：若主通道已通过，直接沿用主通道结果，不因 flip 降级。
- 特点：flip 只补充召回证据，不改主空间拓扑。

效果对比（相对同一无 flip 基线）：

1. 早融合替换
- 重点人物 A：`734 -> 767`（`+33`）
- 重点人物 B：`480 -> 493`（`+13`）
- 重点人物正向新增：`56`；反向差异：`10`（等价局部净增 `+46`）
- 全量归属口径：新增 `94`、移除 `42`、净增 `+52`
- 被移除样本均回落到 `noise`，未出现“直接错挂到他人”

2. 晚融合补充
- 重点人物 A：`734 -> 750`（`+16`）
- 重点人物 B：`480 -> 487`（`+7`）
- 全量归属口径：新增 `24`、移除 `0`、净增 `+24`
- `noise_count: 687 -> 663`
- `person_consensus_attach_count: 114 -> 138`
- 新增样本均来自基线 `noise`，并通过补充证据回挂

结论：

- 早融合替换的总增召回更高，但会显著扰动主空间，回撤风险也更高。
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

聚焦人物（`person_0/person_1`）复核：

- 直接计数：`person_0: 748 -> 751`（`+3`），`person_1: 487 -> 478`（`-9`）
- 为规避 `face_id` 漂移，按“同 `photo_relpath` + `bbox IoU>=0.5` 一对一”做真实匹配：
  - `person_0`：保留 `723`、流出 `21`、流入 `27`、基线未匹配丢失 `4`、候选未匹配新增 `1`
  - `person_1`：保留 `477`、流出 `2`、流入 `0`、基线未匹配丢失 `8`、候选未匹配新增 `1`

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

### v5 质量门控建议参数（首版）

1. face 级硬排除：
- `face_min_quality_for_assignment` 建议从全库质量分位点 `p5~p10` 初始化，再按误并案例回调。

2. 微簇级硬排除：
- `low_quality_micro_cluster_max_size <= 3`
- `low_quality_micro_cluster_top2_weight` 可从 `0.5` 起步
- `low_quality_micro_cluster_min_quality_evidence` 可从 v4 已验证值 `0.65` 起步，再做网格搜索

3. 软约束层：
- 对未触发硬排除但质量偏低样本施加惩罚项，惩罚建议联动 `magface_quality/det_confidence/face_area_ratio`，而非单阈值硬切。

执行原则：硬门控兜底 precision，recall 主要靠软约束和全局优化提升。

### 评测协议与上线闸门

离线评测最少包含：

1. face-level pairwise precision/recall/F1
2. BCubed precision/recall/F1
3. 主人物召回与误并率（如当前重点人物）
4. 自动通过样本 precision 下限
5. 待复核队列规模（人工负载）

上线闸门建议：

1. 自动通过 precision 不低于 v4 基线
2. recall 相比 v4 有显著增益
3. 待复核规模可控，不出现数量级膨胀

### 落地步骤（建议）

1. 先补齐 `person_uuid` 与审计数据结构，不改当前判定。
2. 接入统一候选图与边特征抽取，先 shadow 打分。
3. 上线 `cluster->person` 全局优化（先不启 `person->person` 变量）。
4. 接入 `person->person` 全局合并与 EM 迭代，逐步替换 AHC 主判定。
5. 用持续 review 数据做周期性重训，进入稳定迭代。

### v5 产品化 TODO

1. `det_size 640 -> 1280` 已完成全库评估（2026-04-21）：当前口径下基本无优化效果，暂不考虑推进。
2. scan 处理过程并发加速？按 cpu 核数 / 2？
