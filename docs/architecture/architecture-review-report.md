# 架构汇报：dsh-attune-memory 阶段性架构审查

> 呈交对象：独立架构顾问
> 呈交人：项目负责人（外部架构顾问 窗口）
> 目的：请顾问独立判断——项目是否仍符合最初产品目标、架构是否存在系统性问题、是否出现「打补丁导致复杂度累积」、哪些模块该重构甚至推倒重来、下一阶段该继续开发还是先解决架构问题。
> 原则声明：本文不报喜不报忧，不为自己过去的决策辩护，区分「已从源码/证据核实的事实」与「推测」。凡标【已核实】者均逐行核对过源码或文档；标【推断】者为分析判断。

---

## 0. 一句话结论（先给，后论证）

**方向是对的，但有系统性问题，且「看起来能工作」的部分已经出现静默腐化。** 建议为 **B（局部重构后再开发）**，最高优先级是**统一跨层契约**。证据：至少两个在 0.1.0 发布说明中列为「已交付」的能力，经我逐行核对源码后确认在真实运行时是坏的——memory_preview 工具与整条「兜底抽取」写通道（详见 §4.4、§4.5）。它们坏在「四层各自手写同一份契约、无人统一」这一结构性缺陷上，而不是坏在某一个拼写错误上。

---

## 一、项目当前状态

### 1. 最终想解决什么问题

跨会话长期记忆：让助手在跨月之后，用户**不再需要重复交代**「我是谁、我在干嘛、我喜欢怎么被教」。产品目标是持续维护一份 **User Model**（接近 ChatGPT 的 memory），明确**不是**聊天记录备份、不是向量库、不是「摘要 + 检索 + prompt 拼接」的堆料。

北极星（唯一验收标准）：跨月后用户不再重复交代身份/状态/偏好。

### 2. 已实现的核心能力（截至公开版 0.1.0）

- **写入**：模型主动工具（memory_remember 等 10 个工具）+ turn/end 确定性句式抽取（兜底）。
- **读取注入三通道**：S1（用法手册，system prompt）、S2（always 稳定事实，system prompt）、T（本次相关，pre-step 插入）。
- **写入决策**：等价去重（touch）、冲突三档（自动取代 / 并存标注 / 丢弃）、显式取代（supersede）。
- **生命周期**：active → superseded / archived / deleted；软删（forget）可恢复、物理删（purge）须先软删；惰性维护（tick）与只读维护扫描（scan）。
- **可解释**：memory_explain（时间轴 + 旧版本 + 取代链）。
- **可观测**：计数（memory_counter）、读日志、丢弃日志、memory_probe 自检、管理页诊断。
- **可靠**：写三态（WRITE_OK / WRITE_RETRYABLE / WRITE_FAILED）、幂等回放（memory_request，7 天 TTL）、读失败 fail-open。

### 3. 主要模块

| 层 | 位置 | 模块（文件） | 职责 |
|---|---|---|---|
| 引擎（Python） | engine/attune_memory/（发布树）或 engine/attune_memory/（源码树） | const / db / util / text / store / search / write / read / explain / manager / rpc / memctl（12 个） | 语义核心：校验、去重、冲突、检索、生命周期。**不认识宿主/LLM** |
| 插件（JS） | plugin/dsh-attune-memory/lib/ | index / engine / recall / tools / routes / extract / message / ui / client（9 个） | 宿主接线：通道、RPC 客户端、模型工具、管理页、抽取。**不认识记忆语义** |

引擎约 3.3K 行 Python；插件约 2.9K 行 JS + 管理页 ui.js(435 行) + 侧栏面板 client.js(892 行)。测试：引擎单测 **107**、评估 **24/24**、插件断言（recall 12 / engine 3 / extract 4 / routes 11 / panel 1）、宿主契约 **H1–H5 5/5**。ADR **34 条**。schema **v3**（6 张表）。

### 4. 数据流与调用关系（端到端）

```
[数据产生]
  模型调 memory_remember ──┐
  用户消息 → turn/end 抽取 ─┤（兜底，见 §4.5 已失效）
                          ▼
[进入 Memory]
  tools.js → engine.js(RPC 客户端, 附 __dedupe 幂等键)
           → 常驻 Python 子进程 rpc.py:handle → _call
           → manager → write.consider → store（SQLite 单文件, WAL）
[提取/判断/结构化]  write.py：validate → value_gate → find_similar → 决策表
[存储]   memories(24 列) + memory_change(审计) + memory_meta/counter/observation/request
[检索]   search.py：候选(≤200) → 资格 → 排序(六因子) → 预算(Top-K)
        命中闸门 = Relevance×Confidence×ContextMatch ≥ 0.15（不卡六因子乘积, ADR-018）
[注入]   S1/S2 = systemPrompt.section(order 150/155)，每请求重渲染，压缩免疫
         T = agent/pre-step，插到最后一条真人消息前，进历史（可被压缩）
[更新/合并/冲突]  等价→touch(+0.05)；互斥→三档(≥0.8 取代 / 0.6-0.8 并存 / <0.6 丢弃)；推断永不自动取代
[遗忘/删除]   forget=软删(deleted 可恢复)；purge=物理(仅对 deleted)
[查看/管理]  模型工具(memory_search/explain/forget/...) + 管理页(HTTP) + CLI(memctl)
```

