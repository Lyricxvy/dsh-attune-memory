# Anima Brain 记忆架构技术研究报告

> 研究对象：https://github.com/spearsheep/anima-brain （Anima Brain，MIT 许可，Python 3.11+，版本 Alpha 0.6.0）
> 研究日期：2026-11-19
> 目的：判断哪些设计可被 dsh-attune-memory（DSH）借鉴、如何吸收，而非直接复制。
> 证据原则：以源码为依据；区分「宣称能力 / 代码实际实现 / 测试验证能力」；关键判断注明源码位置。

---

## 0. 结论先行

1. **Anima Brain 是一个「证据优先 + 时态事实 + 版本化纠正 + 后台巩固」的本地记忆引擎**，在「时间性事实治理」和「派生内容一致性」上比 DSH 当前实现更成熟，是研究「时态正确性」最有价值的参照对象之一（其 GitHub 明确声明支持 DeepSeek Harness 作为宿主，设计语境高度重叠）。

2. **最值得 DSH 借鉴的核心设计只有一个：把「事实的时态类型」和「失效类型」拆成正交字段。**
   - Anima 用 temporal_kind（current / historical / event / uncertain）区分「当前事实、历史事实、一次性事件、不确定」；
   - 用 invalidation（corrected / superseded）区分「被纠正的错误（不该再出现）」与「曾经正确、只是被更新取代（as-of 查询该出现）」。
   - DSH 现在只有 status=superseded 一种失效态，把「正常更新」和「错误纠正」混为一谈；也没有 event_date（事件发生日）与「有效期」的分离。这是 DSH 在《Dreaming 研究报告》已识别的「时态正确性」缺口的**具体落地方案**，且 Anima 有测试覆盖（test_temporal_supersession_and_atomic_correction）。

3. **次值得借鉴的是「revision 版本号 + expected_revision」的乐观并发控制**：Anima 写操作带版本检查，并发编辑失败而非静默覆盖。DSH 有幂等键（ADR-022）但无 revision CAS。

4. **明确不建议引入的**：情绪/关系状态（PAD、valence/arousal）、多模态、角色人格、向量检索（Anima 自己也承认是线性精确扫描、非 ANN）、SQLite 触发器同步索引（DSH 是单写者常驻引擎，无多写者一致性需求）。

5. **Anima 自身的能力边界（诚实）**：其 architecture.md 明确写「quality has not been benchmarked in this new repo」——**它的测试证明的是「机制符合设计」，不是「记忆结果长期更好」**。这与 DSH 外部评价体系（防自证）立场一致，不能因 Anima 测试全绿就认定它记忆能力强。

---

## 1. 项目概况

- **定位**：为 AI 伙伴提供持久记忆与情绪连续性。本地优先，SQLite + FTS5，不依赖任何账号或后端。
- **对外名称** Anima Brain；Python 包名 companion-engine；命令行入口 companion。
- **入口三选一**：Python SDK；MCP 接已有 Agent（明确支持 Claude Code / Claude Desktop / DeepSeek Harness）；HTTP API。
- **核心包不依赖前端**；可选 React 展示页在 examples/showcase/。
- **规模**：src/companion_engine/ 约 30 模块；tests/ 19 个测试文件；核心 memory.py（34.6KB）、retrieval.py（13KB）、temporal.py（7.6KB）、continuity.py（8.5KB）、automation.py。

> 注意：README 明确「这里是独立适配的开发工具，不是 Anima 线上后端的完整开源版」——本文结论只针对此仓库，不代表其线上系统。

---

## 2. 实际架构与运行流程

Engine 是编排层，MemorySystem 在 memory.py，检索在 retrieval.py，时态在 temporal.py，巩固在 automation.py。数据通路：现有 Agent 通过 stdio / 本地 HTTP MCP 接入，Python 代码走 SDK，HTTP 客户端走 API，三者汇到 Engine + MemorySystem，落到 SQLite/FTS5（可选向量）。

### 一次对话往返

1. prepare_turn：预留幂等 pending turn，组装角色设定 + 边界/风格 + 纠正 + 最近历史 + 核心/情景摘要 + 待跟进事项（open loops）+ 原始证据。
2. 生成回复：SDK 调模型，或由 MCP 宿主（如 DSH）自己生成——宿主用自己的模型，不需要额外 chat-model key（host mode）。
3. commit_turn：单事务写入「回复 + 原始证据 + 事实/修订 + 状态」。
4. 后台 worker（可选）：索引、摘要、巩固（consolidation）、对账（reconciliation）。

