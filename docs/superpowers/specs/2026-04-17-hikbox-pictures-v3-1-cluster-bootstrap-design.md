# HikBox Pictures v3.1 密度主导 Cluster 生长与边界剔除设计文档

## 目标

本设计用于把 v3 首版 bootstrap 从“局部连边规则主导”切换到“密度成团主导、边界成员后清洗”的结构。

本次设计的目标是：

- 让未归属 observation 中真实存在的人物 cluster 能先稳定长出来。
- 把“是否成团”和“哪些成员应该保留”拆成两层决策。
- 允许 cluster 在存在边界噪声时继续成立，不再因为少量不稳定 observation 被整体打散。
- 把 `materialized`、`review_pending`、`discarded` 的判定提升为 cluster 级决策，而不是局部 pair 规则的副产物。
- 让人物来源关系脱离 `person.origin_cluster_id` 这种单点绑定，改为独立来源记录。
- 让 WebUI 看到的是最终 cluster 生命周期和成员清洗结果，而不是被过早打散后的 singleton 统计。

## 非目标

本次不做的事情包括：

- 不在本设计中展开具体 migration SQL、仓库落地步骤或实施顺序。
- 不在本设计中展开离线评测、回归验证或 A/B 对比方案。
- 不保留 `tight pair recovery` 这一类补丁式旁路机制。
- 不继续围绕 `top2 + mutual + margin` 局部规则做小修小补。
- 不保留 `person.origin_cluster_id` 作为人物来源字段。

## 背景判断

v3 首版的方向本身是对的：

- 先控制 observation 质量。
- 再用 cluster 长出新人物。
- 让人物原型只由高信任正样本池定义。

但首版 bootstrap 仍然延续了很重的局部连边思维。

它的核心判断更像是：

- 这两个 observation 能不能连边。

而不是：

- 这里是否存在一个正在成形的人物 cluster。

这会带来三个根本问题：

- 邻域很密时，`margin` 很容易变小，结果“越像同一个人，越容易被拒边”。
- cluster 是否成立，过早受到单个 observation 局部歧义的影响。
- 边界 observation 一旦不够稳，系统的代价不是“剔掉成员”，而是“整个 cluster 长不出来”。

在最近一次人工抽样里，最近邻页共抽出 6 组样本，按页面展示是 54 条记录，去重后是 42 个 observation。

从距离结构看，这 42 个 observation 更像是：

- 一个大密度团；
- 外加少量边界 observation；
- 而不是多块完全断开的 identity。

但首版 bootstrap 最终却把大量 observation 打回单点 cluster，并统一落成 `cluster_too_small`。

这说明问题不在“没有相似邻居”，而在“成团前的拒绝条件过强，且拒绝位置放错了”。

## 已选方案概览

v3.1 采用 `C + B` 的结构：

- `C`：以密度聚类作为主骨架，负责发现 cluster。
- `B`：以成员级边界剔除和 cluster 级处置作为后处理，负责清洗 cluster。

对应到判断顺序就是：

1. 先判断这里是否存在稳定 cluster。
2. 再判断哪些 observation 属于 cluster 核心。
3. 再排除边界上不稳定的 observation。
4. 最后才决定该 cluster 直接物化、进入待审核，还是丢弃。

这里的关键变化是：

- “是否成团”由整体密度结构决定；
- “是否留在团里”由 cluster 内部一致性决定；
- “是否建人物”由 cluster 级稳定度决定。

## 核心原则

### 先成团，再清洗

真实存在的人物 cluster 应先有机会长出来。

如果一个团内部混有少量边界 observation，系统应优先：

- 保住 cluster；
- 再剔除边界成员；
- 而不是在成团前就把整个 cluster 打散。

### cluster 成立与成员保留分离

以下两个问题必须显式分开：

- 这里有没有 cluster；
- 这个 cluster 里哪些 observation 应该保留。

不能继续让一个局部 pair 规则同时承担这两层决策。

### 人物来源关系独立化

人物不再通过 `person.origin_cluster_id` 绑定单个来源 cluster。

更合理的模型是：

- `person` 只表达当前人物实体；
- 人物来源关系单独落库；
- cluster 生命周期和人物生命周期分开追踪。

### review_pending 表示“cluster 存在但未落地”

`review_pending` 不再表示“差一点连不上边”。

它应表示：

- cluster 本身存在；
- 但目前还不适合直接物化成人物。

## 输入池设计

主聚类不应直接在全量 observation 上运行，而应先把 observation 分层。

### `core_discovery_pool`

用于主聚类发现 cluster，只包含：

- 有效 embedding；
- 质量达到较高门槛；
- 去重后保留下来的代表 observation。

这层池只回答“哪些 observation 有资格参与发现 cluster”。

### `attachment_pool`

用于后续吸附，不参与主聚类发现，只包含：

- 有效 embedding；
- 质量中等；
- 不足以定义 cluster，但可能属于已有 cluster 的 observation。

### `excluded_pool`

直接排除在本轮主流程之外：

- embedding 缺失；
- 质量明显过低；
- 前置去重或数据清洗后被判定为无独立证据价值的 observation。

