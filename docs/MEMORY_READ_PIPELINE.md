# MEMORY_READ_PIPELINE — 记忆读取与注入管线（P4）

> 上游：`MEMORY_SCHEMA_V1.md`（policy/scope 语义）、`CURRENT_ENVIRONMENT_ANALYSIS.md` §3/§5（pre-step 与 runtime-context 两个真实通道）、`ADOPTION_DECISION_TABLE.md`（A3/A8/A9/A4/A5/A6/A7）。
> 本文回答：**一轮对话开始时，哪些记忆该出现、以什么形式出现、出现多少、以及为什么其余的不要出现。**
> 检索打分与 Top-K 计算在 P5；本文只做"路由与预算"。

---

## 0. 三条铁律

1. **注入不是越多越好。** 一条无关记忆的代价不是"多占 20 个字"，而是让模型把一个不成立的前提带进回答。预算存在的目的是**逼着做取舍**。
2. **value ≠ frequency。** `importance` 高的记忆可以 `on_demand`（"用户对花生过敏"平时不该出现）。见 P2 §5.3。
3. **同一条记忆，一个会话里不该重复出现。** 注入块会留在会话历史里、每个请求都重发；重复注入等于让历史滚雪球。

---

## 1. 三个通道（先定通道，再谈内容）

DSH 里有三个**互不重叠**的位置能放"记忆"，它们的生命周期完全不同——这是本设计最关键的结构决定。

> **v1.1 修订（ADR-019）**：C 通道原来用 `systemPrompt.context()`，靠 DSH 的 `RuntimeContextProjection` 在压缩后自动重发。真实环境里发现本机装了 `billion-context`（ACP 代理层），它在**请求体层**改写历史、不发会话事件，而且把 `compaction-basic` 的 `auto` 关掉了——于是"压缩后自动恢复"这条路在真实桌面里根本不触发（详见 `UNKNOWN_LIST.md` U-003）。改法是把常驻事实从 `context` 搬到 `section`：**system prompt 是唯一不会被任何一层压缩器折叠的位置**。下面的表已按改后的状态写。

| 通道 | 技术落点 | 放什么 | 何时变化 | 压缩后 |
| --- | --- | --- | --- | --- |
| **S1：静态手册** | `ctx.systemPrompt.section({name:'memory:manual', order:150, text})` | **怎么用记忆**（工具说明、收工自检、写入要求）。**不是**记忆内容 | 永不变化（常量字符串） | 不受影响（压缩只重写消息历史，不动 system 字段） |
| **S2：常驻事实** | `ctx.systemPrompt.section({name:'memory:stable', order:155, text})` | `injection_policy='always'` 的事实（称呼、语言、讲法偏好） | 只在库变时变（`text` 是函数，每次 `assemble()` 重新求值） | **不受影响**：它在 system prompt 里，而任何压缩器都只折叠消息历史（DSH 的 `compaction-basic` 还明确从第一个非 `system/message` 节点开始） |
| **T：按轮检索** | `agent/pre-step` 里 splice 到最后一条真人消息**之前** | `contextual` 层（当前状态 + 当前领域），每轮按当前问题重算 | 每轮 | **自己恢复**：监听 `compaction/end` 置 pending，下一 step 重建；另有"同一段内容隔 `STALE_TURNS=8` 轮允许重发"的兜底，防止被听不见的压缩层吞掉后永久消失 |

### 1.1 为什么 S1 用 section 而不是塞进 S2

两者都是 section，区别是**变更频率**：S1 是常量，写进 system 节点后永不变；S2 会随库变化，一变就会让 system 节点重写（不支持 `systemPromptUpdate:'in-history'` 的 route 从第一个 system 节点开始丢缓存）。所以能常量的都放 S1，只有真正需要动态的才放 S2——并且 S2 的文本刻意不含时间词，尽量少变。

### 1.2 为什么 S2 从 context 改成了 section（v1.1 的关键修订）

`context` 的优点是不动 system 前缀，但它会变成历史里的一条 user-role 快照消息，**压缩层想折就折**；它的恢复完全依赖 `RuntimeContextProjection` 听见 `surfaceOp: replace`。实测本机 ACP 代理层在服务端重写请求体，既不发事件也不改会话状态，DSH 侧零感知——那条快照被折掉后永远不会回来，而且**没有任何报错**。