### 5. 稳定 vs 试验

- **稳定**：SQLite 存储、三通道注入（S1/S2 抗压缩已实机验证）、写入决策表、冲突三档、生命周期、可解释、写三态、幂等。
- **试验/过渡**：①ACP 代理层压缩盲区的 T 通道兜底（隔 8 轮重发 + 下一轮核对，**症状级证据，非结论**）；②中文二元组召回（纯内存词法级，FTS5 留给 V2）；③Formation 只到「共享二元组主题簇候选」（M3 部分完成）；④O3 A/B 证据仅 1 轮方向性。

### 6. 目标是否变过

**没有根本变化，只有深化**：v1.0 解决「记得住」→ v1.1 解决「知道自己有没有记住、记错了会不会说」→ 0.1.0 是三方审查 26 项收口。这是同一目标的可靠性递进，不是转向。

---

## 二、架构全景

### 1. 文字架构图

```
┌────────────────────────── 宿主（DeepSeek Harness / Cordis）──────────────────────────┐
│                                                                                      │
│  systemPrompt.section(order=150) ── S1 手册（常量, 压缩免疫）                          │
│  systemPrompt.section(order=155) ── S2 稳定事实（always 层, 压缩免疫）                  │
│  agent/pre-step ─────────────────── T 通道（本次相关, 进历史, 可被压缩, 8 轮兜底重发）   │
│  tools ─────────────────────────── 10 个记忆工具（模型主动调用）                        │
│  session/event(turn/end) ───────── 确定性抽取（兜底写通道）                            │
│  webServer(/dsh-attune-memory/*) ─ 管理页 HTTP（信任栅栏 fail-closed）                │
│                                                                                      │
│                    ┌────────── EngineClient (engine.js) ──────────┐                   │
│                    │  常驻 Python 子进程 · stdio JSON-RPC · 串行   │                   │
│                    │  写操作附 __dedupe 幂等键 · 超时 mayHaveApplied│                   │
│                    └──────────────────────┬──────────────────────┘                   │
└───────────────────────────────────────────┼──────────────────────────────────────────┘
                                             ▼
                    ┌────────────────── rpc.py ──────────────────┐
                    │  _SPEC 参数白名单 → _check_args → _call 分发  │
                    └──────────────────────┬──────────────────────┘
                                             ▼
                    ┌────────────────── manager.py（Memory 门面）───┐
                    │  add/supersede/merge/forget/purge/restore/    │
                    │  search/read_for_context/tick/scan/explain...  │
                    └───┬──────────┬──────────┬──────────┬──────────┘
                        ▼          ▼          ▼          ▼
                     write.py   read.py    search.py   store.py
                     （写决策）  （三通道）  （检索）    （SQLite + 审计）
                        └──────────┴──────────┴──────────┘
                                    ▼
                              SQLite（单文件, WAL, 6 表）
```

### 2. 核心数据模型（schema v3）

| 表 | 用途 | 关键点 |
|---|---|---|
| memories | **唯一事实表**（约束 A，24 列） | type/subtype/subject_type/subject_name/domain_key/content/keywords/confidence/importance/injection_policy/scope/status/source_message_id/source_conversation_id/valid_from/valid_until/superseded_by/created_at/updated_at/metadata/inject_count/use_count |
| memory_change | append-only 变更流水 | action/old_snapshot/new_snapshot/reason/actor/at |
| memory_meta | 键值 | schema 版本、维护时间 |
| memory_counter | 单调计数 | 只观测，不参与排序 |
| memory_observation | 观测流水 | read/discarded/maintenance/inject-* |
| memory_request | 幂等回放 | key/op/response/created_at，7 天 TTL |

类型体系：type ∈ {profile, domain, state, episodic}；subtype（profile: identity/preference/interaction/skill/constraint；domain: convention/fact/decision/pitfall；state: goal/activity/situation；episodic: event/decision/lesson）。importance 枚举→固定数值（key 1.0 / important 0.8 / normal 0.5 / low 0.25）。status ∈ {active, superseded, archived, deleted}。半衰期：profile 3650 / domain 365 / state 90 / episodic 180（天）。

### 3. 各层边界（关键）

