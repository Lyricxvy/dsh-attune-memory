# MEMORY_SCHEMA_V1 — 记忆数据模型（P2）

> 上游：`MEMORY_SYSTEM_DESIGN_PRINCIPLES.md`（P0）、`ADR.md`（P0.5）、`CURRENT_ENVIRONMENT_ANALYSIS.md` / `ADOPTION_DECISION_TABLE.md`（P1）。
> 本文只定义**数据模型**，不写检索算法（P5）、不写写入决策（P3）、不写注入策略细节（P4）。
> 唯一实现约束：P7 的 Engine 必须能仅凭本文建出表、写出数据、回答 `explain`。

---

## 0. 三条设计红线（先立规矩，再谈字段）

| 约束 | 在本 schema 里的落法 | 自检方式 |
| --- | --- | --- |
| **A. User Model 是逻辑视图，不建副本** | 只有一张主表 `memories` 存事实；User Model = `SELECT … WHERE subject_type='user' AND status='active'` 的**查询结果**；没有任何 `user_profile` 表、没有缓存列、没有双写 | 删掉除 `memories` 外的所有表，User Model 的查询结果不变（只丢历史与元数据） |
| **B. importance 与 injection_policy 独立** | 两个互不推导的列：`importance ∈ {key,important,normal,low}` 管"多重要"，`injection_policy ∈ {always,contextual,on_demand,never}` 管"怎么进入上下文"。**代码里禁止** `if importance=='key' then always` 这类映射 | 能构造出四种组合都存在的数据：key+on_demand（重要但只按需查）、low+always（不重要但必须每轮说，如"用中文回答"） |
| **C. 冲突是 feature** | 主表**只存当前有效态**，旧值走 `superseded_by` + `memory_change` 历史表；任何写入都不许 UPDATE 覆盖掉原文 | 任意一条记忆都能回答"它以前是什么、为什么变、谁改的" |
| **D. Interaction Model 带 scope** | 它是 `type='profile' AND subtype='interaction'`，`scope ∈ {global, domain, task}`，**不单开表** | 同一条"先讲思路再给步骤"可以只有一条 global 记录，不需要为每个领域复制 |

---

## 1. 四种语义角色（type）

原则：**type 回答"这条记忆在扮演什么角色"，不回答"它属于哪个话题"**（话题交给 `domain_key`）。角色互斥且穷尽——一条记忆只能是一种角色。

### 1.1 `profile` — 关于用户（或某个主体）的长期稳定属性

**定义**：跨会话、跨任务都成立，且**不会因为一次对话结束而失效**的属性。是 User Model 的主体。

**判据（三条全中才算）**：① 明天问还是这个答案；② 换个话题仍然成立；③ 不需要"当前"这个限定词。

**subtype**：
- `identity`：身份、专业、职业、语言、所在地
- `preference`：喜好/厌恶（内容层面："喜欢具体例子"）
- `interaction`：**怎么帮这个人**（形式层面：先思路后步骤、给足背景、不要套话）——见 §2
- `skill`：能力水平（"能读 Rust，但没写过 async"）
- `constraint`：硬约束（"只能用内网"、"不接受付费方案"）

**真实例**：
1. `identity` —「用户本科是能源与动力工程专业，方向偏传热学。」
2. `preference` —「用户偏好用具体代码/数据说明结论，不喜欢纯概念性描述。」
3. `constraint` —「用户的工作环境不能联网，方案必须能离线跑。」

**反例（不得记为 profile）**：
- 「用户今天心情不好」→ 会变，是 `episodic` 或根本不记。
- 「用户问了一个 Python 问题」→ 一次行为，不构成属性。
- 「用户朋友喜欢红色」→ 主体不是用户，见 §3。

### 1.2 `domain` — 某个领域/项目内部的持续性事实与约定

**定义**：只在特定领域或项目内成立的、相对稳定的事实、约定、决定、坑。**换个领域就不适用**——这正是它和 profile 的分界。