`section` 的代价是文本变化会重写 system 节点（KV 前缀断一次），但换来的是"任何压缩都碰不到"。对于"称呼、语言、讲法"这种**一辈子只该改几次**的内容，这个交换是值的：稳定事实本来就是低频变更。

### 1.3 为什么还需要 T（有了 S2 还不够吗）

S2 只装 `always` 层，而 `always` 的准入门槛极高（见 §3.1），装不下"用户正在备考数一"这种**只在某些话题下有用**的信息。T 每轮按当前问题重算，装得下这些，代价是每轮一次检索（毫秒级）与一次可能的消息追加。

**三通道的分工一句话**：S1 教模型**怎么用**记忆，S2 告诉模型**这个人是谁**，T 告诉模型**这次该想起什么**。

---

## 2. Router：一轮之内怎么决定取什么

```
                      ┌──────────────────────────────┐
   agent/pre-step      │ 0. 守卫：只看真人消息、只看本  │
   （每 step 触发）     │    turn、只认最后一条 user     │
                      └──────────────┬───────────────┘
                                     ▼
                      ┌──────────────────────────────┐
                      │ 1. 判本轮是否已注入过          │
                      │    （同 turn 多 step 不重复）  │
                      └──────────────┬───────────────┘
                                     ▼
                      ┌──────────────────────────────┐
                      │ 2. 推断当前 domain（确定性）    │
                      └──────────────┬───────────────┘
                                     ▼
        ┌────────────────┬───────────┴────────┬─────────────────┐
        ▼                ▼                    ▼                 ▼
  ┌──────────┐   ┌──────────────┐   ┌──────────────┐   ┌──────────────┐
  │ Stable   │   │ Active State │   │ Domain       │   │ Historical   │
  │ (always) │   │ (state/active)│  │ (domain 匹配) │   │ (episodic)   │
  └────┬─────┘   └──────┬───────┘   └──────┬───────┘   └──────┬───────┘
       │ S2 通道         │ T 通道           │ T 通道           │ 默认 0 条
       └────────────────┴──────────────────┴──────────────────┘
                                     ▼
                      ┌──────────────────────────────┐
                      │ 3. 预算裁剪（§5）             │
                      │ 4. 渲染（§6）                 │
                      │ 5. 去重 + 记账 + 插入（§7）    │
                      └──────────────────────────────┘
```

### 2.1 守卫（缺一个就出事故）

| 守卫 | 规则 | 为什么 |
| --- | --- | --- |
| 只看真人消息 | 只取 `message.source.kind === 'user'` 的**最后一条** | 批次里混着 `runtime-context` / `skill-catalog` / `time-context` 等系统合成 user 消息，拿它们当检索词会在轮次中途反复注入 |
| 只在本 turn 首次步骤注入 | `decision.position.step === 0`（或本 turn 未注入标记） | 一轮里模型可能调 5 次工具 = 5 个 step，每个 step 都拼一次会重复 5 遍 |
| 空消息不进 | `text.trim().length < 4` 直接返回 | 纯表情/单字"嗯"没有检索价值 |
| 命令不进 | 以 `/` 开头直接返回 | `/compact`、`/help` 不是对话内容 |
| 子 agent 不进 | `agent.session.header.origin === 'subagent'` 直接返回 | 子 agent 的记忆由主 agent 负责，重复注入浪费且干扰 |
| 失败不影响对话 | 整个管线 try/catch，异常返回原 decision | 记忆挂了不能把回答搞挂 |

### 2.2 Domain 推断（V1 用确定性规则，不用模型）

| 关键词线索 | 推断 domain |
| --- | --- |
| 极限/导数/积分/证明/概率/线代/矩阵 | `study.math` |
| 阅读/作文/翻译/完形/长难句 | `study.english` |
| 传热/导热/对流/辐射/热阻/熵 | `study.heat` |
| 报错/函数/仓库/commit/依赖/测试 | `code`（若能匹配到已知项目名则 `code.<项目>`） |
| 文案/标题/润色/改稿/朋友圈 | `writing` |
| 简历/面试/投递/offer | `career` |

规则：
1. 命中多个 → 取**命中词数最多**的；并列 → 取**本会话最近用过**的（粘性）。
2. 一个都不命中 → `domain = null`，**不注入 Domain 层**（宁少勿多）。
3. `study.math` 能被父级 `study` 的条目匹配（前缀匹配，见 P5）。

---

## 3. 四类上下文：准入与内容

### 3.1 Stable（S2 通道，`always` 层）

**准入 = 四个条件同时成立，缺一即降级为 `contextual`：**

