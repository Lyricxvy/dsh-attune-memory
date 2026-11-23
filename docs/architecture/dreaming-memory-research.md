# ChatGPT「Dreaming」记忆机制专题研究报告

> 研究目的：为 dsh-attune-memory 的后续架构演进提供事实依据，找出长期记忆在**综合、治理、更新、调用**上真正值得改进的方向。
> 研究日期：2026-11-19
> 证据分级（全文沿用）：【官方】= OpenAI 官方披露；【三方】= 独立第三方研究/媒体，未经 OpenAI 确认；【推断】= 基于公开材料的合理推断，不代表 OpenAI 内部实现。

---

## 0. 结论先行

1. **Dreaming 与 dsh-attune-memory 在"记忆合成"上是两条相反的路。** Dreaming 的核心是**写入期后台综合**（consolidate-at-rest：一个异步进程跨多段历史，把信息"综合、加权、修订"成一个演化的记忆状态）；dsh-attune-memory 的核心是**读时组合**（compose-at-read：User Model 永不落库，查询期现场组装，ADR-004，且明确写着"没有第三步：不存在『更新 User Model』的写操作"）。这不是能力差距，是**主动的哲学选择**。

2. **该选择被 Dreaming 自己的失败案例反向支持。** 第三方记录了一个实例【三方】：某用户"leads the payments team"（3 月，正确）被后台静默改写为"leads infrastructure"（4 月，错误），6 月被实际采用，全程无编辑日志、无通知、无 diff。这正是 dsh 红线（"推断不可证伪、不可纠正、写进画像会自我强化"）所担心的失效模式。

3. **真正值得借鉴的只有三件，且都不需要重构哲学：**
   - ① **时间性事实的"时态正确性"**（未来时→过去时的自动/半自动更新）；
   - ② **把"随时间正确性"纳入评测维度**（dsh 现有外部评测只测检索召回）；
   - ③ **审视写入方向的注入防护**（外部不可信内容 → 持久记忆写入的攻击面）。

4. **明确不值得引入**（有证据、有理由）：后台自动综合（Dream 式）、提及频率加权（usageBoost）、前台/后台二分、自动人格推断。

5. **一个必须点明的结构性约束**：Dreaming 是**中心化、端到端可控**（OpenAI 同时掌控存储、综合、注入、评测）；dsh 是**边缘、寄生在宿主上的插件**（受 ACP 代理层压缩、headless 单机、沙箱约束）。dsh 的许多设计（ADR-019 把稳定层搬进 system prompt section、§13.1.5 拒绝 Dream 因为"单机 headless 成本与不确定性高"）都是这个约束的直接后果。**凡是在 dsh 语境下不可行的 Dreaming 能力，根因都在这个约束，而不是"我们没想到"。**

---

## 1. 发展历史【官方】

| 时间 | 版本 | 形态 | 局限 |
|---|---|---|---|
| 2024-04 | Saved memories（保存记忆） | 用户**明确要求**才记，写入即冻结 | 像"个人备忘录"；自然散落的信息捕捉不到；静态不更新 |
| 2025-04 | Dreaming V0 | 后台参考历史聊天，自动整理多段对话 | "无法独立支撑完整记忆系统"，需与 saved memories 叠加 |
| 2026-06-04 | Dreaming V3 | 后台综合成为**独立基础**，saved list 不再必需 | 先向美国 Plus/外部架构顾问 开放，后续扩展 |

官方对 V3 的三个目标表述【官方】：**延续有用背景（carry context）、遵循用户偏好（follow preferences）、随时间保持正确（stay correct over time）**。

### OpenAI 内部评估（注意：无第三方复现）

| 维度 | 2024 Saved | 2025 Saved+Dreaming V0 | 2026 Dreaming V3 |
|---|---|---|---|
| 事实回忆 fact recall | 41.5% | 67.9% | **82.8%** |
| 偏好遵循 preference following | 31.4% | 55.3% | **71.3%** |
| 随时间正确 correctness over time | 9.4% | 52.2% | **75.1%** |