**反复强调的边界**：
- 「Malformed/failed planner output cannot become evidence or replace the reply」（模型坏输出不能成为证据）。
- 每角色一个 pending turn 防静默排序竞争；精确重试（同 request_id）无额外副作用。
- 付费操作在事务外 reserve/journal，永不自动重试。

---

## 3. 数据模型（memory.py 的 SCHEMA + models.py）

核心理念（memory.py 模块 docstring）：「Evidence-first memory: immutable sources, contextual notes, typed links, temporal facts, citation-backed consolidation」——原始证据不可变，派生描述只帮助检索、永不替代原始证据。

### 3.1 表清单（memory.py SCHEMA）

| 表 | 作用 | 关键约束 |
|---|---|---|
| evidence | 原始对话（不可变） | id, speaker, text, observed_at；UNIQUE(turn_id, speaker) |
| evidence_search | 原始证据 FTS5 索引 | 触发器级联删除 |
| notes | 检索注释 | evidence_id PK；description(≤600) + keywords(≤8) + revision |
| evidence_links | 证据关联 | relation(continuation/clarification/correction) + 双侧 quote |
| documents | 核心笔记/情景摘要 | kind + text + sources |
| memories + memory_details | 结构化事实 | 见下 |
| knowledge_updates | 知识更新队列 | memory_key + operation + processed |
| vectors | 可选 embedding | evidence_id + model + digest + vector |
| maintenance_processed | 后台维护标记 | evidence_id PK |

### 3.2 事实的三层结构（models.py）

事实 = Memory（身份）+ MemoryDetails（语义详情）+ MemoryLifecycle（生命周期）。

**MemoryDetails（语义详情，本文重点）**：
- state_key：状态键（同属性冲突判定用）
- correction_evidence：纠错证据
- recurrence：none / annual（年度重复）
- date_kind：ordinary / celebrate / gentle / deadline
- subject：user / character / shared / third_party（主体归属四态）
- **temporal_kind：current / historical / event / uncertain**（时态类型）
- event_date / event_end（事件发生日/结束日）
- valid_from（有效期起点）
- importance（1–10）、confidence（0–1）、valence/arousal（情绪，DSH 不需要）
- loop_status：none / open / resolved / suppressed / stale
- followup：ask / background_only / never_unless_asked

**MemoryLifecycle（生命周期）**：
- observed_at、last_reinforced_at、mentions
- **invalidation：corrected / superseded | None**（失效类型）
- invalidated_at、valid_until、superseded_by

**Memory（事实本体）**：
- key、text、category（identity/preference/relationship/event/goal/boundary/style/emotional/date）
- source_quote（句级引用）、source_turn_id
- **origin：user_assertion / manual / host_observation**（来源三态）
- revision（版本号）、updated_at

### 3.3 字段间一致性校验（models.py coherent 校验器）

    待跟进事项必须描述用户自己的生活：
    if self.loop_status == "open" and self.subject != "user":
        raise ValueError("Open loops must describe the user's own life")

这类「字段间一致性」直接写进校验器，是「把规则下沉到数据层」的范例，与 DSH 的「类型判据进引擎（ADR-026）」同思路。

---

## 4. 写入与提取

### 4.1 三种写入模式（memory_policy）
- automatic（新角色默认）：自动提取事实；
- review：提取后等用户审批才落库；
- off：只存历史，拒绝事实写入。

（对应 DSH 的「模型工具主通道 + 抽取辅通道」，但 Anima 把「自动/审批/关闭」做成角色持久策略。）

### 4.2 显式陈述 vs 模型推断（origin + source_quote）
- 事实必须带 source_quote（当前用户消息里的原话引用）或「显式注册的、不确定的宿主媒体观察」（ObservedMedia，confidence 强制 < 1）。
- origin 三态：user_assertion（用户明确陈述）/ manual（人工）/ host_observation（宿主观察）。
- MemoryDraft docstring 明写：「A candidate claim, not automatically proven true by a model」。

> 与 DSH 的 explicit 默认 False（ADR-036）是同一哲学：模型提取的事实是「候选」，不是「已被证明的真」。Anima 用 origin + source_quote 落地「谁说的、依据哪句」，DSH 用 explicit + source_message_id。**DSH 缺 source_quote 这层句级引用（DSH 只到消息级）。**