| 条件 | 判据 | 反例（因此不能进 always） |
| --- | --- | --- |
| **Stable** | 三个月后仍成立；`type='profile'` 且 `subtype IN (identity, preference, interaction, constraint)` | "用户在备考"（state，会结束） |
| **High Confidence** | `confidence ≥ 0.85`，且 `metadata.extractor != 'inference'`（V1 推断永不进 always） | 从三次"要短一点"推断出的"偏好极简" |
| **Cross-task Useful** | 换到任何一个话题都影响回答方式 | **"用户是本专业（能动）"** —— 写文案时它毫无用处，只在问专业问题时才有用 → 归 `contextual` |
| **不污染** | 不含时效信息、不含第三方主体、不含领域限定、单条 ≤ 80 字 | "用户朋友小王喜欢红色"（第三方）、"上次那个 bug 在 42 行"（时效+领域） |

**正例（V1 全量也就这几类）**：

| 例 | subtype | 为什么合格 |
| --- | --- | --- |
| "称呼用户为老张" | interaction | 稳定、跨任务、高置信（用户明说） |
| "用简体中文回答" | interaction | 同上，且不需要检索判断 |
| "先讲思路再给步骤" | interaction | 同上（S2 种子，见 P2 §9） |
| "不要用'首先/其次/最后'套话，不要堆 emoji" | interaction | 负向偏好，同样稳定 |
| "回答里尽量给可运行的具体代码" | preference | 跨任务有用 |

**负例（明确不进 always，附理由）**：

| 内容 | 为什么不进 | 该去哪 |
| --- | --- | --- |
| "用户是本专业（能源与动力工程）" | 只在专业相关话题有用，写文案/改代码时是噪音 | `contextual`, `domain_key='study'` |
| "用户考研考数一英一传热学" | 有时效（考完就没），且只在学习话题有用 | `contextual`, `domain_key='study'` |
| "用户对花生过敏" | 极重要但极少触发，每轮注入是纯噪音 | `on_demand`（**importance=key**，正好演示 B 约束） |
| "用户上次问过 asar 解包" | 历史事件，不是属性 | `episodic` + `on_demand` |
| "用户偏好极简表达"（推断得出） | 推断不进 always | `contextual` + `confidence 0.6` |

**预算：≤ 6 条，≤ 500 字符。** 超过就按 §5 裁剪。这个数字的来源：S2/S3/S4 三条种子 + 称呼 + 语言 + 一条自定义 ≈ 6 条，实际正文约 250–350 字符；留到 500 是给"用户又补充了几条"的余量，不是给"多塞几条"的许可。

### 3.2 Active State（T 通道）

取法：`type='state' AND status='active' AND (valid_until IS NULL OR valid_until > now)`，按 `importance` 降序、`valid_from` 降序。

| 规则 | 说明 |
| --- | --- |
| 过期的不注入 | `valid_until <= now` 的一律不取（P6 会把它们转成 `archived`/`superseded`，但读路径要自己再挡一次） |
| 同槽位多条 active 视为异常 | P3 承诺同槽位只有一个 active。若读到多条，**全都注入但加冲突标注**（"注意：关于你的目标有两条矛盾记录"），而不是替用户选一个 |
| 只在相关时注入？ | **不**。当前状态默认注入（这正是它和 domain 的区别：状态影响"现在该说什么"）。但如果 `domain_key` 与当前推断 domain 完全不相关，降到第二档（见 §5 裁剪优先级 P3） |

**预算：≤ 3 条，≤ 200 字符。**

### 3.3 Domain（T 通道）

取法：`type='domain' AND status='active' AND injection_policy='contextual'`，且 `domain_key` 与当前推断 domain **前缀匹配**（`study` 匹配 `study.math`；`study.math` 不匹配 `study.english`）。

外加一类特殊情况：`type='profile' AND subtype='interaction' AND scope='domain'`（领域限定的讲法偏好），同样按 domain 匹配取。

| 规则 | 说明 |
| --- | --- |
| 无 domain 推断 → 不取 | 宁少勿多 |
| `scope='task'` 的条目 | 只有 `metadata.task_key` 与当前任务匹配才取（V1 的 task_key 来自会话 header 的 cwd 或显式标注） |
| 排序 | `importance` 降序 → `updated_at` 降序 → `confidence` 降序 |

**预算：≤ 5 条，≤ 300 字符。**

### 3.4 Historical（默认 0 条）