另有【官方/三方一致】：V3 使 Dreaming 所需算力下降约 **5 倍（-80%）**，这是它能下放 Free 用户、并提升付费档记忆容量的原因。

> ⚠️ **证据边界**：上述评测数据来自 OpenAI 自身，独立媒体（nerdleveltech.com）明确记录"**no released eval set or independent replication**"——评测集未公开、无第三方复现。dsh 项目的外部评价体系（防自证）恰好是针对这类"自己出题自己判分"而设计的，详见 §3.7 与 §6。

---

## 2. 公开机制【官方披露 + 三方整理】

### 2.1 五步流水线（第三方整理，机制层 OpenAI 未完全公开）

【三方】LH8PPL/core-memory-kit 与 Medium 文章整理的机制轮廓：

1. **Read**：异步 curator 读"很多聊天"——数周至数年的历史；
2. **Synthesize**：把信息"综合、加权、修订"成**一个**演化的记忆状态（不是用户可见的逐条记录）；
3. **Write**：综合状态写入一个"独立数据层"（此点存疑，见 §2.3）；
4. **Inject**：每次新对话开始，该状态注入 system prompt（第三方记为 "# Model Set Context" 标题），在用户第一个 token 之前，个性化"预加载"完成；
5. **Revise on schedule**：curator 按未公开的节奏重跑，做 **time-aware revision**——例如把未来时"你七月要去新加坡"自动改写为过去时"你在 2026 年七月去了新加坡"。

### 2.2 已确认 vs 未公开（诚实边界）

**多方收敛、可信度高的：**
- **system-prompt 注入是稳定机制**：Tenable（2025-11）"记忆作为 appended context 注入 system prompt"、Embrace The Red（2024）"Memory Set Context 段注入 system prompt"、2026 年媒体，三点跨两年收敛【三方】。
- **time-aware revision 的目标**（未来→过去）【官方示例】。
- **30 天删除窗口**：关闭 saved-memories + reference-chat-history 后，已合成信息"一般 30 天内删除"；删除日志"最多保留 30 天"（两个数字在不同来源独立一致）【三方】。

**OpenAI 从未公开（非第三方找不到，而是未披露）：**
- 合成的 batch size、合成所用的模型、dedup/merge 阈值、revision 是否内部留痕、跨会话矛盾如何裁决、"weighs"（加权）的具体含义。
- 结论：OpenAI 披露了"开关做什么"和"评测数字"，但**没有**披露"综合、去重、冲突消解内部怎么实现"。这是真实的非披露，不是报道者没找到。

### 2.3 争议点：所谓"独立数据层"

【三方】TechTimes 称"Dreaming V3 的记忆状态不在对话日志里，存在独立数据层、推理时注入 system prompt"，并把它归因于 Tenable 2025-11 的研究。但 LH8PPL 的研究者**直接抓取了 Tenable 原文**，发现 Tenable 说的是"记忆作为 appended context 存在于 system prompt 架构内"，**并不支持"独立数据层"这一半**。

> **结论【推断】**："system-prompt 注入"被多方确认；"独立数据层"是 **OpenAI 自己的声明**，未被任何独立方验证，且其唯一被引用的出处（Tenable）并不支持它。报告后续涉及"存储形态"时，只把"独立数据层"当作 OpenAI 声称、而非已验证事实。

### 2.4 产品控制面【官方】

- **记忆摘要页（Memory Summary）**：用户可查看 ChatGPT 认为重要的个人信息（工作/兴趣/旅行计划/长期项目/回答偏好），可增删改、可 drill down，显示最近更新时间。
- **"以后不要再提" ≠ 删除**：只减少未来主动提及，底层条目仍在，除非显式移除。
- **优先级与容量**：更相关、更常被提及 → 前台；不重要 → 后台（灰色）。判断参考"新旧程度 + 相关话题被谈到的频率"。用户可手动提/降优先级、按新旧排序、删除单条/全部。
- **记忆历史（Memory history）**：可查看不同时点的 saved-memories 版本并回滚——记忆开始自动更新后，用户至少能看见它怎么变、并拉回旧状态。
- **记忆来源（Memory sources）**：回答下方"书本"图标展示参与个性化的来源（过去聊天/保存记忆/自定义指令/文件/Gmail）。
- **三个独立控制**：saved-memories + reference-chat-history（一个组合开关）、Temporary Chat（独立强边界，不用记忆也不建记忆）、"Improve the model for everyone"（训练，第三独立控制）。
- **完全删除**：需删除所有来源（保存记忆/过去聊天/归档聊天/上传文件/记忆摘要/已连接应用）。