| 边界 | 约定 |
|---|---|
| 引擎 ↔ 宿主 | 引擎不 import 任何 harness/LLM；只经 rpc.py 的 stdio JSON-RPC 暴露 _SPEC 白名单的 op |
| 插件 ↔ 引擎 | 插件不碰 SQL、不做语义判断；语义全在引擎；插件只「发请求 + 翻译异常 + 注入文本」 |
| 写语义 | write.py 的 consider() 是唯一写决策点；插件/extract 不自己决定「该不该记」 |
| 读语义 | read.py 的 read_for_context() 是唯一注入出口；search.py 是唯一检索出口 |

---

## 三、过去的重要架构决策（ADR 摘录 34 条中的关键项）

> 完整 34 条在 docs/DESIGN.md §11。此处按「决策/原因/备选/未选理由/代价/可逆性」挑最重要、最能暴露取舍的条目。

### ADR-001 SQLite 单文件存储
- **决策**：SQLite 单文件作为 V1 存储。
- **原因**：零运维、单文件可备份/迁移、事务与并发够用。
- **备选**：Postgres / 向量库 / JSON 文件。
- **未选理由**：本地桌面插件不想要服务依赖；向量库是「检索」问题不是「存储」问题。
- **代价**：中文全文检索能力弱，V1 只能 LIKE + 关键词 + 二元组兜底（把「语义召回」压力后移）。
- **可逆性**：高。存储层隔离在 db.py/store.py，换引擎代价可控。

### ADR-002 V1 不引入 Embedding
- **决策**：结构化过滤 + 词法/二元组检索，Embedding 推迟 V2。
- **原因**：先把「该不该记、怎么取舍」做对，检索召回可用词法兜底；语义相似是后一个数量级的问题。
- **代价**：同义改写召回（「存过但换个说法搜不到」）在 V1 是已知缺口（R1 未做）。
- **可逆性**：中。检索层 search.py 已隔离为三阶段，V2 换 FTS5/Embedding 不碰写管线。

### ADR-003 Interaction Model 是 profile 的 subtype，带 scope，不单建表
- **决策**：profile/interaction 子类型 + scope ∈ {global, domain, task}。
- **原因**：「怎么帮他」是「关于这个人的属性」的子集；单建表会制造「这条进哪张表」的歧义，查询 User Model 还要 union 两张表（人为副本，违反约束 A）。
- **代价**：scope 三级 + domain_key 的组合让「槽位」（subject_type, subject_name, type, subtype, domain_key）变复杂，去重/冲突判定都依赖槽位对齐（缺陷 #1 正是槽位对不上导致重复写）。
- **可逆性**：中低。槽位定义已经渗透到 same_slot/find_similar/scan 多处，改槽位等于改写管线。

### ADR-004 User Model 是查询期逻辑视图（约束 A）
- **决策**：read_for_context 每次现场组装，库里**没有「画像表」**，不落任何副本。
- **原因**：任何「预计算画像」都会变成第二份事实源，迟早和 memories 漂移。
- **代价**：每次注入都要现查现组（性能成本），且「用户画像」没有稳定物化形态（对可视化/调试不友好）。
- **可逆性**：低。这是全项目最核心的不变量，改它等于推翻产品定义。

### ADR-005/019 三通道 + 稳定层搬进 system prompt
- **决策**：S1/S2 用 systemPrompt.section，T 用 agent/pre-step 注入。
- **重大修订（ADR-019）**：稳定层从 systemPrompt.context（历史快照）**搬到 section**。
- **原因**：实机发现用户桌面装了 billion-context（ACP 代理层），它在**请求体层**重写历史、不发 compaction/end 事件、还把 DSH 自带 compaction-basic.auto 关掉——原依赖的「压缩后自动恢复」两条路径在真实环境**根本不触发**。system prompt 是唯一不被任何压缩层折叠的位置。
- **代价**：T 通道仍在历史里，被代理折叠后**无人通知**，只能靠「隔 8 轮允许重发 + 下一轮核对」兜底；这是当前最大的**未关闭风险**（见 §4.6、§5）。
- **可逆性**：中。通道锚点一旦定死，宿主行为一变（如代理连 system 字段也重写）就无解。

### ADR-006/032 写入以模型工具为主，抽取为辅；抽取先预览再落库
- **决策**：主通道=模型主动 memory_remember；兜底=turn/end 确定性句式抽取；抽取走 preview→add。
- **原因**：抽取看不到「这件事后来怎样了」，模型有上下文，写入主动权给模型。
- **代价**：**这条决策目前是坏的**——抽取通道先 preview，而 preview 在 RPC 层已失效（§4.5），所以抽取通道实际一条都写不进。
- **可逆性**：低（写通道是架构主干）。