**subtype**：
- `convention`：约定（命名、目录、流程）
- `fact`：领域事实（技术栈版本、环境限制）
- `decision`：做过的决定 + 理由（"为什么选 A 不选 B"）
- `pitfall`：踩过的坑（"看着像 A 其实是 B"）

**真实例**：
1. `convention` —「这个仓库的测试统一用 `pytest -q`，CI 只跑 `tests/` 目录。」
2. `pitfall` —「Electron 的 asar 里 `read` 能读、`grep`/`ripgrep` 读不到，必须先解包。」

**反例**：
- 「用户喜欢简短回答」→ 跨领域都成立，是 `profile/interaction`。
- 「用户正在备考」→ 有起止时间，是 `state`。

### 1.3 `state` — 有时效性的当前状态

**定义**：**带明确起止**的当前情况。`valid_until` 不是装饰，是它的本质：状态会结束，结束时必须被 `SUPERSEDE`/`ARCHIVE`，不能悄悄留着当事实。

**subtype**：
- `goal`：正在进行的目标（备考、找工作、赶 deadline）
- `activity`：正在做的事（"这周在重构 auth 模块"）
- `situation`：当前处境（"机器只有 8G 内存"、"人在出差"）

**真实例**：
1. `goal` —「用户正在备考研究生入学考试，科目是数学一、英语一、传热学，预计 2026-12 初试。」
2. `situation` —「用户当前这台机器是 Windows，PowerShell 7，没有 WSL。」

**反例**：
- 「用户是工科生」→ 没有终点，是 `profile/identity`。
- 「用户 2024 年考过一次研」→ 已结束的具体事件，是 `episodic`。

**硬规则**：`type='state'` 必须有 `valid_from`；`valid_until` 允许为空（表示"至今有效"），但一旦有了新状态，旧状态必须写 `valid_until` 并置 `status='superseded'`（P3 的 SUPERSEDE）。

### 1.4 `episodic` — 具体发生过的事

**定义**：某个时间点上真实发生的事件、做出的决定、得到的教训。它的价值不在"现在是什么"，而在"当时发生了什么、为什么"。

**subtype**：
- `event`：发生了什么
- `decision`：当时做了什么决定 + 理由
- `lesson`：从一次经历里得出的可复用结论

**真实例**：
1. `event` —「2026-11-13 用户让我实现 DSH 长期记忆功能，要求先研究后编码、不许臆造机制。」
2. `lesson` —「上次给用户直接上代码被退回，他要的是先讲思路再给实现——这次先给方案框架他才认可。」

**反例**：
- 「用户喜欢先讲思路」→ 从事件里提炼出的**稳定属性**，应该（也允许）另记一条 `profile/interaction`；事件本身仍作为 `episodic` 留着当证据（两条互相 `metadata.derived_from` 引用，**不是**副本，因为一条是事实、一条是推断）。

### 1.5 四角色对照速查

| | profile | domain | state | episodic |
| --- | --- | --- | --- | --- |
| 时间性 | 长期稳定 | 长期稳定（限于领域） | **有时效** | 定点、已发生 |
| 作用域 | 跨任务 | 单领域/项目 | 跨任务但短暂 | 单次 |
| 默认 injection_policy | contextual（少数 always） | contextual | contextual | on_demand |
| 默认衰减 | 最慢 | 慢 | 最快 | 中 |
| 典型问句 | "他是什么样的人" | "这个项目怎么定的" | "他现在在干什么" | "上次发生了什么" |

---

## 2. Interaction Model 的落位（约束 D）

**结论：`type='profile'`, `subtype='interaction'`，不单开表。** 理由见 ADR-003：它是"关于这个人的属性"的一个子集，单开表会立刻产生"这条该进 profile 还是 interaction"的歧义，并且在查询 User Model 时要 union 两张表（等于人为制造副本）。

**scope 三级的语义与优先级**：