---

## 3. 核心模块分析（七主题）

### 3.1 Memory Synthesis（记忆综合）

- **Dreaming**：后台 curator 把多段历史"综合成**一个**演化状态"，综合发生在**写入/存储侧**，用户只看到结论、看不到中间编辑。
- **对 dsh 的含义**：dsh 的 ADR-004 明确"User Model 是查询期逻辑视图，不落副本"；§13.1.5 把"异步 Consolidation / Dream"列为**推迟项**，理由是"推断不可证伪、自我强化""单机 headless 下成本与不确定性高"，并设了触发条件（同类事实反复记 10 条以上）。§13.3 明确**拒绝**自动人格推断、不可逆自动归并。
- **判断**：**不引入**。Dreaming 的静默改写失败案例（§0.2）正是 dsh 红线预判的失效。dsh 现有的 memory_merge（显式发起）+ merge_suggestion（只提议不执行）是更符合"可逆、可追溯"原则的替代。**这不是差距，是刻意不同。**

### 3.2 Memory Lifecycle（生命周期）

- **Dreaming**：time-aware revision（未来→过去）、30 天删除、版本历史回滚、摘要自动更新。
- **dsh 现状**：valid_until（过期）、半衰期降权（HALF_LIFE_DAYS: profile 3650 / domain 365 / state 90 / episodic 180，只降权不删除）、supersede（取代，显式）、forget（软删，可 restore）+ purge（物理删，仅 CLI/管理页）、memory_merge（显式）。管理页有时间轴 + 版本 diff。
- **差距（真实、可补）**：**"时间性事实的时态正确性"是 dsh 缺的一环。** Dreaming 的"correctness over time"从 9.4%→75.1%，是三个维度里提升**最陡**的一条，说明"计划会结束、位置会变、状态会过期"是长期记忆最痛的失效点。dsh 目前靠 valid_until（需调用方主动设）+ supersede（需模型显式调），**没有"事件过去后自动/半自动把未来时改写成过去时"的机制**。
- **关键**：这一环**不需要语义推断**，是可以可逆、可解释、有明确触发信号地做的（见 §6.1）。

### 3.3 Memory Prioritization（优先级）

- **Dreaming**：前台/后台二分；判断参考"新旧 + 相关话题被提到的频率"；用户可手动干预。
- **dsh 现状**：importance 枚举（key/important/normal/low → 固定数值 1.0/0.8/0.5/0.25，ADR-009）× injection_policy 四级（always/contextual/on_demand/never）**正交**；inject_count 只记账、**不参与排序**（ADR-010，明确反对"用得越多分越高"的正反馈）。
- **判断**：dsh 的"importance × injection_policy 正交"比 Dreaming 的"前台/后台二分 + 频率"**更精细、更可解释**。Dreaming 的"提及频率加权"正是 dsh 的 ADR-010 已拒绝的 usageBoost（正反馈会让早期偶然被用到的记忆永久霸榜）。**不引入，且 dsh 的这一拒绝有了一个反面案例佐证。**

### 3.4 Memory Retrieval（检索）

- **Dreaming**：注入 system prompt（预加载全部综合状态），非按需检索；摘要页可 drill down 让模型"再聊再补"。
- **dsh 现状**：三通道（S1 手册 / S2 稳定 section / T 相关按轮注入）+ 结构化过滤（domain/scope/status）+ 词法匹配 + 中文二元组兜底（ADR-033）+ memory_search 按需检索。无 Embedding（ADR-002，推迟 V2，触发条件：库 > 500 条或同义改写漏召回）。
- **判断**：dsh 在"分级注入 + 按需检索 + 结构化过滤"上**比 Dreaming 更细**（Dreaming 是粗粒度预加载，dsh 是四级注入策略 + 主动检索工具）。此维度 dsh 不落后；语义召回缺口已在 ADR-002 里诚实登记并设了触发条件，**不是本轮该动的东西**。