### 证据去重

主聚类前要先做证据压缩，避免 burst 或重复采样人为抬高密度。

压缩规则包括：

- 完全相同 embedding 折叠；
- 同一 photo 的重复 observation 只保留质量最高代表；
- 同一 burst 内极近 observation 折叠成一个代表。

这里的目标是让主聚类看到的是“独立证据密度”，而不是“重复采样密度”。

## 主聚类设计

### 总体思路

主聚类采用 HDBSCAN 风格的密度聚类思路，但设计上不绑定具体第三方库。

核心要求只有两点：

- 能把局部高密度 observation 自然长成 cluster；
- 不再把“小 margin”当成成团前硬拒绝信号。

### 邻域构建

主聚类先在 `core_discovery_pool` 上构建较大的 kNN 图。

这里的邻域不再是 `top2` 一类的极小局部视角，而应是能反映局部密度结构的中等规模邻域。

### 密度主导成团

主聚类阶段只负责回答：

- 哪些 observation 落在同一稳定密度区域中；
- 哪些 observation 只是噪声；
- 哪些 observation 对 cluster 的归属本身就偏边界。

因此，主聚类产物应天然允许：

- 稳定 cluster；
- 模糊边界成员；
- 噪声 observation。

### 过并保护

密度聚类不能只解决“别过早拆”，还要避免“把桥接结构误并成一个大团”。

因此主聚类之后必须允许一次 cluster 内部二次拆分，用来处理：

- 两个高密度子团只靠少量桥接 observation 相连；
- 一个大团内部其实存在明显双峰或多峰结构。

也就是说，主聚类负责发现 cluster 候选，但不直接假设每个 raw cluster 都已经是最终 cluster。

## 边界剔除与 cluster 清洗

### `anchor_core`

每个 raw cluster 都需要先识别一组最稳定的核心成员，作为 cluster 的锚点。

这些 observation 负责定义：

- cluster 中心；
- cluster 紧致度；
- cluster 的“这个人长什么样”。

这组成员不应由简单排序替代，而应由 cluster 内稳定度共同决定。

### cluster 轮廓

在识别出核心成员后，需要为每个 cluster 建立轮廓信息：

- medoid；
- 核心半径；
- cluster 紧致度；
- 对最近竞争 cluster 的分离度；
- observation 在 cluster 内的支持率。

这一步的目标不是给页面展示，而是为成员清洗提供统一判据。

### 成员级决策

对每个进入 raw cluster 的 observation，都应做成员级决策。

可接受的结果包括：

- 保留为核心成员；
- 保留为边界成员；
- 暂缓，不纳入当前 final cluster；
- 明确排除。

关键约束是：

- 排除 observation，不等于否掉 cluster；
- 边界 observation 的存在，不应自动拉低整个 cluster 到 `discarded`。

### 清洗后的 cluster

成员清洗后，cluster 需要重新定型。

允许出现三种结果：

- 原 cluster 保留，只是少了一批边界 observation；
- 原 cluster 被拆成两个或多个更稳定子团；
- 原 cluster 清洗后失去稳定核心，才真正丢弃。

这里的关键是：最终处置应基于“清洗后的 cluster”，而不是基于 raw cluster 的第一版结果。

## 低质量 observation 的吸附

`attachment_pool` 中的 observation 不参与定义 cluster，只能在 cluster 已经成立后做后吸附。

吸附条件应同时满足：

- 接近某个 final cluster 的中心或代表；
- 在局部邻域中对该 cluster 有足够高的支持率；
- 与最近竞争 cluster 之间有清晰的归属优势；
- 不会明显破坏现有 cluster 的紧致度。

否则应保持未归属，而不是为了提高召回强行塞进 cluster。

这一步的原则是：

- attachment 只增强已有 cluster；
- attachment 不反过来决定 cluster 是否存在。

## Cluster 级处置

主聚类和成员清洗完成后，系统才进入 cluster 级处置。

### `materialized`

满足以下特征的 final cluster 可以直接物化：

- 有稳定核心；
- 核心成员数量足够；
- 不同照片支撑足够；
- cluster 紧致度好；
- 与其他 cluster 分离足够清晰；
- 边界成员占比不高；
- 不存在明显待拆分信号。

### `review_pending`

以下类型的 final cluster 更适合进入待审核：

- cluster 本身存在，但规模还偏小；
- cluster 成立，但边界成员较多；
- cluster 成立，但与邻近 cluster 的分离还不够强；
- cluster 的主体可信，但落地成人物前仍需要人工确认。

这里的核心语义是：

- cluster 存在；
- 只是暂时不直接建人物。

### `discarded`

只有在以下情况下才进入 `discarded`：

- 清洗后根本没有稳定核心；
- 清洗后只剩孤立 observation；
- cluster 内部高度混杂，且无法拆成稳定子团。

因此，`discarded` 只表示“cluster 本身不存在”，不再表示“局部连边没过”。

## 数据结构与落库模型

### 总体原则

落库模型必须完整表达以下事实：