### ADR-008 引擎常驻 Python 子进程 + stdio JSON-RPC
- **决策**：不把逻辑写进 JS，单独 Python 进程。
- **原因**：引擎要能脱离宿主单独测；记忆读写失败绝不拖垮整轮；stdio 有背压、少一层端口鉴权。
- **代价**：跨进程通信带来「契约要在 JS/Python 两侧各自维护」的问题——**这正是 §4.4 的系统性缺陷根源**。进程崩溃要 fail-open 重建；Windows 编码坑（缺陷 #8）。
- **可逆性**：中。协议简单，但已嵌进 _SPEC/_call/tools.js/routes.js 四处。

### ADR-022 幂等键放请求信封，引擎回放成功响应
- **决策**：__dedupe 键由 JS 客户端生成（写操作自动 randomUUID），引擎 memory_request 回放成功响应。
- **原因**：调用方自带 id 会被模型忘记；内容哈希会吞掉合法重复；不做幂等会写重复。
- **代价**：**幂等键是「每次调用随机生成」，不绑定写内容/操作语义**——所以它只能防「客户端内部传输重试」，防不住「模型重试一次工具调用」（模型第二次调是全新 UUID，照样写两条）。这是审计根③「幂等不彻底」的实情。
- **可逆性**：中。

### ADR-023 冲突三档，推断永不自动取代
- **决策**：互斥冲突按「谁说的 + 多确定」分三档；explicit=False 永不自动取代。
- **代价**：**该不变量的 enforcement 依赖 explicit 字段，而该字段对模型工具不可见**（§4.5），导致「推断永不自动取代」在模型主通道上退化为「靠 schema 描述里一句话劝模型自觉」。
- **可逆性**：中。

### ADR-009 单库 + domain_key，不按工作区拆库
- **决策**：全用户单库，领域用 domain_key 表达。
- **代价**：跨领域去重/冲突全压在一个库、一套槽位上；user_id 字段存在但**目前只支持单用户**（多用户是 C 档暂缓项）。

---

## 四、当前最值得担心的问题（主动自查）

> 格式：问题 → 为什么是问题 → 当前影响 → 未来风险 → 建议处理。

### 4.1 跨层契约被四处手写，无单一事实源（系统性根因）
- **问题**：同一份「哪些 op、哪些参数合法」的契约，分别在 tools.js（模型工具 schema）、routes.js（HTTP 路由）、rpc.py:_SPEC（RPC 白名单）、write.py/manager.py（引擎函数签名）四处各写一份，靠人工同步。
- **为什么是问题**：没有一处能推导出另一处，也没有一条**端到端穿透测试**（模型工具 → JS 客户端 → Python handle → 引擎函数）来守住它们一致。单元测试各层各自绿，但跨层路径无人覆盖。
- **当前影响**：已造成至少 3 个真实故障（§4.4、§4.5）——功能「发出去」了，运行时坏。
- **未来风险**：每加一个 op/字段，都要同步四处，漂移概率随时间单调上升；这是「不断打补丁」的温床。
- **建议**：**最高优先级**。把 _SPEC 变成引擎侧唯一契约源，生成/校验 JS 侧 schema 与 routes 参数；补一条真 RPC 端到端测试（对每个 op 至少 1 例 happy path + 1 例非法参数）。

### 4.2 explicit 默认 True + 对模型不可见（P0，审计根②）
- **问题**：write.py 的 Candidate.explicit: bool = True（危险默认），而 tools.js 的 memory_remember schema **没有 explicit 字段**，模型无法声明「这是推断」。
- **为什么是问题**：引擎里整套「推断处理」（confidence 压到 0.7、禁 always、interaction 收窄、永不自动取代）都挂在 explicit 上，但主通道永远拿不到 explicit=False，这套保护对模型写入**是死代码**。
- **当前影响**：若模型把一条推断写得 confidence ≥ 0.8（它自认为有把握），它会**自动取代一条用户亲口说的 0.95 事实**；系统唯一防线是 schema 描述里一句「推断的不超过 0.7」——纪律层而非引擎层。
- **未来风险**：误覆盖不可逆（旧条目标 superseded 后默认不再注入），且只出现在用户察觉不到的地方。
- **建议**：A 档。默认 explicit=False（安全默认），schema 加 explicit 字段让模型显式声明「用户明说」，引擎保留兜底校验。

### 4.3 硬删除非原子（审计根③）
- **问题**：物理删除是「先 forget（软删）再 purge（物理）」两次独立 RPC。
- **为什么是问题**：两步之间进程崩/请求断，会留下「已软删但没物理删」的中间态；幂等键不绑定操作语义，重试语义模糊。
- **当前影响**：低（管理页才有 hard 入口），但 memory_forget(hard:true) 模型工具**直接不可用**（§4.4 第 2 条）。
- **建议**：B 档。purge 收进一个「先软删再物理删」的引擎侧原子 op，或明确「hard 删除」只能走管理页/CLI，并从模型工具 schema 里移除不可用的 hard 字段。

### 4.4 三个「已交付但运行时坏」的确认故障（已核实，最高警讯）

以下三条我逐行核对源码确认，均非推测：