| scope | 含义 | 例子 |
| --- | --- | --- |
| `global` | 任何任务都适用 | 「先讲思路再给步骤」「不要用'首先/其次/最后'套话」 |
| `domain` | 只在 `domain_key` 匹配时适用 | 「讲数学证明时可以直接给步骤，不用铺垫」 |
| `task` | 只在特定任务上下文（`metadata.task_key`）适用 | 「这次改这份简历时不要改措辞，只调格式」 |

**冲突解决**：同一"槽位"（subtype+subject+domain_key）上，`task > domain > global` 更具体者胜；检索时不是丢弃 global，而是**显式标注**"更具体的偏好覆盖了它"（约束 C：不许悄悄覆盖）。

**四条种子偏好必须落到这里**——见 §9 的 S2/S3/S4。

---

## 3. 主体模型（防坍缩，必做）

**问题**：不加区分的话，"我朋友喜欢红色"会被存成"用户喜欢红色"，然后污染 User Model。

**方案**：两个必填/可空列组合。

| 列 | 约束 | 说明 |
| --- | --- | --- |
| `subject_type` | NOT NULL, ∈ `{user, person, project, org, other}` | 这条事实**属于谁** |
| `subject_name` | 可空；`subject_type='user'` 时固定为 `用户`；其它情况写具体名字/标识 | 消歧用（"朋友"可能有多个 → `朋友-小王`） |

**判定顺序（P3 的 Subject Identification 步骤照此实现）**：
1. 句子里主语是"我/我的" 且修饰的是**用户自身属性** → `user`
2. 主语是第三方人（朋友/同事/我妈）→ `person` + 具体称呼
3. 主语是项目/仓库/产品 → `project` + 名称
4. 主语是公司/组织/团队 → `org` + 名称
5. 说不清 → `other` + 原句主语词

**关键规则**：`subject_type != 'user'` 的记忆，**永远不参与** User Model 查询，也不允许 `injection_policy='always'`（别人的偏好不该每轮注入）。这条写成数据库触发器或应用层断言，二选一，P7 定。

**真实例**：
- 用户说"我朋友小王喜欢红色" → `subject_type=person, subject_name=朋友-小王, type=profile, subtype=preference, content="用户的朋友小王喜欢红色"`
- 用户说"我公司在做储能项目" → `subject_type=org, subject_name=公司, type=profile, subtype=identity`（是"公司属性"，不是用户属性）

---

## 4. 字段表

主表 **`memories`**。