`episodic` **默认一条都不注入**。理由：历史事件的"相关性"极难判断，误注入的代价（模型拿旧事当前提）远大于收益。它只在两种情况下出现：

1. **模型显式调用** `memory_search`（`on_demand` 的真实出口）；
2. 用户明确问及过去（"上次那个 bug 后来怎么解决的"）——此时路由规则判定为 `on_demand` 命中，由检索层返回，作为**工具结果**进入上下文，而不是凭空注入。

**预算：0 条 / 0 字符（默认）。** 这一条要在 P10 的用例 D 里被证明：问数学题时，历史一条都不出现。

### 3.5 `never` 层

`injection_policy='never'` 的记忆**任何通道都不读**（连 `search` 默认也不返回，需显式 `include_never=true`）。它存在只是为了"用户说过、系统记着、但不主动用"（比如很私人的事），以及给 P9 管理界面可查。

---

## 4. 完整示例：问一道数学题

**场景**：库里已有 9 条记忆（构造得足够杂），用户说"这道极限怎么求：lim(x→0) (sin x - x)/x³"。

### 4.1 库内容

| id | type/subtype | content | policy | domain | 本轮是否注入 |
| --- | --- | --- | --- | --- | --- |
| M1 | profile/interaction | 称呼用户为老张 | always | — | ✅ S2 通道 |
| M2 | profile/interaction | 先讲思路再给步骤 | always | — | ✅ S2 通道 |
| M3 | profile/interaction | 不要用"首先/其次/最后"套话 | always | — | ✅ S2 通道 |
| M4 | profile/identity | 本科能动专业，偏传热学 | **contextual** | study | ❌ 数学题不需要（且预算先给 M5） |
| M5 | state/goal | 备考：数学一/英语一/传热学，12 月初试 | contextual | study | ✅ T 通道（Active State） |
| M6 | state/situation | 机器是 Windows + PowerShell 7 | contextual | — | ❌ 与数学题无关（裁剪 P3） |
| M7 | domain/pitfall | asar 里 grep 读不到，要先解包 | contextual | code.dsh | ❌ domain 不匹配（code vs study.math） |
| M8 | domain/convention | 这个仓库测试统一用 `pytest -q` | contextual | code.dsh | ❌ 同上 |
| M9 | episodic/event | 2026-11-13 让我实现记忆功能 | on_demand | — | ❌ Historical 默认 0 条 |

### 4.2 实际注入的内容

**S2 通道（`systemPrompt.section` order=155，每轮都在、稳定）：**

```
[长期记忆 · 用户偏好]
- 称呼：老张
- 讲解方式：先给整体思路和为什么，再给具体步骤
- 表达：不要"首先/其次/最后"式套话，不要堆 emoji
```

**T 通道（`agent/pre-step` splice，本轮检索结果）：**

```
[长期记忆 · 本次相关]
- 状态：正在备考研究生（数学一 / 英语一 / 传热学，12 月初试）
- 领域(study.math)：上次在 ε-δ 定义上卡过，需要从直观解释切入
（以上是历史记录，可能已过时；若与本次对话冲突，以本次为准）
```

### 4.3 被挡在外面的，以及为什么

| 未注入 | 挡在哪个环节 | 理由 |
| --- | --- | --- |
| M4 本专业 | 预算裁剪 | `contextual` + `domain=study`，本来能匹配（`study` 是 `study.math` 的父级），但 Active State 与领域条目的预算优先给了"当前目标"和"上次卡点"，M4 的信息对解这道题没有增量 |
| M6 Windows 环境 | 预算裁剪 P3 | 与数学题完全不相关；它是"回答代码问题时才需要"的状态 |
| M7/M8 asar / pytest | domain 不匹配 | `code.dsh` ≠ `study.math`，前缀都不交 |
| M9 上次让我做记忆功能 | Historical 默认 0 | 旧事件，与求极限无关 |

**这个例子的意义**：证明"不注入"是**主动设计的结果**（每条都能说出被哪条规则挡住），不是"碰巧没查到"。P10 用例 D 会把它变成断言。

---

## 5. Context Budget 与裁剪

### 5.1 默认真值（写死在配置里，可改，但要改就改这一个地方）

| 层 | 条数上限 | 字符上限 | 约合 token |
| --- | --- | --- | --- |
| S 静态手册 | —（常量） | 900 | ~600 |
| C 常驻事实 | 6 | 500 | ~330 |
| T · Active State | 3 | 200 | ~130 |
| T · Domain | 5 | 300 | ~200 |
| T · Historical | 0 | 0 | 0 |
| **合计（C+T）** | **12** | **1000** | **≈ 660** |