1. **memory_preview 工具整体失效**。rpc.py:_SPEC 字典**没有 "preview" 键**，但 _call() 有 if op == "preview" 分支、_required() 也有 "preview"。_check_args() 先执行 allowed = _SPEC[op] → 对 preview 直接 KeyError，被 handle() 的 except (…, KeyError) 捕获，返回 {ok: false, error: "'preview'"}。→ 模型一调 memory_preview 就报 READ_FAILED。
2. **memory_forget(hard:true) 不可用**。tools.js 在 hard===true 时向 forget op 传 {id, reason?, hard:true}，但 _SPEC["forget"] = ("id","reason")，_check_args 报 forget 不接受这些参数：['hard']。→ 模型无法硬删（软删 forget 正常）。
3. **管理页 /api/reads 与 /api/discarded 失效**。routes.js 这两个 handler 用了未定义的 json(...) 和 q.get(...)（实际函数是 sendJson、变量是 query），走到即 ReferenceError，被外层 catch 后返回 400。ui.js 的诊断面板（第 206、215 行）正是调这两个接口，因此**诊断页「最近检索/被丢弃候选」两块是坏的**（其余接口正常）。

- **为什么是问题**：这三处不是「边缘 case」，而是发布说明明确宣传的能力（memory_preview、诊断入口），却在**每一层单测都绿**的前提下坏掉——证明测试矩阵存在「跨层盲区」。
- **未来风险**：同类「契约漂移」会继续在其它 op/字段上复发。
- **建议**：随 4.1 一起修；修法就是「单一契约源 + 端到端穿透测试」。

### 4.5 兜底抽取通道整体失效（explicit=False 写路径是死代码）
- **问题**：index.js 的 extractAndStore 对每条候选先 engine.call('preview', candidate)，preview 又因 §4.4-1 必失败 → 抛异常被 catch → add 永不执行。8 条确定性规则（identity/preference/interaction/…/explicit）**一条都落不了库**。
- **为什么是问题**：ADR-006 说「模型为主、抽取为辅」的双通道写入，实际退化成**单通道**；explicit=False 在 write.py 里的整套推断降级逻辑，因为唯一产生 explicit=False 的通道死了，变成**生产环境死代码**。
- **当前影响**：用户明说偏好但模型忘了记时，系统**不再有兜底**；「推断永不自动取代」这个安全不变量在当前版本实际无人触发。
- **未来风险**：双通道设计名存实亡，且无人察觉（抽取失败只写 log.debug，不计数、不进丢弃日志）。
- **建议**：A 档（修 preview 即恢复）；同时把「抽取被 preview 拦截/异常」纳入计数与丢弃日志，避免再次静默。

### 4.6 ACP 压缩盲区：T 通道可能「静默失忆」（最大未关闭风险）
- **问题**：真实桌面的 billion-context 代理在**服务端**折叠历史，不发任何会话事件、不改会话状态，插件「听不见」压缩。S1/S2 已靠搬进 system prompt 免疫（ADR-019），但 T 通道注入块在历史里，会被折掉。
- **为什么是问题**：这是环境约束，不是代码 bug；但「注入了却进不了模型视野」是比「没注入」更隐蔽的失效（哲学②：静默失效最贵）。
- **当前影响**：已有三层缓解——STALE_TURNS=8 允许重发、下一轮 inject_visible/inject_faded_* 核对、S1 自愈指令「看不到就先 search」。但证据都是**症状级**，不能证明一定是 ACP。
- **未来风险**：若代理层连 system 字段也开始重写，则无解（发布说明已诚实标注）。
- **建议**：三关验收第一位盯它（§8）；**暂不做**任何「自动加倍注入」之类的正反馈修复（ADR-021 已否决）。

### 4.7 补丁式逻辑与特殊 case 累积的迹象
- 检索 context_block 里「profile 桶」曾「只有注释没有代码」导致 contextual profile 永不注入（缺陷 #4）；_domain_rules 是专为「领域约定词法撞不上」打的补位；中文二元组是 R2 兜底；STALE_TURNS 重发是 ACP 兜底。
- 这些**每一个单独都合理**，但它们是「在主检索语义之外再叠一层补位」，累积起来使「读管线」成为最难讲清楚、最易再踩坑的模块（§6）。暂未到必须重写的程度，但需要警惕继续叠加。

### 4.8 Memory / Chat History / Profile 概念边界
- **目前边界是清晰的**：Memory=结构化事实（memories 表）；Chat History=不落库（只在会话上下文里）；Profile=不存在独立「画像表」（约束 A，User Model 是读时视图）。丢弃日志保存用户原话前 120 字是「对不存聊天记录」的**有意识例外**（14 天、可 purge、已写进隐私声明）。
- **风险点**：state 与 profile/identity 的边界靠手册文字引导（缺陷 #10「备考被写成 identity」），非引擎强约束；episodic 事实记录在结构上是「又一个 memory」，但语义上是「事件」，与 profile/domain 的注入预算混在同一套桶里。