### 4.3 提交原子性
单事务写「回复 + 原始证据 + 事实/修订 + 状态」；幂等；每角色一个 pending turn。

---

## 5. 事实生命周期与冲突治理（本报告核心）

### 5.1 三个时钟分离（temporal.py docstring）

> 「Occurrence dates, observation dates and state validity are separate clocks.」

三个概念严格分开：
1. occurrence date（发生日）：event_date / event_end——事情何时发生；
2. observation date（观察日）：observed_at——我们何时知道；
3. state validity（状态有效期）：valid_from / valid_until——该事实在哪个时间段为真。

temporal.py 的 lookup 支持五种查询模式：current / history / asof / events / observations。

### 5.2 失效二态：corrected vs superseded（关键设计）

lookup 判定语义：
- invalidation == corrected：直接跳过——被纠正的错误，任何历史视图都不该再出现；
- invalidation == superseded：保留在 history——曾经正确、只是被更新，as-of 查询该出现；
- 未知日期保持 unknown，不臆造。

对应 architecture.md：「False claims do not reappear in as-of history; formerly true claims may. Unknown dates stay unknown.」

**这就是 DSH 缺的核心能力。** DSH 的 status 只有 superseded 一种，无法区分「纠正错误（永久抹掉）」与「正常更新（旧值是历史事实，as-of 该保留）」。后果：DSH 无法诚实回答「2026 年 5 月时我当时的地址是什么」（as-of 查询），也无法区分「用户纠正了我记错的事实」与「用户搬家了」。

### 5.3 版本化纠正（revision + expected_revision）
- MemoryCorrection 强制带 expected_revision（≥1）；
- 写事实带 revision 检查，版本不匹配则冲突失败——「Concurrent edits fail instead of silently overwriting」；
- 测试 test_same_claim_changed_metadata_requires_explicit_revision 验证「同一声明改元数据必须显式给版本」。

### 5.4 级联失效
事实被纠正/删除后，依赖它的派生摘要被保守地失效；测试 test_forget_cascades_derived_data 验证。

### 5.5 跨 Key 冲突（诚实边界）
architecture.md：「Cross-key semantic conflicts still need model/human judgment; no perfect contradiction detection is claimed.」——与 DSH 的 ADR-023 + ADR-031 是同一克制。

---

## 6. 检索机制（retrieval.py + memory.py recall）

### 6.1 原始证据检索：RRF 三路融合
architecture.md：「Evidence uses RRF k=40」（raw / note / lexical 三路）：
- raw：可选向量；note：注释向量；lexical：FTS5 词法。
- RRF（Reciprocal Rank Fusion）融合三路，k=40。
- 原始证据、注释、事实/文档的向量是分开命名空间。

### 6.2 事实/情景检索
relevance + recency + importance 加权；待跟进事项用另一套（重要性 + 情绪强度 + 时间衰减 + 日期紧迫度）。

### 6.3 关联证据扩展（expansion）
expand 返回 linked + neighboring 原始证据，保留归属；证据关联带双侧 quote（「Exact quotes on both originals ground links, but do not prove semantic entailment」——诚实）。

### 6.4 时间过滤语义
召回时间过滤指 observation time；事实/事件有效期是另一回事。

---

## 7. 后台维护（automation.py + memory.py maintenance）

### 7.1 触发与批次
- worker 每次为每角色认领一个维护任务（跨进程 lease）；
- 索引 ≤32 chunk；后台维护默认 ≤20 原始消息（可配 2–60）；
- 维护：新/旧注释演化 → 原子写 episode/core notebook → 可选 character core。

### 7.2 防无限循环
「Processed markers differ from citations, so uncited acknowledgements do not loop forever」——「已处理」与「被引用」标记分开，被提及但从未被引用的内容不会无限重处理。

### 7.3 故障恢复（DSH 若做 Dream 应照抄）
- worker 在付费请求期间续租 lease；另一进程无法认领同角色工作；
- dead lease 或 provider/validation 失败 → 任务移到 review，停止该角色自动维护；无盲目付费重试；
- 用户检查 receipt 后显式 retry_memory_job 允许重试；
- pending 工作重启存活；已存向量/已处理批次不重复提交；
- 每日 cap 是滚动 24h 任务数（默认 100），非金额预算。