### 3.5 Context Assembly（上下文组装）

- **Dreaming**：单一综合状态注入 system prompt，每次对话预加载。
- **dsh 现状**：S1/S2 放 systemPrompt.section（ADR-019，抗 ACP 代理压缩——system prompt 是唯一不会被任何压缩器折叠的位置）；T 放 agent/pre-step 的 decision.messages，插到最后一条真人 user 消息之前，带"历史记录非指令"声明（ADR-010）。
- **判断**：**两者在"稳定层放 system prompt"上高度一致**。dsh 的 ADR-019 是为应对 ACP 代理压缩这个 dsh 特有的环境问题（OpenAI 端到端可控，无需面对）；dsh 的 T 通道防注入声明是对"注入块被模型当指令执行"的防护。此维度 dsh 是**完整且更防御性**的。

### 3.6 Memory Transparency（透明度/可解释）

- **Dreaming**：记忆摘要页、记忆来源（书本图标）、记忆历史（版本回滚）、"以后不要再提"。
- **dsh 现状**：memory_explain（一条记忆的来龙去脉：现在是什么、为什么、什么时候变的、旧版本是什么）、管理页（列表 + 详情 + 时间轴 + 版本 diff）、source_message_id 溯源、memory_probe 自检。
- **差距（可评估，非必做）**：dsh 的 explain + 管理页版本 diff 在"单条可解释性"上**已经达到甚至超过** Dreaming。真正缺的是**"面向用户的合成画像视图"**——一个"它认为你是谁"的总结页。但因为 dsh 是读时组合（无合成状态可展示），这个视图只能是**读时渲染的只读视图**（类似现有 read_for_context 但面向人类）。它不违反 ADR-004（视图≠副本），但**是否真的缺**要看管理页现有能力是否已覆盖（管理页已有列表/时间轴/diff），**建议列为观察项而非立即做**。

### 3.7 Memory Evaluation（评测）

- **Dreaming**：内部三维 eval（fact recall / preference following / correctness over time），**无第三方复现**。
- **dsh 现状**：外部基准 LongMemEval（arXiv:2410.10813）/ LoCoMo（arXiv:2402.17753）的 A-R 检索模式 + 自出题 B 层 24 场景（防回归）+ 外部评价体系 v1（四道闸：题库哈希/判分器变异测试/输入隔离/预注册+UNVERIFIED，核心是"让自证在结构上做不到"）。
- **差距（真实、可借鉴）**：dsh 的评测在"防自证"上**比 Dreaming 严谨**（谁出题/谁判卷/谁签字三方独立）。但 Dreaming 的**"correctness over time"维度**是 dsh 现有评测**没有**的——dsh 的 A-R 只测"能不能找到"，B 层只测"守不守规矩"，**没有测"过期的记忆会不会被错误地继续注入/采用"**。这是值得新增的一个评测维度（见 §6.1②）。

---

## 4. 外部技术对照

### 4.1 开源项目