---

## 五、Memory 系统专项说明

### 1. 什么能成为 Memory？

**有明确判据，但分两层：**
- **引擎层（强约束）**：value_gate（时效/任务/疑问/指代句式过滤）+ validate（类型/主体/白名单）+ 冲突决策。只拦「明显不是记忆」的，不判断「值不值得记」。
- **纪律层（软引导）**：手册教模型「下月还成立吗 / 换个任务还有用吗 / 能从代码文档看出来吗 / 有起止的计划用 state」。

**关键事实**：写决策主要在引擎（value_gate 句式化），但「这条信息是否值得长期记住」的判断**仍在模型手里**（ADR-006），系统只做「校验 + 去重 + 拒绝明显没价值的」。**这不是 bug，是有意设计**——抽取看不到「后来怎样了」。

### 2. 生命周期各步谁负责

| 步 | 谁负责 | 落点 |
|---|---|---|
| 产生 | 模型工具（主）/ 确定性抽取（兜底，已失效） | tools.js / extract.js |
| 提取 | 模型自述 + 8 条句式规则 | extract.js（explicit=False, conf 0.65） |
| 验证/判断 | 引擎 validate + value_gate + consider 决策表 | write.py |
| 存储 | store.insert/update_fields/set_status（tx + 审计） | store.py → SQLite |
| 检索 | search()（三阶段） | search.py |
| 注入 | S1/S2 section + T pre-step | read.py + recall.js |
| 更新 | update（白名单字段，type/subject_type 不可改） | write.py |
| 合并 | merge（同槽位多条收一条，旧转 archived） | write.py |
| 冲突 | 等价 touch / 互斥三档 / 数值按同名属性 | write.py |
| 遗忘/删除 | forget 软删 / purge 物理（先软删）/ tick 惰性归档 | write.py / manager.py |

### 3. 同一事实变化怎么办

以「曾喜欢 A，现喜欢 B」为例，系统**不靠猜**，靠三层：
1. **同槽位等价** → touch（加固置信度，不新增）。
2. **同槽位互斥** → 三档：显式且 confidence ≥ 0.8 → 自动 supersede（A 标 superseded、B 为 active，A 留档可查）；0.6–0.8 → 两条并存、B 打 metadata.conflict_with=A（冲突是 feature）；< 0.6 → 丢弃 + 丢弃日志。
3. **显式指定** → memory_supersede(id, content) 无条件照做。

**判断「替代 vs 并存 vs 暂时失效」的依据**：explicit（谁说的）+ confidence（多确定）+ subject/type/subtype/domain_key 槽位对齐 + key_values（数值冲突只在同名属性上判）。**信息不足时**：<0.6 并存会被丢弃（宁可不记），0.6–0.8 并存并标注（保留不确定性）。**诚实缺口**：上述 enforcement 依赖 explicit，而 explicit 对模型不可见（§4.5），「推断永不取代」目前靠模型自觉。

### 4. Memory 与聊天记录的关系

- 原始聊天记录**是证据，不是记忆**；不落库（除丢弃日志前 120 字、14 天、可 purge）。
- Memory **可追溯来源**：source_message_id / source_conversation_id 列 + memory_change 审计（old/new 快照）+ memory_explain（时间轴）。
- **删聊天记录不影响 Memory**（两条链路独立）；**删 Memory 不影响聊天记录**（forget/purge 只动 memories 表）。
- **但**：注入块（T 通道）作为 user 消息进会话历史，会随历史被压缩；删/折历史会让「当前这轮」的注入失效（§4.6），这不等于删库。

### 5. 是否真形成「长期用户模型」，还是「摘要 + 向量 + prompt 拼接」？

**诚实判断：是前者（结构化的读时 User Model），不是后者，但目前是「弱形态」。**

- **不是**「摘要 + 向量 + prompt 拼接」：没有摘要表、没有 embedding、没有「画像预计算」；是 memories 事实表 + 读写管线 + 读时组装。
- **是**「长期用户模型」的**结构**：有类型/主体/领域/作用域/置信度/生命周期/冲突/可解释，且有约束 A（不落副本）保真。
- **但**当前规模与验证不足以称「长期」：O3 只有 1 轮方向性 A/B；ACP 压缩盲区未关；兜底抽取通道已失效（§4.5）；「模型是否真的复用记忆」尚未有 ≥20 轮证据。**结论：架构是长期用户模型的骨架，尚未被证明「长期有效」。**

---

## 六、最复杂的三个地方