- 一轮 run 看过哪些 observation；
- 哪些 observation 进入主聚类候选池；
- 主聚类先得到了哪些 raw cluster；
- raw cluster 如何被清洗、拆分、合并；
- 每个成员为什么被保留或排除；
- 最终哪些 cluster 被物化，哪些进入 review。

因此，当前 `auto_cluster + auto_cluster_member + person.origin_cluster_id` 这一组扁平模型不足以承载 v3.1。

### `identity_cluster_run`

一轮 bootstrap 或 incremental 聚类运行。

它负责表达：

- 本轮使用的算法版本；
- profile 快照；
- 模型绑定；
- 本轮整体摘要。

它不直接表达 cluster 结果本身。

### `identity_cluster_pool_entry`

显式记录 observation 在本轮 run 中属于哪一层池：

- `core_discovery`；
- `attachment`；
- `excluded`。

同时记录去重与前置排除结果。

### `identity_cluster`

表示 cluster 生命周期中的一个节点，而不是单纯的“最终 cluster”。

每条记录都应至少能回答：

- 它属于哪一轮 run；
- 它处在哪个阶段；
- 它后来是否被拆分、合并或丢弃；
- 它的 cluster 级稳定度如何；
- 它最终如何被处置。

### `identity_cluster_lineage`

记录 cluster 之间的父子关系，用于表达：

- 清洗前后继承；
- split；
- merge。

没有这张表，就无法追踪 cluster 的真实演化过程。

### `identity_cluster_member`

成员关系表必须显式记录：

- observation 在该 cluster 中的角色；
- observation 的归属强度；
- observation 最终被保留、排除还是延后；
- observation 被排除的原因。

理想上，一个边界 observation 即使最终未保留，也应留下完整审计痕迹，而不是从结果里消失。

### `identity_cluster_resolution`

cluster 结果与产品动作解耦。

这张表负责表达：

- 该 cluster 是直接建人物；
- 还是进入 review；
- 还是被忽略或丢弃。

后续人工处理，也应写在这条 resolution 链上，而不是覆盖掉 cluster 本身。

### `person_cluster_origin`

人物来源关系单独建表。

`person` 本身不再保留 `origin_cluster_id`。

这样可以表达：

- 某个人最初由哪个 cluster 长出；
- 某次 review 是否触发了人物创建；
- merge 后人物吸收了哪些 cluster 来源。

## 状态枚举

### Cluster 生命周期

`cluster_stage`

- `raw`
- `cleaned`
- `final`

`cluster_state`

- `active`
- `split`
- `merged`
- `discarded`

### Cluster 处置

`resolution_state`

- `unresolved`
- `materialized`
- `review_pending`
- `ignored`
- `discarded`

### 成员角色

`member_role`

- `anchor_core`
- `core`
- `boundary`
- `attachment`

### 成员决策

`decision_status`

- `retained`
- `excluded`
- `deferred`

### 人物来源

`origin_kind`

- `bootstrap_materialize`
- `review_materialize`
- `merge_adopt`

## WebUI 心智映射

### `/identity-tuning`

页面主语应从“本轮跑出了多少 cluster”切换成“本轮最终保留下来了哪些 cluster”。

因此首页更适合围绕：

- run summary；
- final materialized clusters；
- final review_pending clusters；
- discarded / noise 摘要。

`raw cluster` 更适合作为调试视图，而不是主界面核心数字。

### `materialized` 区

页面不再只突出一个 `origin_cluster_id` 或 seed 数量。

更适合突出：

- cluster 核心规模；
- 保留成员规模；
- 被排除成员规模；
- 不同照片支撑；
- cluster 稳定度和紧致度。

### `review_pending` 区

页面应明确表达：

- cluster 已经存在；
- 但还不适合直接建人物。

因此 review 区应围绕 cluster 主体展示，而不是把它当成“失败残留物”。

### Person 详情页

人物页的重点应是：

- 当前 trusted samples；
- 当前 active assignments；
- 人物来源历史。

因为 `person.origin_cluster_id` 被移除，来源关系应通过独立来源记录展示，而不是单个字段。

## 与 v3 的关系

v3.1 不是对 v3 的方向反转，而是把 v3 的方向补完整。

v3 解决的是：

- 人物原型不应由全量自动归属样本反向决定。

v3.1 解决的是：

- 新人物 bootstrap 不应再由过强的局部连边规则主导。

两者合起来，才构成同一个完整方向：

- 先让人物以更干净的 cluster 形式长出来；
- 再只用稳定核心去定义人物；
- 最终把人物原型建立在高信任正样本池上。

## 最终结论

v3.1 的主导变化不是“把阈值调松”，而是把 bootstrap 的主问题重新拆开：

- 用密度结构判断 cluster 是否存在；
- 用成员清洗判断 observation 是否应留在 cluster 中；
- 用 cluster 级稳定度判断是否直接长成人物。

只有这样，系统面对“大密度团里混着少量边界 observation”的情况时，行为才会符合直觉：

- 保住 cluster；
- 剔除边界成员；
- 再决定它是 `materialized`、`review_pending` 还是 `discarded`。