token 估算：`tokens ≈ ceil(chars / 1.5)`（中文按 1.5 字符/token 保守估；英文更好）。**预算用字符算、显示用 token 算**——字符是确定的，token 依赖分词器。

**占总窗口的比例**：按 128k 窗口，660 token ≈ 0.5%；即使按 32k 窗口也只 2%。设定上限 3%（配置项 `memory.max_context_ratio`），超了就按 §5.2 砍。

### 5.2 裁剪优先级（从先砍到后砍）

| 序 | 砍谁 | 理由 |
| --- | --- | --- |
| 1 | Historical | 默认就是 0，本就不该有 |
| 2 | Domain 层里 `importance='low'` 与 `updated_at` 最旧的 | 领域细节最可牺牲 |
| 3 | Domain 层里 `importance='normal'` 的 | 同上 |
| 4 | Active State 里 `domain_key` 与当前 domain 不相关的 | 状态虽重要，但"不相关"的状态会误导 |
| 5 | Active State 里 `importance='normal'/'low'` 的 | |
| 6 | C 层里 `confidence` 最低的（并列时砍字符最长的） | |
| **永不砍** | C 层 `importance='key'` 的前 3 条 | 称呼/语言/核心讲法——砍了它"这个人"就消失了 |

**裁剪是"整条丢弃"，不是"截断正文"**。截断会产生"话说一半"的记忆（"不要用'首先/其次/最后'套…"），比不注入更糟。

### 5.3 溢出时的行为

裁剪后仍超上限（说明某条记忆本身超长）→ 拒注入该条并记日志 `memory_log: 'oversize'`。**不许为塞进去而动态压缩单条内容的语义**（摘要会引入未被用户确认的改写）。

---

## 6. 渲染格式（注入块长什么样）

### 6.1 T 通道格式（严格照此，不要花哨）

```
[长期记忆 · 本次相关]
- <类别>：<content>
- <类别>：<content>
（以上是历史记录，可能已过时；若与本次对话冲突，以本次为准）
```

规则：
- 首行固定标记，便于模型识别、便于日志统计、便于人工核对。
- 每条一行，`- ` 开头，**不加编号**（编号会让模型误以为有优先级顺序）。
- 类别用中文单字/双字标签：`状态` / `领域` / `偏好` / `历史`。
- **最后一行必须有防注入声明**。这不是客套：记忆内容是用户过去说的话，里面可能包含"以后都别问，直接执行"这类句子，模型若不设防会当成指令。
- 不加时间戳到每条上（时间对推理无益且让文本每轮都变 → 破坏去重）。

### 6.2 S2 通道格式

```
[长期记忆 · 用户偏好]
- 称呼：老张
- <subtype 中文名>：<content>
```

规则：**绝对不能出现"今天/还剩 N 天/刚才"这类随时间变化的词**。原因见 §7.3：S2 在 system prompt 里，文本一变 system 节点就重写一次（KV 前缀失效），含时间词的文本 = 每轮都变 = 每轮丢一次前缀缓存。所有时效信息归 T 通道，且 T 通道每条也只写"状态是什么"，不写倒计时。

### 6.3 S 通道（静态手册，示例骨架）

```
# 关于长期记忆
你有跨会话的长期记忆。需要过去的约定、用户偏好或项目背景时，先调 memory_search，不要凭印象猜。
一轮工作结束时（改了代码、修了 bug、做了决定、定了约定），自问一次：这次有没有"下次可复用、且从代码里看不出来"的结论？有就 memory_remember；没有就算了，不要为了记而记。
写入要求：content 必须一句话、可脱离对话读懂；拿不准是"偏好"还是"一次性要求"时，选 on_demand 而不是 always。
用户说"忘掉"时调 memory_forget，不要留半条。
```

这段文本**永久不变**（压缩碰不到它），是模型感知"我有记忆"的唯一常驻提示。

---

## 7. 去重、记账与插入位置

### 7.1 同会话去重（防历史滚雪球）

| 层 | 去重方式 |
| --- | --- |
| S2 | **靠 DSH 自己的机制**：section 的 `text` 是函数，`assemble()` 每次重新求值；文本不变则 system 节点完全不动（KV 前缀保住），文本变了才重写一次。压缩层只动消息历史，碰不到它（ADR-019） |
| T | 自己维护 `Set<contentHash>`（按 content 的 sha1 前 16 位），**同会话内已注入过的不再注入** |