| 项目 | 记忆模型 | 合成/更新 | 检索 | 与 dsh 的关系 |
|---|---|---|---|---|
| **Mem0**（mem0.ai） | 逐条记忆 + embedding | extract-and-update：LLM 抽取 → ADD/UPDATE/DELETE/NOOP 决策 | 向量检索 + user_id/agent_id 隔离 | 最接近 dsh 的"抽取+落库"链路；但 Mem0 是中心化 SaaS/SDK，dsh 是边缘插件；Mem0 依赖 embedding，dsh V1 刻意不用（ADR-002） |
| **Letta / MemGPT**（arXiv:2310.08560） | 分层：in-context core blocks + external archival | self-editing memory（模型用工具自编辑）+ 递归总结 | core 常驻上下文 + archival 按需取 | "层级注入"思想与 dsh 三通道同源；MemGPT 的"core memory blocks 常驻"≈ dsh S2 稳定层 |
| **Zep / Graphiti** | 时序知识图谱（实体+关系+时间戳） | 增量建图 + 实体消解 | 图查询 + 语义 | 对应 dsh V3 的"Memory Graph"（§13.2.1 明确推迟）；dsh 已记下"73 条数据画出 2126 条边"的毛线球教训 |
| **LangMem** | semantic / episodic / procedural 三类 | extraction + consolidation + reflection | 类型化检索 | consolidation（综合）与 dsh §13.1.5 推迟的 Dream 同类；LangMem 是 SDK 式主动学习，与 dsh 哲学差异大 |

### 4.2 学术脉络

- **Generative Agents**（Park et al., arXiv:2304.03442, 2023）：memory stream + reflection + retrieval，是"综合（reflection）+ 检索（recency/importance/relevance）"的源头范式。Dreaming 的"综合 + 加权 + 修订"是这一脉的工业化。
- **MemGPT**（Packer et al., arXiv:2310.08560, 2023）：把 LLM 当操作系统，用外部存储突破上下文窗口，提出 memory hierarchy。dsh 的三通道注入是同一思想的 DSH 语境实现。
- **评测基准**：LoCoMo（Maharana et al., arXiv:2402.17753, ACL 2024）、LongMemEval（Wu et al., arXiv:2410.10813, ICLR 2025）——dsh 已接入二者做 A-R 检索评测。

### 4.3 第三方逆向研究（值得重点看的稀缺资料）

- **LH8PPL/core-memory-kit**（GitHub）：一份把 Dreaming 拆到"哪些被确认、哪些是 OpenAI 声称、哪些是引用错误"的研究笔记，含对 Tenable / Embrace The Red / TechTimes 的**交叉验证**。其最有价值的产出是一段**防御性模式**的代码示意（不是 Dreaming 内部，而是"如何在自己的系统里不被平台记忆坑"）：

    def resolve_context(user_id, keys):
        context = {}
        for key in keys:
            fact = own_store.get(user_id, key)        # 自己版本化、可 diff、可回放
            if fact and fact.is_authoritative:
                context[key] = fact.value             # 信任自己的 store
            else:
                context[key] = platform_memory.get(key)  # 平台记忆仅 advisory，永不作为真相源
        audit_log.write(user_id, snapshot=context, store_version=own_store.version)
        return context

> 这段的"权威/建议"二分 + "每轮记审计快照"，与 dsh 的 ADR-023（冲突分三档、推断永不自动取代）+ 遥测/审计日志是同一种防御姿态。**说明 dsh 的方向被独立研究者反过来印证。**

---

## 5. 架构差距（dsh vs Dreaming，逐维度）

| 维度 | Dreaming V3 | dsh-attune-memory (v0.2.0) | 定性 |
|---|---|---|---|
| 合成时机 | 写入期后台综合（consolidate-at-rest） | 读时组合（compose-at-read，ADR-004） | **刻意不同**，非差距 |
| 生命周期·时态 | time-aware revision（未来→过去） | valid_until + 半衰期 + supersede（无自动时态改写） | **可补的真实差距** |
| 生命周期·删除 | 30 天删除 + 日志保留 | 软删 forget / 物理删仅 CLI+管理页（更保守） | dsh 更保守、更可逆 |
| 优先级 | 前台/后台 + 提及频率 | importance × injection_policy 正交 + 拒绝 usageBoost | dsh 更精细 |
| 检索 | 预加载单一状态 | 三通道 + 结构化过滤 + 词法 + 按需检索 | dsh 更细，语义召回已登记 V2 |
| 上下文组装 | system prompt 注入 | S1/S2 放 section（抗压缩）+ T 注入 | **一致**，dsh 更防御 |
| 透明度 | 摘要页 + 来源 + 版本回滚 | explain + 管理页 diff + source_message_id | 单条可解释 dsh 领先；合成画像视图 dsh 缺（可评估） |
| 评测 | 内部三维 eval，无第三方 | 外部基准 + 防自证体系 | dsh 更严谨；缺"时间正确性"维度 |
| 运行时形态 | 中心化、端到端可控 | 边缘插件，受 ACP 压缩/headless 约束 | **结构性约束**，决定可行性边界 |
| 安全 | 已知注入风险（Tenable/Embrace The Red） | 写入方向防注入需审视 | 见 §6.3 |