| # | 字段 | 类型 | 约束 | 为什么需要（不是"别人都有"） |
| --- | --- | --- | --- | --- |
| 1 | `id` | TEXT | PK | 稳定标识，格式 `m-<36进制毫秒>-<4位随机>`；历史引用不断链（软删/重建也不复用） |
| 2 | `user_id` | TEXT | NOT NULL, 默认 `default` | V1 单用户也保留：V2 若要多用户/多主体账号，不需要改表 |
| 3 | `type` | TEXT | NOT NULL, CHECK ∈ {profile,domain,state,episodic} | 四种语义角色，决定检索优先级与衰减速度 |
| 4 | `subtype` | TEXT | NOT NULL | type 内部的细分（见 §1）；`(type, subtype)` 的配对在应用层校验，不写死 CHECK（SQLite 的跨列 CHECK 会随 subtype 扩张而变脆） |
| 5 | `subject_type` | TEXT | NOT NULL, CHECK ∈ {user,person,project,org,other} | 防坍缩（§3） |
| 6 | `subject_name` | TEXT | NULL | 主体消歧；`user` 时为 `用户` |
| 7 | `domain_key` | TEXT | NULL | 逻辑分区（`study`、`study.math`、`code.repo_x`、`writing`）。**不是**外键，不建 domain 表——V1 的 domain 就是字符串前缀，允许 `study.math` 被 `study` 前缀匹配 |
| 8 | `content` | TEXT | NOT NULL | **一句可独立读懂的事实陈述**（不带"他/这个"这类依赖上下文的指代）。这条决定检索质量 |
| 9 | `keywords` | TEXT | NOT NULL, 默认 `''` | 空格分隔的检索词。V1 词法匹配的直接输入；由写入方（模型或抽取器）填写，缺省时从 content 抽 |
| 10 | `confidence` | REAL | NOT NULL, CHECK 0..1 | 这条推断有多可信。显式陈述 0.9–1.0，推断 0.5–0.7，猜测不允许入库 |
| 11 | `importance` | TEXT | NOT NULL, CHECK ∈ {key,important,normal,low} | 多重要（枚举 → 固定数值参与排序，见 §5） |
| 12 | `injection_policy` | TEXT | NOT NULL, CHECK ∈ {always,contextual,on_demand,never} | 怎么进上下文（与 #11 **完全独立**） |
| 13 | `scope` | TEXT | NOT NULL, CHECK ∈ {global,domain,task}, 默认 `global` | 生效范围（约束 D）；非 interaction 类型主要用于检索匹配 |
| 14 | `status` | TEXT | NOT NULL, CHECK ∈ {active,superseded,archived,deleted} | 当前态。`created`/`updated` 是**时刻**（见 §7），不是状态值 |
| 15 | `source_message_id` | TEXT | NULL | 来源消息 id（会话事件 seq 或消息 id）→ 来源追踪、可回溯原话 |
| 16 | `source_conversation_id` | TEXT | NULL | 来源会话 id（DSH `session.id`）→ "这条是哪次聊出来的" |
| 17 | `valid_from` | TEXT | NOT NULL | ISO8601 UTC。state 必填且是语义核心；其它类型填首次成立时间 |
| 18 | `valid_until` | TEXT | NULL | 失效时间。NULL = 至今有效。**superseded 时必须写**（P6） |
| 19 | `superseded_by` | TEXT | NULL, 自引用 | 指向取代它的那条 id → 追溯链；配合 `memory_change` 回答"为什么变" |
| 20 | `created_at` | TEXT | NOT NULL | 写入时间（UTC ISO8601） |
| 21 | `updated_at` | TEXT | NOT NULL | 最后一次变更时间 |
| 22 | `metadata` | TEXT | NOT NULL, 默认 `'{}'` | JSON：`{extractor, reason, tags[], derived_from, task_key, …}`。**只放辅助信息，不放任何会被查询/排序的字段**（否则等于绕过 schema 建隐式列） |
| 23 | `inject_count` | INTEGER | NOT NULL, 默认 0 | 观测：被注入过几次（**不参与排序**，见 ADR-010） |
| 24 | `use_count` | INTEGER | NOT NULL, 默认 0 | 观测：被 `search` 命中过几次（同上） |

**明确不做的列**（V1）：
- 无 `embedding`/`vector`：ADR-002。
- 无 `parent_id`/`relation`：关系图是 V3。
- 无 `pinned`：用 `injection_policy='always'` 表达（ADT A9）。
- 无 `decay_score` 物化列：衰减在查询时算，物化等于存副本且会过期。

### 4.1 辅助表

**`memory_change`** — 变更时间轴（可解释性的物质基础，P6 用）

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `id` | INTEGER PK AUTOINCREMENT | |
| `memory_id` | TEXT NOT NULL | 被改的那条 |
| `action` | TEXT NOT NULL | `add/update/merge/supersede/archive/delete/restore` |
| `old_snapshot` | TEXT NULL | 变更前整行 JSON（add 时为 NULL） |
| `new_snapshot` | TEXT NULL | 变更后整行 JSON |
| `reason` | TEXT NOT NULL | 人可读理由（"用户说考研取消了，取代原目标"） |
| `actor` | TEXT NOT NULL | `model_tool` / `turn_extractor` / `user_ui` |
| `source_message_id` | TEXT NULL | 触发它的那句话 |
| `at` | TEXT NOT NULL | UTC ISO8601 |

**`memory_meta`** — 运行元数据（键值）

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `k` | TEXT PK | `schema_version`、`last_maintenance_at`、`created_at` |
| `v` | TEXT NOT NULL | |