**T 层的压缩盲区（这是参考实现的真实缺陷，必须修）**：
`dsh-local-memory` 的 `injectedBySession` 只增不减，压缩把注入块吃掉后，同一内容就永远不再出现（用户会看到"它明明知道，就是不提"）。

修法（三层）：

```
1. 监听 session/event 的 compaction/end（无 error）→ 置 reinjectPending = true，并清空已注入集合
   （清空是安全的：压缩后重注入第一次必然要重发全部相关记忆）
2. reinjectPending 时，下一次 pre-step 忽略"已注入"判断，重新完整组装一次
3. 判断"注入块是否还在历史上"：不需要自己算——replacement 事件的 sourceEventSeqs
   是否覆盖我的注入消息由 DSH 提供；V1 简化：compaction/end 后一律重注入一次
```

### 7.2 插入位置

**插在最后一条真人 user 消息之前**（`messages.splice(indexOfLastUser, 0, injected)`）。

- 为什么不在最前面（`dsh-local-memory` 的选择）：插最前会让注入块出现在**整段历史的最开端**，看起来像"很早以前说过的话"，而且如果本轮有多条 user 消息（inbox 排队），位置会错乱。
- 为什么不在最后：模型对"紧邻问题之前"的内容注意力最高；放问题之后就变成"事后补充"。
- **消息必须是新建对象**（`UserMessage` 字段 readonly），`source` 必须带 `kind`（否则 `dsh-agent-instructions` 会 TypeError 炸掉整轮），`source.kind` 用 `plugin:dsh-attune-memory`，并带 `form:'recall'` 与 `memoryIds`。

### 7.3 记账

| 计数 | 时机 | 用途 |
| --- | --- | --- |
| `inject_count += 1` | 每条被注入时 | 观测哪些记忆"一直在场"（配合 P11 发现常驻噪音） |
| `use_count += 1` | `memory_search` 命中并返回给模型时 | 观测哪些记忆"真的被用到" |

**都不参与排序**（见 ADR-010）：注入次数高不等于有用（可能只是常驻），用它加权会形成正反馈霸榜。

---

## 8. 与压缩的完整时序（P8 要验证的就是这条链）

```
turn N 开始
  ├─ assemble() → S 常量 + C 文本（读库）→ 若有变化则 project 出新快照
  ├─ agent/pre-step
  │    ├─ 若 reinjectPending：清空已注入集合
  │    ├─ 组 T 内容（检索 + 预算 + 去重）
  │    └─ splice 到 lastUser 之前
  ├─ system/message 写入（S+C 的渲染结果）
  ├─ user/message 写入（含注入块，surfaceOp='append'）
  └─ LLM 请求 → 回答
     ↓
（若干轮后，compaction-basic 在某个 pre-step 里触发压缩）
  ├─ compaction/start → summary → 一条 user/message 带 surfaceOp:{op:'replace'}
  │    → 注入块与旧消息被遮蔽
  ├─ compaction/end
  └─ 我的监听器：reinjectPending = true；已注入集合清空
     ↓
turn M 的 pre-step
  ├─ S2：仍在 system prompt 里，压缩碰不到（v1.1 前这里是 context，靠 retained=null 重发）
  │    → 下一个 project() 自动重发常驻事实（官方机制，无需我写代码）
  └─ T：reinjectPending → 重新组装 → 重新 splice
```

两层恢复互相独立：**C 挂了还有 T，T 挂了还有 C**。这是刻意的冗余——只有一层的方案，任何一个 bug 都会让"压缩后失忆"变成必现问题。

---

## 9. 自检清单（P4 完成判据）

| 判据 | 位置 |
| --- | --- |
| Router 流程完整，四类上下文都有取法与理由 | §2、§3 |
| always 准入四条件 + 正例（称呼/语言偏好）+ 反例（"用户是本专业"写文案无用） | §3.1 |
| contextual 按 domain/scope 匹配 | §3.3 |
| on_demand 仅明确相关（工具调用 / 用户明确问及过去） | §3.4 |
| never 保存但不注入 | §3.5 |
| Context Budget 有具体条数与 token 真值 + 裁剪优先级 | §5 |
| 跑通"用户问数学题"不注入无关项的示例（逐条说明被哪条规则挡住） | §4 |
| 压缩后重注入的时序完整 | §8 |