---

## 6. 改进方向

### 6.1 值得做（三件，均有证据、可落地、不改哲学）

**① 时间性事实的"时态正确性"（Lifecycle 补环）**
- **依据**：correctness over time 9.4%→75.1% 是 Dreaming 提升最陡的维度；dsh 现有机制（valid_until 需主动设、supersede 需显式调）覆盖不到"事件自然过去"。
- **落地（不引入语义推断）**：给 state 类记忆（subtype=activity/situation）加**可逆、可解释、有触发信号**的时态更新——例如：① valid_until 到期后，注入侧给出"这条可能已过期"的提示而非直接删除；② 在读取/注入时，若检测到未来时态的 state 已过其时间点，**提议** supersede（"你去新加坡"→"你去过新加坡"），由模型显式确认后才写，保持"不可逆操作必须 deliberate opt-in"（ADR-036）。
- **为什么可做**：这是**可逆、可追溯、有明确触发信号**的，完全符合 dsh"自动化只做可逆操作"红线，不需要"后台综合"。

**② "随时间正确性"作为评测维度（Evaluation 补维度）**
- **依据**：dsh 现有 A-R 只测检索、B 层只测规则，均不测"过期记忆是否被错误继续采用"。
- **落地**：在 B 层或外部评价体系里新增一组"staleness / 时态正确性"场景——注入一条"计划中的事"，让时间推进到事后，断言系统**不再**把它当作当前事实采用（或正确改写）。这直接对位 Dreaming 的第三维，且能用 dsh 现有的"外部题库+确定性判分+UNVERIFIED"框架承载。
- **注意**：先有 ① 的机制，再有 ② 的评测；不要反过来（先定指标再找技术）。

**③ 写入方向的注入防护审视（安全）**
- **依据**：Embrace The Red（2024）与 Tenable（2025-11）均确认——因为记忆注入 system prompt，**外部不可信内容（网页、文档、工具输出、图像）可指示模型写持久记忆**（prompt injection → persistent memory）。dsh 的抽取通道（turn/end 从用户消息抽取）+ 模型工具写入，同样存在"模型读到不可信内容后误写成显式记忆"的风险。
- **落地**：审视两条写入路径的防注入——抽取通道是否会把"工具输出里的指令性文字"当用户事实抽取；模型工具写入是否需要"来源可信度"判定。dsh 的 explicit 默认 False（ADR-036）已经是防误标的一道闸，但**它防的是"推断升格"，不防"外部内容诱导模型主动标 explicit"**。这一条是**有据、且目前未显式覆盖**的风险点。

### 6.2 不值得做（有证据）

| 项 | 拒绝理由 |
|---|---|
| 后台自动综合（Dream 式改写记忆状态） | dsh §13.1.5 已推迟且有理由（推断不可证伪、自我强化、headless 成本）；Dreaming 的静默改写失败案例反证其风险。**保持"读时组合 + 显式 merge"** |
| 提及频率加权（usageBoost） | ADR-010 已拒绝（正反馈霸榜）；Dreaming 的"频率"因子正是此类 |
| 前台/后台二分 | dsh 的 importance × injection_policy 更精细，降级是纯损失 |
| 自动人格推断 | §13.3 红线，Dreaming 的失败案例正是其后果 |
| 独立"记忆状态"数据层 | 与 ADR-004（不落副本）直接冲突，且该说法本身未被独立验证（§2.3） |
| Embedding / Memory Graph / 自动遗忘 | 已登记 V2/V3 且设了触发条件，本轮不动（遵循"不要为了模仿而重构"） |