约束 A 自检：`memory_change` 是**历史流水**，`memory_meta` 是**运行元数据**，两者都不是"User Model 的另一种存法"——删掉它们，User Model 查询结果完全不变。

---

## 5. `importance` × `injection_policy`（约束 B 的具体落法）

### 5.1 importance：枚举 → 固定数值（ADR-009）

模型只填枚举，不填数字（`dsh-local-memory` 的经验：让模型打 1–5 分不稳定，同名分数不同含义）。代码里的映射**写死并出文档**：

| 枚举 | 数值 | 含义 |
| --- | --- | --- |
| `key` | 1.00 | 定调子的：身份、硬约束、核心偏好 |
| `important` | 0.80 | 明显影响回答质量 |
| `normal` | 0.50 | 一般事实（默认） |
| `low` | 0.25 | 边缘信息，有比没有好 |

### 5.2 injection_policy：四级

| 级别 | 语义 | 准入条件（P4 细化） | 例子 |
| --- | --- | --- | --- |
| `always` | 每轮都进上下文 | Stable ∩ 高置信 ∩ 跨任务有用 ∩ 不污染，**宁少勿多** | 「用中文回答」「先思路后步骤」 |
| `contextual` | 按 `domain_key` / `scope` 匹配当前上下文后进 | 领域/状态匹配上才进 | 「备考科目是数学一」 |
| `on_demand` | 只有模型显式 `memory_search` 才出 | 不进默认上下文 | 「上次那个 bug 的具体报错」 |
| `never` | 存着，但永不自动注入 | 只可 `explain`/管理界面看 | 「用户提过一次很私人的事」 |

### 5.3 为什么必须独立（写进评估用例）

四种组合都要能存在，任何一条被"优化"掉就说明又把两者耦合了：

| 组合 | 真实例子 | 如果耦合会怎样 |
| --- | --- | --- |
| key + always | 「始终用中文回答」 | 没问题，但会诱使人以为所有 key 都该 always |
| key + on_demand | 「用户对花生过敏」——非常重要，但只在聊吃饭/买东西时才需要，平时注入是噪音 | 耦合实现会把它每轮塞进去，污染上下文（评估用例 D 会抓） |
| low + always | 「不要用 emoji」——谈不上"重要"，但违反了很烦 | 耦合实现会漏掉它，因为 importance 低 |
| low + never | 「用户提过喜欢下雨天」——存着，别打扰 | —— |

---

## 6. `scope` 与 `domain_key` 的分工

容易混，先定死：

| | `domain_key` | `scope` |
| --- | --- | --- |
| 回答什么 | **话题/领域**是什么（`study.math`、`code.repo_x`） | **生效范围**有多广（global/domain/task） |
| 可否为空 | 可（跨领域事实） | 不可，默认 `global` |
| 谁用 | 检索时的 domain 匹配 | 冲突时的优先级（task > domain > global） |

组合规则：
- `scope='global'` → `domain_key` 通常为 NULL（"任何话题都适用"）
- `scope='domain'` → `domain_key` **必须**非空（否则这条 scope 等于没写）
- `scope='task'` → `domain_key` 可空，但 `metadata.task_key` 必须非空

这三条写成应用层不变量校验，P7 的 `add()` 要拒收违反者。

---

## 7. 状态与历史（为 P6 留好接口）

**`status` 四值**：`active` / `superseded` / `archived` / `deleted`。

**`created` 与 `updated` 不是状态值**，是两个时刻：
- `created` = 行首次落库的瞬间，由 `created_at` + `memory_change(action='add')` 表达。
- `updated` = 行内容被修正的瞬间，由 `updated_at` + `memory_change(action='update')` 表达。

理由：把"刚创建"和"更新过"做成 status 值，会让每次写入都必须先写一个瞬时状态再改回来（两阶段写），而它们对**检索**没有任何影响——检索只关心"现在还有效吗"。P6 的状态机会画出六个节点，但**持久化的枚举是四个**，这一点必须在 P6 里显式说明，避免和本文冲突。