### 7.4 对账（reconciliation）
对新 current-state/correction 事实，翻页遍历旧候选；精确引用、较晚观察、版本检查共同 gate 状态转移；被取代事实保留为历史；被纠正错误排除；失效强制触发 core 修复。

---

## 8. 工程实现与成本

- 存储：SQLite WAL；SQLite 序列化 reservations/writes；网络调用在事务外。
- 索引一致性：SQLite 触发器让 facts/documents 自动同步进 retrieval_items，保证任何写者（含 MCP、宿主摘要）走同一索引。
- 向量边界：精确余弦扫描 O(向量数 × 维度)，只保留有界 top，非 ANN（「this is a local engine, not an ANN service」）。
- LLM 依赖：chat 用宿主模型；embedding/summary/reconciliation 用配置 writer 或宿主工具；query embedding 失败显式回退 lexical。
- 规模边界：明确「无负载测试容量声明」「SQL 不是可互换接口」「多用户需另做鉴权/配额/独立 worker」。

---

## 9. 测试体系与能力边界

### 9.1 test_full_memory.py 的 12 个用例（覆盖什么）

1. test_original_evidence_attribution_restart — 原始证据归属 + 重启后仍在
2. test_grounded_notes_links_and_revision — 注释落地 + 关联 + 版本
3. test_embeddings_hybrid_and_stale_description — 混合检索 + 过期描述
4. **test_temporal_supersession_and_atomic_correction** — 时态取代 + 原子纠正
5. test_off_mode_atomic_no_memories — off 模式不写
6. **test_forget_cascades_derived_data** — 遗忘级联派生数据
7. test_existing_schema_one_migrates_without_losing_facts — 迁移不丢事实
8. test_export_details_not_silently_capped — 导出不静默截断
9. test_migration_backfills_originals_without_network — 迁移回填不联网
10. **test_same_claim_changed_metadata_requires_explicit_revision** — 同声明改元数据需显式版本
11. **test_core_stays_pinned_and_invalid_consolidation_is_atomic** — 核心固定 + 无效巩固原子性
12. test_history_cursor_has_no_overlap — 历史游标无重叠

### 9.2 四层测试的严格区分（本文判断）

| 层 | Anima 是否有 | 证据 |
|---|---|---|
| 单元测试通过 | 有 | 19 个测试文件全绿 |
| 机制符合设计 | 有 | 12 用例直接断言机制（版本、级联、原子性、迁移） |
| 记忆结果符合原始证据 | 部分 | source_quote 落地机制保证「句级可溯」，但测试多验证结构正确，少验证语义真符合证据 |
| 实际模型长期任务表现更好 | 无 | architecture.md 明写「quality has not been benchmarked in this new repo」 |

**结论：Anima 的测试强在「机制可验证」，弱在「能力可验证」。** 这与 DSH 的外部评价体系立场一致——不能因测试全绿就认定记忆能力可靠。Anima 没有任何等价于 DSH 的 LongMemEval/LoCoMo 外部基准，也没有独立的「防自证」第三方审计。

---

## 10. 与 DSH 的逐维度对比

（DSH 事实来源：engine/attune_memory/db.py 的 DDL_MEMORIES、store.py 的 FIELDS、manager.py、rpc.py、const.py）

| 维度 | Anima Brain | DSH (v0.2.0) | 结论 |
|---|---|---|---|
| 事实时态类型 | temporal_kind: current/historical/event/uncertain | 无（type=state 表达状态但无时态细分） | DSH 缺，可借鉴 |
| 失效类型 | invalidation: corrected / superseded | 仅 status=superseded | DSH 缺，可借鉴 |
| 事件发生日 | event_date / event_end | 无（valid_until 表有效期，非事件日） | DSH 缺，可借鉴 |
| 有效期 | valid_from / valid_until | valid_from / valid_until | 已有 |
| 版本竞争控制 | revision + expected_revision（CAS） | 幂等键（ADR-022），无 revision | DSH 缺，可借鉴（改造） |
| 句级来源引用 | source_quote（当前消息原话） | source_message_id（消息级） | DSH 有，粒度更粗 |
| 来源类型 | origin: user_assertion/manual/host_observation | explicit 布尔 + subject_type | DSH 有等价物 |
| 主体归属 | subject: user/character/shared/third_party | subject_type: user/person/project/org/other | 双方都有，语义不同 |
| 原始证据 | evidence 表（不可变）+ FTS5 | 无独立证据表（只存结构化事实） | 哲学差异：Anima 证据优先，DSH 事实优先 |
| 检索排序 | RRF k=40（证据）/ relevance+recency+importance（事实） | 结构化过滤 + 词法 + 六因子（ADR-018 闸门） | 各有取舍，DSH 不落后 |
| 后台巩固 | consolidation + reconciliation + worker lease | 推迟 V2（Dream） | 哲学差异，DSH 刻意不做 |
| 级联失效 | 纠正/删除使派生摘要失效 | 无派生摘要（读时组合） | DSH 无此问题 |
| 并发控制 | 单角色 pending turn + lease + CAS | 常驻引擎单写者 + BEGIN IMMEDIATE | 双方都有 |
| 情绪/关系 | valence/arousal/PAD/relationship | 无 | DSH 无需求 |