### 1. 写决策表 write.py:consider()（最复杂）
- **为何复杂**：把「等价/互斥/互补/显式取代/合并建议」五种关系 + 「explicit/confidence 分级」+「推断降级」塞进一个函数，还要保证 supersede 的「先插新行再标旧行」（外键顺序）。
- **复杂度来源**：必要的业务复杂度（冲突处理本就不简单）+ 补丁累积（W5 interaction 收窄、W1 type_hint 留痕、conflict_with 并存，都是后加的分支）。
- **是否必要**：大部分必要；但「决策 + 落库副作用（计数/日志）」耦合在同一函数里，preview 不得不复制半段决策逻辑（两份几乎相同的前半段）。
- **可否重划**：可。把「纯判定（return Decision）」与「副作用（计数/审计/丢弃日志）」分离；preview 复用判定、跳过副作用，消除复制。
- **重做会怎样**：决策表用「规则列表 + 优先级」显式化，每条规则自带 reason，让 explain 能直接读决策原因，而不是事后拼文案。

### 2. 读管线 read.py:context_block（最容易继续膨胀）
- **为何复杂**：T 通道要同时满足「词法检索命中 + 领域约定补位（词法撞不上）+ profile 分桶补位（contextual 进不了 C 通道）+ 预算四桶 + 去重 + 压缩重建」。
- **复杂度来源**：**每一段都是为某个真实缺陷打的补丁**（缺陷 #4 profile 永不注入 → 加 profile 桶；缺陷 #5 领域约定隐形 → 加 _domain_rules；ADR-033 中文召回 → 加 bigram）。补丁都合理，但叠加后读管线成了「最难说清、最易再踩」的地方。
- **是否必要**：逐条看都必要；整体看是「词法检索能力不足 → 不断补位」的信号。
- **可否重划**：中。_domain_rules、profile 桶、bigram 都是「召回补位」，可在 V2 统一成一个「召回增强」层，而不是各自硬编码在主流程里。
- **重做会怎样**：context_block 只做「预算分配 + 组装」，召回增强（领域/同义/二元组）作为可插拔的候选源注入，不再堆分支。

### 3. 宿主接线 index.js + recall.js（最可能成为技术债中心）
- **为何复杂**：要同时处理 DSH 的消息深冻结（必须造新对象）、source.kind 缺了会 TypeError、ACP 听不见的压缩、去重「记轮次不记布尔」、注入核对「查会话历史不查 decision.messages」（缺陷 #12）——**全是踩坑得来的环境细节**。
- **复杂度来源**：宿主 API 的隐式约束 + 代理层盲区，**属于环境复杂度，不是业务复杂度**，但都被硬编码在接线里。
- **是否必要**：必要（不处理就崩/就失忆），但它是「对宿主的脆弱假设」的堆积场——已被 ADR-020 契约测试部分守住（H1–H5），却守不住代理层的 ACP。
- **可否重划**：中。把「宿主假设」集中进一层显式 adapter，契约测试直接对 adapter 断言，避免散落在 index/recall 各处。
- **重做会怎样**：recall.js 的注入/核对/去重拆成「纯状态机」（不依赖宿主对象形状），宿主 adapter 只负责翻译消息形状。

---

## 七、如果今天从零开始

> 假设：不兼容当前代码，可保留已验证经验，可删历史包袱。

### 会保留的
1. **约束 A（读时 User Model，不落副本）**——这是产品正确性的根基，也是唯一能回答「为什么不是聊天摘要」的设计。
2. **SQLite 单文件 + 结构化事实表**——零运维、可解释、可审计，对本地插件是正确选择。
3. **写入决策表（等价/互斥三档/显式取代）**——冲突是 feature、推断不取代，这套语义是对的。
4. **三通道注入，且 S1/S2 放 system prompt**——在真实代理层压缩环境下，这是唯一被验证的「压缩免疫」位置。
5. **引擎/插件分层（引擎不碰宿主、插件不碰语义）**——边界是对的，坏在边界的**契约没有单一事实源**。
6. **幂等键放信封 + 引擎回放成功响应**——方向对，只需把键从「随机 UUID」改成「内容+操作的确定性哈希」。
7. **变更流水（memory_change）+ 可解释（explain）**——审计与信任的基础，保留。

### 会修改的
1. **跨层契约**：从「四处手写」改为「引擎侧单一契约源（如 JSON schema），JS 侧 schema 与 routes 参数由它生成或校验」。**这是最重要的修改。**
2. **explicit 默认值**：默认 False（推断是安全默认），模型工具显式暴露 explicit 字段，引擎再兜底。
3. **幂等键**：由「随机 UUID」改为「写操作语义的确定性键」（如 op + 主体 + content 归一化 + 目标 id），才能防「模型重试」而不只是「传输重试」。
4. **硬删除**：合并成引擎侧单一原子操作，或明确 hard 只走管理页/CLI。
5. **抽取通道**：写失败/被拦要计数并进丢弃日志（当前静默）。
6. **preview 与 consider 复用**：判定与副作用分离，消除两份几乎相同的决策前半段。