**不变式**（P7 的测试要覆盖）：
1. `status='superseded'` ⇒ `superseded_by` 非空 且 `valid_until` 非空
2. `superseded_by` 指向的必须是 `status IN ('active','archived')` 的行（不许指向另一个 superseded，链条要短）
3. `status='deleted'` ⇒ 不可被检索命中，但行仍在（`purge` 才真删）
4. `type='state'` ⇒ `valid_from` 非空
5. `subject_type != 'user'` ⇒ `injection_policy != 'always'`

---

## 8. 索引（按检索路径设计，不是按字段美观）

检索路径来自 P4/P5：① 取 always 集合；② 按 domain 取 contextual；③ 按关键词/主体/状态查；④ 追溯链；⑤ 过期扫描。

| 索引名 | 列 | 服务的查询 |
| --- | --- | --- |
| `idx_mem_policy` | `(user_id, injection_policy, status)` | 每轮取 `always`（最高频，必须最先命中） |
| `idx_mem_type_domain` | `(user_id, type, domain_key, status)` | 按角色 + 领域取 contextual 候选 |
| `idx_mem_scope` | `(user_id, scope, status)` | scope 优先级裁决（task/domain/global 三层取） |
| `idx_mem_subject` | `(subject_type, subject_name, status)` | 主体查询（"我朋友喜欢什么"）+ 防坍缩校验 |
| `idx_mem_valid` | `(status, valid_until)` | 过期扫描（`valid_until < now AND status='active'`） |
| `idx_mem_superseded` | `(superseded_by)` | 追溯链向上查 |
| `idx_mem_change_mid` | `memory_change(memory_id, at)` | `explain` 拉历史 |
| `idx_mem_updated` | `(user_id, updated_at)` | 管理界面按时间列表 |

**关于关键词检索的诚实说明**：SQLite 的 `LIKE '%词%'` **用不上索引**，V1 的 `keywords`/`content` 匹配是"结构化前置过滤后的表扫描"。可接受的前提：V1 单用户、条目量级 < 5000，单次扫 5000 行的开销在毫秒级；真正的大头（`always`、domain 过滤）都走索引。V2 上 FTS5 + bigram 才是正解（任务书已划界）。**不为此在 V1 引入任何额外结构**。

---

## 9. 四条种子偏好：逐条落库

任务书要求这四条必须落到**明确的 schema**，不能悬空。

| 种子 | 原文意图 | type / subtype | subject | domain_key | scope | injection_policy | importance | 说明 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| **S1** 本专业 + 备考科目 | "我本科能动专业，现在备考数学一、英语一、传热学" | 拆两条：`profile/identity` + `state/goal` | user | `study` | global | contextual | key（专业）/ important（备考） | 专业是长期属性；备考有起止，必须是 state。**拆开**是因为它们失效时间不同——考完试，专业还在 |
| **S2** 先讲思路再给步骤 | "讲东西先讲思路，再给步骤" | `profile/interaction` | user | **NULL** | **global** | always | key | 见下方决策 |
| **S3** 给足背景从零讲 | "默认从零开始讲，别假设我知道" | `profile/interaction` | user | NULL | global | always | key | 与 S2 同族，但可独立失效（可能某天说"这个我熟，跳过背景"→ supersede） |
| **S4** 不喜欢某种回答方式 | "别用'首先/其次/最后'那套，也别堆 emoji" | `profile/interaction` | user | NULL | global | always | important | 负向偏好也必须显式存（"不要什么"往往比"要什么"更稳定） |

### S2 的 scope 决策：`global`，不是 `study.math`

任务书要求对此做出选择并说明。**选 `global`**，理由三条：