---

## 11. 借鉴建议（逐项：现存问题 / 外部依据 / 实现机制 / 修改范围 / 收益 / 代价 / 独立验证 / 优先级）

### 建议 1：引入「时态类型 + 失效二态」（temporal_kind + invalidation）—— 优先级最高

- **现存问题**：DSH 用 status=superseded 一种失效态，无法区分「正常更新（旧值是历史事实，as-of 应保留）」与「错误纠正（旧值是错的，应彻底抹掉）」；也无法表达「一次性事件（event_date）」与「当前事实（current）」的区别。上一份《Dreaming 研究报告》已识别此缺口（correctness over time 是提升最陡的维度）。
- **外部依据**：Anima 的 temporal_kind 四态 + invalidation 二态 + temporal.py 的 asof/current/history/events 五种查询模式；测试 test_temporal_supersession_and_atomic_correction 覆盖。
- **实现机制**：在 DSH 的 state 类记忆上增加两个字段（metadata 内或独立列）：temporal_kind（current/historical/event/uncertain，默认 current 兼容旧数据）；invalidation（None/corrected/superseded）。corrected 的记忆在 read_for_context 与 memory_search 里**永不召回**；superseded 的记忆在「历史/as-of 查询」里可召回。
- **修改范围**：engine/attune_memory/db.py（加字段/或进 metadata）、store.py、read.py（检索过滤 corrected）、manager.py（supersede 语义细分：正常取代→superseded，纠错→corrected）、explain.py（区分两类失效说明）、rpc.py/tools.js（memory_supersede 增加「纠错还是更新」参数，默认更新）。
- **收益**：诚实回答「过去某时刻真相是什么」；纠正错误后不再被旧值复活；为「时态正确性」评测打地基。
- **代价与风险**：一次 schema 迁移（加字段需兼容旧库）；supersede 语义变化需更新既有测试与 eval。
- **独立验证**：新增 B 层场景「纠正后的错误旧值绝不复活」+「as-of 查询返回当时事实」；可在外部评价体系加「时间正确性」维度（呼应 Dreaming 报告建议）。
- **优先级**：高（是 DSH 已知缺口的具体落地方案，改动可控、可逆）。

### 建议 2：为写入增加 revision 版本号 + expected_revision CAS —— 优先级中

- **现存问题**：DSH 有幂等键（ADR-022，防重复提交），但无「基于旧版本的乐观锁」。并发/乱序更新时，基于过期状态的 supersede 可能静默覆盖更新内容。
- **外部依据**：Anima 的 revision + expected_revision（「Concurrent edits fail instead of silently overwriting」）；测试 test_same_claim_changed_metadata_requires_explicit_revision。
- **实现机制**：memories 表加 revision 整数列（每次更新 +1）；memory_supersede / memory_update 接受 expected_revision，不匹配则返回 CONFLICT（可重试）。
- **修改范围**：db.py、write.py、manager.py、rpc.py、tools.js。
- **收益**：消灭「基于旧值覆盖新值」的静默数据丢失。
- **代价**：工具参数 +1，调用方需先读 revision 再写。
- **独立验证**：B 层新增「并发 supersede 冲突被拒」场景。
- **优先级**：中（DSH 是单写者常驻引擎，并发窗口比 Anima 多写者小，收益边际递减；但作为防御性正确性仍值得）。

### 建议 3：source_quote 句级引用（可选，低优先级）