### 会完全删除的
1. **_SPEC 这种手写参数白名单 + 手工 _call if-elif 分发**：改成由单一契约源生成的分发表，或直接用带 schema 的 RPC 框架。它正是 3 个确认故障的温床。
2. **散落在 context_block 里的补位分支**（profile 桶、领域规则、二元组）各自硬编码：收敛进一个「召回增强」层，或等到 V2 用 FTS5/Embedding 统一替换。
3. **memory_forget 工具上的 hard 字段**（如果决定 hard 只走管理页），避免「看似可用实则报错」的假能力。

### 会新增的
1. **端到端穿透测试**：对每个 op，至少 1 例「真实工具调用 → JS 客户端 → Python handle → 引擎函数」的 happy path + 1 例非法参数，作为契约漂移的门禁。**当前没有这条，是三个故障漏过去的直接原因。**
2. **注入/抽取的完整可观测闭环**：抽取拦截、preview 失败、注入消失都要进计数（现在是盲区）。
3. **一个显式的「宿主假设 adapter」**：把「消息深冻结 / source.kind / 压缩事件 / ACP 盲区」这些环境细节集中，契约测试直接对 adapter 断言。

---

## 八、下一阶段建议

### 结论：**B（需要局部重构后再开发）**，不是 A，也暂不到 C。

- **不到 C**：产品目标未偏、约束 A/三通道/写决策表等核心设计是对的、无「推倒重来」的必要；「架构有根本问题」这个结论**不成立**。
- **不是 A**：存在一个**系统性问题**（跨层契约无单一事实源）已经产出 3 个真实故障 + 1 条死通道（兜底抽取），必须修完再谈扩功能。

### 必须先重构/修复的（按优先级）

| 优先级 | 项 | 性质 | 说明 |
|---|---|---|---|
| P0 | 跨层契约单一事实源 + 端到端穿透测试 | 架构 | 根因；不做则其余修复都是「再打一个补丁」 |
| P0 | 修 _SPEC 缺 preview（恢复 memory_preview 与兜底抽取通道） | 缺陷 | 一行级修复，但牵出整条死通道 |
| P0 | explicit 默认 False + schema 暴露 explicit | 安全不变量 | 让「推断永不取代」真正落地 |
| P1 | 修 routes.js 的 json/q（恢复 /api/reads /api/discarded） | 缺陷 | 诊断面板恢复 |
| P1 | 修 forget 的 hard（要么引擎收下，要么从 schema 移除） | 缺陷 | 消除假能力 |
| P1 | 抽取通道失败/拦截纳入计数与丢弃日志 | 可观测 | 防止再次静默 |
| P2 | 幂等键改语义确定性 | 可靠性 | 防模型重试写重复 |

### 明确不应该做的（下一阶段）

1. **不要再新增 op / 字段**，直到契约单一事实源落地——否则只是继续在四处同步上堆风险。
2. **不要上 Embedding / FTS5 / 记忆图 / 自动遗忘 / 异步 Consolidation**（这些本在 V2_V3_RESERVED，且当前连「召回补位」都没收拾干净，换检索范式是错的优先级）。
3. **不要做「注入消失就自动加倍注入」这类正反馈修复**（ADR-021 已否决，会放大噪声）。
4. **不要把「三关验收」和「继续扩功能」并行**——先关验收盲区，再谈 V2。

### 应该暂缓的

- 语义同义召回（R1）、生成式 Formation（M3 的后半）、多用户（user_id 多租户）、自动遗忘、任何可视化/交互的新功能——**都不是当前正确性的瓶颈**。

### 必须先解决的架构问题（一句话）

**把「一份契约在四处手写」改造成「单一契约源 + 生成式分发 + 端到端穿透测试」，并让「推断永不取代显式事实」这个安全不变量从纪律层落到引擎层。**

---

## 附：本报告的证据来源与诚实边界

- **已核实（逐行核对源码）**：§4.4 三个故障的精确机制（rpc.py:_SPEC、_check_args、handle 的 except KeyError；routes.js 的 json/q；tools.js 的 hard）；Candidate.explicit: bool = True；_SPEC["add"] 含 explicit 而 tools.js schema 不含；index.js 抽取通道 preview-before-add；ACP 盲区的 billion-context 配置证据（compaction-basic.auto=false）。
- **推断（分析判断，非源码事实）**：§4.7 的「补丁累积趋势」、§6 的「重做会怎样」、§7 的重构建议——这些是工程判断，非实测结论。
- **证据边界**：三个故障的「运行时报错」是我从源码推导的必然结果（KeyError/ReferenceError 是确定性的），但**未在真机复现**（本窗口是 外部架构顾问 指挥窗口，不亲自执行；真机复现应交给执行方 Flash 跑）。这一点请顾问知悉。