1. **证据不足以下窄结论**。用户只说了一次"先讲思路再给步骤"，没有说"只在讲数学时"。把它写成 `scope=domain, domain_key=study.math`，等于把一个全局偏好偷偷降级成局部偏好——之后写文案、讲架构时都不生效，用户会觉得"它怎么又忘了"。**宁可宽一点、但写清 confidence**。
2. **窄 scope 的代价不对称**。宽了只是多注入一条（预算可控，一条 20 字）；窄了是"该生效时没生效"，而这正是记忆系统最伤信任的失败模式。
3. **保留了收窄的路径**。如果后续用户说"写代码时不用讲思路，直接给代码"，那不是修改 S2，而是**新增**一条 `scope=domain, domain_key=code`、内容为"写代码时直接给实现"的 interaction 记忆，检索时 `domain` 优先于 `global`，并显式标注覆盖关系（约束 C）。这样两个偏好都在，历史可解释。

**反向条件**（写进文档，便于 P11 复盘）：若评估发现 `always` 层预算被 interaction 挤爆（>30% token），则把 S2/S3 降为 `contextual` + `domain_key=study`，并把这一步记进 ADR。

---

## 10. 完整行示例（P7 的种子数据就用这些）

### 示例 1：S2 —— 讲法偏好（always 层）

```json
{
  "id": "m-lx9k2p-a3f1",
  "user_id": "default",
  "type": "profile",
  "subtype": "interaction",
  "subject_type": "user",
  "subject_name": "用户",
  "domain_key": null,
  "content": "用户希望讲解时先给整体思路和为什么，再给具体步骤；直接抛步骤会被认为没讲清楚。",
  "keywords": "讲思路 讲法 讲解 步骤 偏好",
  "confidence": 0.92,
  "importance": "key",
  "injection_policy": "always",
  "scope": "global",
  "status": "active",
  "source_message_id": "evt-000123",
  "source_conversation_id": "session-<EXAMPLE>",
  "valid_from": "2026-11-13T13:41:00Z",
  "valid_until": null,
  "superseded_by": null,
  "created_at": "2026-11-13T13:41:20Z",
  "updated_at": "2026-11-13T13:41:20Z",
  "metadata": "{\"extractor\":\"model_tool\",\"reason\":\"用户明确陈述偏好\",\"tags\":[\"interaction\",\"explain-style\"]}",
  "inject_count": 0,
  "use_count": 0
}
```

### 示例 2：S1 的备考部分（state，带时效）

```json
{
  "id": "m-lx9k3a-b7c2",
  "user_id": "default",
  "type": "state",
  "subtype": "goal",
  "subject_type": "user",
  "subject_name": "用户",
  "domain_key": "study",
  "content": "用户正在备考研究生入学考试，科目为数学一、英语一、传热学，初试预计在 2026 年 12 月。",
  "keywords": "考研 备考 数学一 英语一 传热学 初试",
  "confidence": 0.95,
  "importance": "important",
  "injection_policy": "contextual",
  "scope": "global",
  "status": "active",
  "source_message_id": "evt-000130",
  "source_conversation_id": "session-<EXAMPLE>",
  "valid_from": "2026-11-13T00:00:00Z",
  "valid_until": null,
  "superseded_by": null,
  "created_at": "2026-11-13T13:42:00Z",
  "updated_at": "2026-11-13T13:42:00Z",
  "metadata": "{\"extractor\":\"model_tool\",\"reason\":\"用户明确说明当前目标\",\"tags\":[\"goal\",\"exam\"]}",
  "inject_count": 0,
  "use_count": 0
}
```

### 示例 3：domain 约定（pitfall，on_demand）