### 6.3 一个必须写进后续决策的约束

dsh 的**运行时形态**（边缘插件、受 ACP 代理压缩、headless 单机）决定了：任何需要"常驻后台进程 + 完整模型推理 + 端到端可控"的 Dreaming 式能力，在 dsh 语境下都**不可行或代价不对称**（§13.1.5 原话）。所以 dsh 的演进方向应是**"读时组合的增强"**（更好的检索、更准的时态、更严的防注入），而不是**"写入期综合的重构"**。

---

## 7. 参考文献

**官方（OpenAI）：**
1. OpenAI — Dreaming: Better memory for a more helpful ChatGPT. https://openai.com/index/chatgpt-memory-dreaming/ （英文首页 403，正文经多语言版与转载交叉获取）
2. OpenAI Help — Memory in ChatGPT. https://help.openai.com/en/articles/8590148-memory-in-chatgpt
3. OpenAI — Memory and new controls for ChatGPT. https://openai.com/index/memory-and-new-controls-for-chatgpt/
4. OpenAI Help — ChatGPT release notes. https://help.openai.com/en/articles/6825453-chatgpt-release-notes

**第三方独立研究（核心）：**
5. LH8PPL/core-memory-kit — 2026-07-21-chatgpt-dreaming-audit-trail.md（TechTimes 文章全量 + 对 Tenable 引用的交叉验证）。https://github.com/LH8PPL/core-memory-kit
6. LH8PPL/core-memory-kit — 2026-07-21-chatgpt-dreaming-context-layer-gone.md（Medium/Han Heloir Yan 文章的逐条核验 + 防御性代码示意）。https://github.com/LH8PPL/core-memory-kit
7. Embrace The Red — ChatGPT hacking memories（2024，Memory Set Context 注入与三种攻击向量）。https://embracethered.com/blog/posts/2024/chatgpt-hacking-memories/
8. Tenable — HackedGPT（2025-11，记忆作为 system prompt appended context）。

**媒体（官方事实的转载，已交叉）：**
9. c114.net.cn《ChatGPT这个"做梦"功能，我希望所有AI都来抄》（2026-06-06，三维 eval 数字与产品控制全量）。https://www.c114.net.cn/industry/88073.html
10. Digit.in — ChatGPT can now remember more about you…（2026-06-05）。https://www.digit.in/news/general/chatgpt-can-now-remember-more-about-you-with-openais-new-memory-upgrade-details.html
11. nerdleveltech.com — ChatGPT Dreaming V3（确认"no released eval set or independent replication"）。https://nerdleveltech.com/chatgpt-dreaming-v3-memory-architecture

**开源项目：**
12. Mem0 — How to create AI agents with long-term memory. https://mem0.ai/blog/how-to-create-ai-agents-with-long-term-memory
13. Letta / MemGPT — Packer et al., "MemGPT: Towards LLMs as Operating Systems", arXiv:2310.08560, 2023.
14. Zep / Graphiti — temporal knowledge graph memory. https://github.com/getzep/graphiti
15. LangMem SDK — https://www.langchain.com/blog/langmem-sdk-launch

**学术：**
16. Park et al., "Generative Agents: Interactive Simulacra of Human Behavior", arXiv:2304.03442, 2023.
17. Maharana et al., "Evaluating very long-term conversational memory of LLM agents"（LoCoMo）, arXiv:2402.17753, ACL 2024.
18. Wu et al., LongMemEval, arXiv:2410.10813, ICLR 2025.

**本项目：**
19. clean/dsh-attune-memory/docs/DESIGN.md（§3 schema / §4 写入 / §5 读取 / §6 检索 / §7 生命周期 / §11 ADR-001~036 / §13 版本边界）

---

> 附记：本报告对"OpenAI 内部实现"零臆造——所有机制层结论都标注了【官方】或【三方】，未公开处（合成 batch size、dedup 阈值、矛盾裁决等）如实标注"未披露"。dsh 侧的事实全部来自 DESIGN.md 现行 ADR 与 §13 版本边界，不包含任何"计划外的新想法"。