- **现存问题**：DSH 的 source_message_id 只到消息级，无法回答「这条记忆来自用户原话的哪一句」。
- **外部依据**：Anima 的 source_quote（事实必须带用户消息里的原话引用）。
- **实现机制**：写入时要求/可选携带 source_quote 文本，存进 metadata，explain 里展示。
- **收益**：提升可解释性（呼应哥哥「宁漏记不误标」与「可追溯」诉求）。
- **代价**：写入时多一个字段；对中文短句抽取通道，句级引用可能不稳定。
- **优先级**：低（DSH 已有消息级溯源，句级是锦上添花）。

### 建议 4：若将来做后台巩固（Dream），照抄「lease + review + 显式 retry + 无盲目重试」—— 仅存档，不现在做

DSH 已把 Dream/consolidation 推迟到 V2，且有理由。本建议**不是现在做**，而是「当触发条件满足时，故障恢复机制照抄 Anima 的 worker lease + review + retry_memory_job + 每日任务数 cap」这一套成熟范式，避免自己造轮子。

---

## 12. 不建议引入的机制

| 机制 | 理由 |
|---|---|
| 情绪/关系状态（valence/arousal/PAD/relationship axes） | DSH 是记忆插件，不是角色引擎，无此需求 |
| 多模态（语音/图像/视频通话） | DSH 无此需求，且引入 provider 依赖 |
| 角色人格（persona/core_persona） | DSH 维护 User Model，不是角色设定 |
| 向量检索（Anima 式线性精确扫描） | Anima 自己承认非 ANN、O(n×d)；DSH 已把 Embedding 推迟 V2 且设了触发条件（库 > 500 条） |
| SQLite 触发器同步索引 | DSH 是单写者常驻引擎，无多写者一致性需求；触发器是 Anima 为 MCP/宿主/摘要多写者而设 |
| 独立 evidence 表（证据优先） | 哲学差异：DSH 是「读时组合、不存全量历史」（拒绝「保存完整聊天记录当记忆」）；Anima 的「不可变原始证据」是它的哲学，不是 DSH 要学的 |
| 情绪连续性反馈（continuity.py 的 REACTIONS 正则） | DSH 无角色情绪，不适用 |

---

## 13. 源码与引用

**Anima Brain 源码（https://github.com/spearsheep/anima-brain ，main 分支）：**
1. `src/companion_engine/` 下的 `memory.py` — MemorySystem、SCHEMA、record_exchange、annotate、expand、recall、consolidate、maintenance_sources
2. src/companion_engine/models.py — MemoryDetails、MemoryLifecycle、Memory、MemoryDraft、MemoryCorrection、TemporalQuery、NotebookEntry
3. src/companion_engine/temporal.py — local_today、lookup（三时钟分离、五种查询模式、corrected/superseded 判定）
4. src/companion_engine/retrieval.py — LayeredMemory（RRF 载体、触发器同步、精确向量扫描）
5. src/companion_engine/automation.py — worker、lease、review、retry
6. src/companion_engine/continuity.py — Continuity（情绪反馈，DSH 不借鉴）
7. tests/test_full_memory.py — 12 个核心记忆测试
8. docs/architecture.md — 架构与「source provenance and non-parity」（诚实声明能力边界）
9. docs/memory-automation.md — 后台维护、故障恢复、规模边界
10. docs/features.md — 功能与宿主接入（明确支持 DeepSeek Harness）
11. README.zh-CN.md — 项目定位与「记忆怎么分工/怎么找回来/历史怎么整理」交互架构入口

**DSH 当前实际代码（已核对，非旧文档）：**
12. engine/attune_memory/db.py — DDL_MEMORIES（memories 表字段：valid_from/valid_until/superseded_by/status 等，无 temporal_kind/event_date/revision/invalidation）
13. engine/attune_memory/store.py — FIELDS、transaction、Store
14. engine/attune_memory/manager.py — supersede、scan（:308 过期判定、:421 superseded 180 天归档）
15. engine/attune_memory/temporal.py — **不存在**（DSH 无独立时态模块）

---

> 附记：本报告对 Anima Brain 的机制描述全部来自其源码与文档（main 分支，经镜像获取），逐条注明文件位置；对 DSH 的描述全部来自 engine/attune_memory/ 实际代码，非旧设计文档推断。Anima 未在源码/文档中明示的机制，一律标「未公开/未在源码确认」，不自行补全。结论遵循「不盲目借鉴、不自行创造标准、不以项目测试全绿当作记忆能力可靠的证明」。