```json
{
  "id": "m-lx9k4d-c9e3",
  "user_id": "default",
  "type": "domain",
  "subtype": "pitfall",
  "subject_type": "project",
  "subject_name": "DeepseekHarness",
  "domain_key": "code.dsh",
  "content": "DSH 的实现在 resources/app.asar 内：read 工具能直接读 asar 路径，但 grep/ripgrep 看不到，必须先解包再检索。",
  "keywords": "asar 解包 grep ripgrep 读取 源码",
  "confidence": 1.0,
  "importance": "important",
  "injection_policy": "on_demand",
  "scope": "domain",
  "status": "active",
  "source_message_id": "evt-000210",
  "source_conversation_id": "session-<EXAMPLE>",
  "valid_from": "2026-11-13T13:50:00Z",
  "valid_until": null,
  "superseded_by": null,
  "created_at": "2026-11-13T13:50:10Z",
  "updated_at": "2026-11-13T13:50:10Z",
  "metadata": "{\"extractor\":\"model_tool\",\"reason\":\"踩过的坑，下次会再遇到\",\"tags\":[\"pitfall\",\"asar\"]}",
  "inject_count": 0,
  "use_count": 0
}
```

### 示例 4：已被取代的 state（展示冲突保留，不是删除）

```json
{
  "id": "m-lx9k5e-d1f4",
  "user_id": "default",
  "type": "state",
  "subtype": "activity",
  "subject_type": "user",
  "subject_name": "用户",
  "domain_key": "study",
  "content": "用户在准备考研。",
  "keywords": "考研 备考 状态",
  "confidence": 0.9,
  "importance": "important",
  "injection_policy": "contextual",
  "scope": "global",
  "status": "superseded",
  "source_message_id": "evt-000120",
  "source_conversation_id": "session-<EXAMPLE>",
  "valid_from": "2026-10-01T00:00:00Z",
  "valid_until": "2026-11-13T00:00:00Z",
  "superseded_by": "m-lx9k3a-b7c2",
  "created_at": "2026-10-01T08:00:00Z",
  "updated_at": "2026-11-13T13:42:00Z",
  "metadata": "{\"extractor\":\"model_tool\",\"reason\":\"被更具体的目标取代（增加了科目与时间）\",\"tags\":[\"goal\"]}",
  "inject_count": 3,
  "use_count": 1
}
```

---

## 11. 约束 A 的自检清单（P7/P8 各跑一次）

| 检查 | 通过标准 |
| --- | --- |
| 是否存在"用户画像"表/缓存/物化列？ | 无。`SELECT * FROM memories WHERE subject_type='user' AND status='active'` 就是 User Model |
| 是否有任何字段是"另一个字段的副本"？ | 无。`content` 与 `keywords` 不是副本（前者给人/模型读，后者给匹配器切） |
| 删掉 `memory_change` / `memory_meta` 后 User Model 是否不变？ | 不变（只丢历史和运行元数据） |
| 是否在插件侧另存了一份用户画像？ | 无。插件只调 Engine，不缓存记忆内容（V1 明确不做本地缓存） |

---

## 12. 明确不做（V1 边界，写清以便 V2 接手）

| 不做 | 归属 | 原因 |
| --- | --- | --- |
| embedding 列 / 向量表 | V2 | ADR-002 |
| FTS5 虚拟表 / bigram 索引列 | V2 | 任务书 V2 范围；V1 用 LIKE + 结构过滤 |
| 记忆关系表（`memory_edge`） | V3 | Memory Graph 是 V3 |
| 类型化半衰期列 | V2 | V1 用代码常量 |
| 冲突检测结果表 | V2 | V1 只在写入决策时做同槽位显式比对（P3） |
| `pinned` / `weight` / `decay_score` | — | 分别被 injection_policy / importance / 查询期计算替代 |

---

## 13. 与下游的接口约定

| 下游 | 依赖本文的哪些部分 |
| --- | --- |
| P3 写管线 | §3 主体判定顺序、§4 字段约束、§5.2 四级语义、§7 不变式、`memory_change` 的 action 词表 |
| P4 读管线 | §1.5 默认 policy、§5.3 四种组合、§6 scope/domain 分工 |
| P5 检索 | §8 索引与检索路径、§5.1 importance 映射、`keywords` 列 |
| P6 生命周期 | §7 四值 + 六节点映射、`memory_change` 表 |
| P7 Engine | §4 全部字段与约束、§7 五条不变式、§8 索引、§10 示例即种子数据 |
