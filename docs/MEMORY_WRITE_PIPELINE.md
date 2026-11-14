# MEMORY_WRITE_PIPELINE — 记忆写入管线（P3）

> 上游：`MEMORY_SCHEMA_V1.md`（字段与不变式）、`CURRENT_ENVIRONMENT_ANALYSIS.md` §4（写点选型）、`ADOPTION_DECISION_TABLE.md`（B1/B3/B5/B6/D1）。
> 本文只定义**一条新信息如何变成一条（或零条）记忆**。检索打分见 P5，注入见 P4，状态机全貌见 P6。

---

## 0. 一句话原则

**写入管线的目标不是"存下来"，而是"让 User Model 更准，且不引入需要以后收拾的垃圾"。**

由此推出三条贯穿全文的取舍：

1. **宁可漏记，不可记错**。漏了一条，用户再说一次就好；记错一条，会一直污染回答（而且用户很难发现是记忆的错）。
2. **不确定就降级，不要升级**。置信度低 → `on_demand` 而非 `contextual`；推断而非明说 → 不覆盖任何已有显式记忆。
3. **不做不可逆的动作**。V1 没有自动合并、没有硬删除、没有"引擎自己觉得该覆盖"。

---

## 1. 两个入口，一条管线

```
                       ┌──────────────────────────────┐
 入口 A（主）：模型工具  │ memory_remember / _update /   │
                       │ _forget（模型显式决定写什么）  │
                       └───────────────┬──────────────┘
                                       │
                       ┌───────────────▼──────────────┐
 入口 B（辅）：回合抽取  │ turn/end 后，对**本回合真人  │
                       │ 消息**跑确定性句式抽取        │
                       └───────────────┬──────────────┘
                                       │  产出 Candidate（还不是记忆）
                                       ▼
   ┌────────────────────────────────────────────────────────────────┐
   │ ① Extractor      把"话"变成候选事实（不判长期价值）             │
   │ ② Normalize      content 可独立读懂 / 提议槽位 / 关键词         │
   │ ③ Subject ID     这条事实属于谁（防坍缩）                       │
   │ ④ Value Gate     五问：值不值得长期记                           │
   │ ⑤ Explicit?      用户明说 vs 我推断（决定可信度与上限）         │
   │ ⑥ Find Similar   在库里找同槽位/近邻条目                        │
   │ ⑦ Decide         IGNORE / ADD / UPDATE / MERGE / SUPERSEDE /    │
   │                  ARCHIVE / DELETE（唯一决策点）                 │
   │ ⑧ Commit         一个事务里写 memories + memory_change           │
   └────────────────────────────────────────────────────────────────┘
                                       │
                                       ▼
                         User Model（查询视图，不落副本）
```

**为什么入口 A 是主通道**：写什么、是不是长期价值，本质上是语义判断。模型在对话现场看到完整上下文，比任何正则都准；引擎的职责是**校验与规范化**，不是猜。参考实现 `dsh-local-memory` 也把写入完全交给工具（`memory_save`），meow-memory 同理（`memory_remember`）。

**为什么还要入口 B**：用户常常不会说"记住这件事"，而是顺口说"我最近在备考"。全靠模型自觉会漏。入口 B 只处理**句式高度确定**的一小撮（见 §1.2），命中率低但误报也低。

### 1.1 入口 A：工具契约（P7/P8 实现）

| 工具 | 必填 | 语义 | 引擎行为 |
| --- | --- | --- | --- |
| `memory_remember` | `content`, `type`, `subtype` | "这条值得记" | 走完整管线，引擎可降级为 ADD 或被 IGNORE 掉（并回告原因） |
| `memory_update` | `id`, `content` | "这条要改" | 校验 id 存在、槽位未变；写 UPDATE + 时间轴 |
| `memory_supersede` | `old_id`, `content` | "这条不再成立，换成新的" | 旧行置 superseded + 新行 ADD，同一事务 |
| `memory_forget` | `id`, `reason` | "别再用这条" | 软删（`status='deleted'`），保留行与历史 |
| `memory_find_similar` | `content` | "先查重" | 只读，返回候选 + 冲突提示；模型在写之前应先调它 |

工具返回值必须包含：`decision`（实际执行了哪个动作）、`id`、`reason`（人可读的一句）、`conflicts?[]`。**模型必须能看到"你以为写进去了，其实被忽略/降级了"**，否则它会以为记忆生效而反复重写。

### 1.2 入口 B：确定性句式抽取（只抽这些）

| 句式（正则要点） | 产出 | 例 |
| --- | --- | --- |
| `我(的)?(本科|专业|专业是|学的|毕业于)` | `profile/identity` | 我本科是能源与动力工程 |
| `我(在|正在)(备考|准备|复习)` / `我要考` | `state/goal` | 我最近在备考考研 |
| `我(不)?(喜欢|讨厌|受不了|偏好)` | `profile/preference` | 我受不了满屏 emoji |
| `(以后|下次|今后|之后)(都|一律|默认)` + 动词 | `profile/interaction` | 以后讲东西先讲思路 |
| `(别|不要|不用)(再)?` + 动词短语 | `profile/interaction`（负向） | 别用"首先其次最后"那套 |
| `记住` / `记一下` / `帮我记` | 按内容判 type | 记住我考研考数一 |
| `(不|取消|放弃|不打算)(考|准备|做)了` | `state` 的结束信号 | 我不考研了 |
| `我(朋友|同事|妈|爸|老板|对象)` + 属性 | `person` + 该属性 | 我朋友小王喜欢红色 |

**硬性限制**：
- 只扫**本回合的真人 `user/message`**（`source.kind === 'user'`），不扫 assistant、不扫注入块、不扫 subagent。
- 一条消息最多抽 3 条，超过就只保留 confidence 最高的 3 条（防一句话炸出十条）。
- 抽不出确定句式就**什么都不做**——不要"尽力而为地理解"。
- 抽取出的候选一律 `metadata.extractor='turn_extractor'`、`confidence ≤ 0.9`，且**不允许** `injection_policy='always'`（always 层只接受显式工具写入或用户 UI 确认，见 §6）。

---

## 2. 阶段 ①–③：从话到候选

### 2.1 Normalize：`content` 的三条硬要求

| 要求 | 反例 | 正例 |
| --- | --- | --- |
| **可独立读懂**（脱离对话也成立） | "他喜欢这个" | "用户的朋友小王喜欢红色" |
| **一句话一个事实** | "用户是能动专业的，考研考数一，喜欢先讲思路" | 拆成三条 |
| **陈述态、无时态歧义** | "用户可能要考研" | "用户在备考考研（2026-12 初试）" |

引擎要**机械可检**的只有第 1、2 条：`content` 里出现"他/她/它/这个/那个/上面/刚才"等指代词 → 拒收并回告模型补全；`content` 长度 > 200 字或含 ≥3 个逗号分句 → 要求拆分。语义质量只能靠提示词约束 + P10 评估兜底。

### 2.2 槽位（slot）——后面所有决策的坐标系

```
slot = (subject_type, subject_name, type, subtype, domain_key)
```

槽位不是唯一键（一个槽位可以有多条并存的记忆，比如"喜欢红色"和"喜欢蓝色"都属 `user/profile/preference`）。它是**"该跟谁比"的范围**：冲突判定、查重、合并候选都只在同槽位内进行。

这条限制很重要：跨槽位比对（比如拿 `domain` 的约定去比 `profile` 的偏好）产生的"冲突"几乎都是噪声，参考实现 `dsh-local-memory` 的 `insight.js` 也只在同质条目间比对。

### 2.3 Subject Identification（照 P2 §3 的顺序执行）

引擎在写入前做一次**机械校验**，防止最常见的一类污染：

```python
# 伪代码，P7 落地
THIRD_PARTY = ["我朋友", "我同事", "我妈", "我爸", "我老板", "我对象", "我室友", "我们组"]
if candidate.subject_type == "user" and any(p in candidate.content for p in THIRD_PARTY):
    reject("content 提到第三方主体，subject_type 不能是 user")   # 除非显式声明 subject_type
if candidate.subject_type != "user" and policy == "always":
    reject("非用户主体的记忆不允许 always 注入")                  # P2 §3 硬规则
```

`"我朋友喜欢红色"` → 必须落成 `subject_type='person', subject_name='朋友-小王'`；拿不到名字就 `subject_name='朋友'` 并在 `metadata` 标 `ambiguous_subject: true`，`injection_policy` 强制降为 `on_demand`。

---

## 3. 阶段 ④：长期价值判断（Value Gate）

**五问，任一答"否"即 IGNORE。** 这是管线里唯一会"拒绝写入"的地方，也是评估用例 A 的战场。

| # | 问题 | 否 → 典型例子 |
| --- | --- | --- |
| V1 | **明天还成立吗？**（durability） | "今天天气不错"、"我现在有点困" |
| V2 | **换个话题还有用吗？**（transferability） | "这个函数第三行有 bug"（只对当前文件有用） |
| V3 | **会被复用吗？**（reusability） | "帮我把这行改成 async"（一次性指令） |
| V4 | **能从代码/文件/环境里直接读出来吗？**（derivability） | "项目用的是 TypeScript"（`package.json` 里有）、"这个文件有 300 行" |
| V5 | **是关于人/领域/状态/经历的吗？**（scope） | "用户这条消息有 42 个字" |

**判定表**（引擎实现时直接照抄，避免"看心情"）：

| 输入 | V1 | V2 | V3 | V4 | V5 | 结论 |
| --- | --- | --- | --- | --- | --- | --- |
| 今天天气不错 | 否 | 否 | 否 | — | 否 | **IGNORE** |
| 我朋友喜欢红色 | 是 | 是 | 是 | 否 | 是(person) | **ADD** |
| 帮我改这段代码 | 否 | 否 | 否 | — | — | **IGNORE** |
| 我本科能动专业 | 是 | 是 | 是 | 否 | 是 | **ADD** |
| 这个项目用 pnpm | 是 | 是 | 是 | **是**（`package.json`） | 是 | **IGNORE**（可派生）→ 但若"为什么用 pnpm 而不是 npm"则**ADD**（理由不可派生） |
| 我在备考数一 | 是（到 12 月） | 是 | 是 | 否 | 是(state) | **ADD** |
| 用户问了数学题 | — | 否 | 否 | 是 | 否 | **IGNORE** |

**V4（可派生性）值得单独强调**：这是记忆系统最容易犯的"贪心"错误——把代码库里能查到的东西再存一遍，结果版本一升就全是过期信息。判据是"**从工作区能查到吗**"，而不是"重要吗"。但"**为什么这样选**"永远不可派生，必须记。

**IGNORE 也要留痕**（不落 `memories`，写日志）：`{candidate, reason, at}`。用途：P10 评估要证明"没乱存"，P11 调优要看被误杀的是什么。日志是 `logs/ignore.jsonl`，不进数据库（它不属于记忆）。

---

## 4. 阶段 ⑤：Explicit vs Inferred

| | Explicit（用户明说） | Inferred（我推断） |
| --- | --- | --- |
| 例 | "我考研考数一" | 用户连续三次要求"更短" → "用户偏好简短回答" |
| confidence | 0.9–1.0 | 0.5–0.7（**上限硬编码 0.7**） |
| `metadata.extractor` | `model_tool` / `turn_extractor` / `user_ui` | `inference` |
| 能否 `injection_policy='always'` | 可以 | **不可以**（V1 硬规则） |
| 能否覆盖(explicit)已有记忆 | 可以（SUPERSEDE/UPDATE） | **不可以**，只能 ADD 并列，且必须标 `metadata.conflicts_with` |
| 能否 `contextual` | 可以 | 可以，但需 domain 精确匹配 |

**为什么推断不许覆盖显式**：推断的错法是系统性的（模型把"这次要短"当成"永远要短"），一旦覆盖了用户亲口说的偏好，用户会觉得"它怎么反着来"。并列存放 + 显式标注冲突，用户一句"对，就按短的来"就能收敛。

**V1 的 inferred 只从一个地方来**：`memory_remember` 工具里模型自己标 `explicit: false`。**引擎不做自动推断**（不从行为统计里"学习"偏好——那是自动人格推断，红线禁止）。

---

## 5. 阶段 ⑥–⑦：查重、冲突与决策

### 5.1 Find Similar 的三类候选

| 类型 | 取法 | 用途 |
| --- | --- | --- |
| **同槽位全量** | `WHERE subject_type=? AND subject_name=? AND type=? AND subtype=? AND domain_key<=>? AND status='active'` | 冲突/更新的主战场（量小，几十条内） |
| **关键词近邻** | `keywords`/`content` 与本条候选词有交集，`status='active'`，限 20 条 | 发现"不同槽位但说的是同一件事" |
| **主体最近** | 同 `subject_type/subject_name` 的最近 10 条 | 给模型提供上下文（"关于这个人我已经知道什么"） |

### 5.2 等价 / 冲突 / 互补：三种关系的机械判定

| 关系 | 判据（确定性，不用模型） | 动作 |
| --- | --- | --- |
| **等价（重复）** | 同槽位 且 `normalize(content)` 相同，或关键词集合 Jaccard ≥ 0.8 且同槽位 | IGNORE（并把已有条目的 `updated_at` 刷新、`confidence` 取 max —— 见 §7 的 `TOUCH`） |
| **互斥（冲突）** | 同槽位 且 命中**极性词对**，或 同槽位 + 同"关键值槽"但值不同 | 冲突候选 → 交决策表 |
| **互补（可合并）** | 同槽位 且 同主题但不同侧面（关键词交集 ≥ 0.3 且不互斥），或模型显式要求 merge | MERGE 候选 |

**极性词对（V1 只收这些，照抄 `dsh-local-memory` 的克制做法）**：

```
需要 / 不需要    支持 / 不支持    启用 / 禁用    开启 / 关闭
保留 / 删除      加上 / 去掉      使用 / 不使用  成功 / 失败
可用 / 不可用    推荐 / 不推荐    必须 / 禁止    考 / 不考
```

**关键值槽**（同一属性上的不同取值）：
- 版本号 `\d+\.\d+(\.\d+)?`
- 路径 `[A-Za-z]:\\[^\s]+` 或 `/[\w/.-]+`
- 端口 `端口\s*[:：]?\s*(\d{2,5})`
- 数字+单位 `(\d+(?:\.\d+)?)\s*(个|条|次|天|周|月|年|GB|MB|KB|毫秒|秒|分钟|小时|%|℃)`（单位归一为小写，"30 秒"="30秒"）

两边都有值且**不重合** → 算冲突；只有一边有值 → 不算冲突（是补充）。

**明确不做**：不用"是/不是"做极性对（中文里满屏都是，全是噪声）；不用语义相似度算冲突（V2 才有 embedding）；不跨槽位判冲突。

### 5.3 决策表（唯一决策点）

按顺序求值，**第一条命中即返回**：

| 序 | 条件 | 决策 | 后续 |
| --- | --- | --- | --- |
| 1 | 用户显式要求删除 / 隐私内容 / 事实错误需清除 | **DELETE** | 软删 + 时间轴，`reason` 必填 |
| 2 | `subject_type != 'user'` 且 `policy='always'` | **降级** | 改 `on_demand` 后继续求值（不是拒绝，事实仍有效） |
| 3 | Value Gate 任一为否 | **IGNORE** | 写 ignore 日志，返回原因给调用方 |
| 4 | 同槽位存在**等价**条目 | **TOUCH**（IGNORE 的特例） | 刷 `updated_at`，`confidence=max(old,new)`，写时间轴 `action='touch'` |
| 5 | 调用方显式声明 `supersedes: <id>` | **SUPERSEDE** | 见 §7 |
| 6 | 调用方显式 `id`（UPDATE 语义） | **UPDATE** | 校验槽位不变 |
| 7 | 同槽位存在**互斥**条目 | **SUPERSEDE**（旧的） | 新事实取代旧的；旧的置 `superseded` + `superseded_by`，不删除 |
| 8 | 同槽位存在**互补**条目且总数 ≥ 3 | **MERGE**（提议） | 先返回"建议合并"，**不自动执行**；等显式 `memory_merge` 或用户确认 |
| 9 | 其余 | **ADD** | 正常新增 |

> **第 8 行的落地细节（P7 实现时确定）**：这条仍要**先把新条目 ADD 进去**，再在返回值里附 `merge_suggestion`（`{ids:[...], hint:"..."}`）。原因：提议合并不该同时拒绝写入——否则模型没理会建议的那一轮，新事实就永久丢了。所以顺序是"ADD + 建议"，不是"先卡住等确认"。红线仍然守住：**引用这些 id 的 `memory_merge` 必须显式发起**，引擎不会自己合并。

**优先级说明（为什么这个顺序）**：
- 删除压过一切——用户说"忘掉这个"，不能因为我"觉得重要"就留下。
- 降级（2）在 IGNORE 之前：事实有效，只是不能每轮注入，属于"修正策略"而非"拒绝写入"。
- 值门（3）在查重之前：先判断该不该记，再判断记成什么。反过来的话，一条垃圾内容因为"和现有条目重复"被 TOUCH，等于给垃圾盖章。
- 显式信号（5/6）优先于自动互斥判定（7）：模型说了"这是取代"，就按取代走，避免它和引擎各判一半导致两条并存。
- MERGE（8）只提议不执行：不可逆自动归并是红线。提议的落地形式是工具返回值里的 `merge_suggestion`，外加 P9 管理界面的"合并"按钮。

### 5.4 UPDATE 与 MERGE 的严格区分

这两个是最容易混的，单独列表：

| | **UPDATE** | **MERGE** |
| --- | --- | --- |
| 改的是 | **一条**记忆 | **多条**记忆 → 合成一条 |
| 触发 | 同一条事实被**修正/细化**（真值没变，表述变了） | 同一槽位上**多条碎片**在说同一件事的不同侧面 |
| 例子 | "用户在备考" → "用户在备考，科目是数一/英一/传热学"（还是同一件事，更具体了） | "用户喜欢简洁" + "用户讨厌长段落" + "用户要求少用列表" → "用户偏好极简表达：短句、少列表、无铺垫" |
| 旧条目怎么处理 | **原地改**，`id` 不变，`updated_at` 变；旧内容进 `memory_change.old_snapshot` | 多条置 `status='archived'`（**不是 deleted**），`superseded_by` 指向新的合并条；新条目 `metadata.merged_from=[id...]` |
| 检索影响 | 历史引用不断链（`id` 稳定） | 被合并条目不再命中；可用 `explain` 追回原文 |
| 谁发起 | 模型工具 `memory_update` / 引擎在同槽位判定"细化" | **只能显式发起**（模型 `memory_merge` 或用户 UI），引擎只提议 |
| 反例 | 拿 UPDATE 去改"考研→不考研"（这是真值变了，该 SUPERSEDE） | 拿 MERGE 去合并不相关条目（信息熵上升，不可逆） |

**一句话记法**：UPDATE = **同一条事实换了说法**；MERGE = **几条事实收成一个说法**；SUPERSEDE = **事实本身变了**。

---

## 6. 阶段 ⑧：落库与 User Model "更新"

```python
with db:                       # 单事务；SQLite 下 BEGIN IMMEDIATE
    write_memories(...)        # 1~n 行
    write_memory_change(...)   # 每行至少一条（add/update/supersede/merge/archive/delete/touch）
# 没有第三步：不存在"更新 User Model"的写操作
```

**约束 A 的落实**：管线里**没有任何一步**去更新"用户画像表/缓存/会话级快照"。User Model 永远是 `SELECT … WHERE subject_type='user' AND status='active'` 的实时结果。管线只负责让这张表的状态正确（`status`、`valid_until`、`superseded_by`），不负责"同步"任何派生结构。P8 接入时也不许在插件里缓存记忆内容（V1 明确禁止本地副本缓存，代价是每轮一次查询——毫秒级，可接受）。

### 6.1 WHY 不做人工审核队列

任务书明确禁止"强制人工审核"，这里的理由要能被追问：

1. **审核队列会把"记错"变成"记不进"**。用户说"我在备考"，弹一个"是否确认"——绝大多数人不想管，于是所有记忆都停在 pending，系统等于没有记忆。
2. **真正需要人确认的是"低置信推断"，不是"用户明说"**。对前者，正确做法是降级（`on_demand` + confidence 0.6），而不是排队——降级后它不影响默认上下文，等真的被检索到时模型会看到"这是推断"的标注。
3. **可纠正性比事前审核更重要**。V1 的重心是 P9 的 `list/get/update/forget/explain`：写错了能一眼看到、一句话改掉、能追到是哪句话导致的。事前拦截的效率远低于事后纠错。
4. **拒绝写入已经有出口**：Value Gate 的 IGNORE 日志 + 工具返回值里的 `reason`，模型自己能看到"为什么没记"，必要时它会换个说法重试或直接问用户。

**替代方案（V1 必须有）**：
- 每条记忆都带 `source_message_id` → 用户能问"你从哪句话得出这个的"
- 低置信条目 `injection_policy='on_demand'` → 不污染默认上下文
- `memory_forget(id)` 一句话删掉，且 DELETE 是软删可恢复

---

## 7. 七种写操作对照表

| 操作 | 定义（一句话） | 触发示例 | 改哪些字段 | 保留历史？ |
| --- | --- | --- | --- | --- |
| **IGNORE** | 判定不值得长期记，**不落库** | "今天天气不错" / "帮我把这行改成 async" / "项目用 pnpm"（可从 `package.json` 派生） | 无（只写 `logs/ignore.jsonl`：candidate + reason） | 日志保留，不进 memories |
| **ADD** | 新增一条独立记忆 | "我本科是能源与动力工程" | 新行全字段；`status='active'`；`memory_change(action='add', old_snapshot=null)` | — |
| **UPDATE** | **同一条**记忆被修正/细化，`id` 不变 | "我在备考" → "我在备考数一/英一/传热学，12 月初试" | `content`、`keywords`、`updated_at`、必要时 `importance`/`confidence`；`status` 仍 `active` | 是：`memory_change(old_snapshot, new_snapshot, action='update', reason)` |
| **MERGE** | **多条**同槽位碎片合成一条 | "喜欢简洁" + "讨厌长段落" + "少用列表" → 一条"偏好极简表达" | 新建一条（`metadata.merged_from=[...]`）；被合并条 `status='archived'`、`superseded_by=新id`；`valid_until=now` | 是：被合并条目整行仍在，时间轴逐条记 `action='merge'` |
| **SUPERSEDE** | 旧事实**不再成立**，被新事实取代（两条都留下） | "考研" → "不考研了"；"用 Python" → "全迁到 TypeScript" | 旧：`status='superseded'`、`valid_until=now`、`superseded_by=新id`；新：ADD 一行 `status='active'`、`supersedes=旧id`（记在 `metadata`） | 是：两条并存，链条完整 |
| **ARCHIVE** | 事实仍成立但**不再需要主动使用**（退场，不删除） | 去年的项目约定、已完成的旧目标（"2025 年考过一次"） | `status='archived'`；`valid_until` 视情况填 | 是：行在，只是不再命中默认检索 |
| **DELETE** | 用户要求忘掉 / 隐私 / 明确错误 | "忘掉我提过的那件事" / "那条记错了" | `status='deleted'`；`memory_change(action='delete', reason)` | **软删**：行仍在，检索不命中；只有显式 `purge()`（P9 二次确认）才物理删除 |

**`TOUCH` 是 IGNORE 的一个特例**（不改内容，只刷新 `updated_at`/`confidence`），单独列出来是因为它会写时间轴，容易被误当成 UPDATE。

**状态与操作的关系一句话**：`ADD/SUPERSEDE(新条)` 产生 `active`；`SUPERSEDE(旧条)/MERGE(被并条)` 产生 `superseded`；`ARCHIVE` 产生 `archived`；`DELETE` 产生 `deleted`；`UPDATE/TOUCH` 不改状态。

---

## 8. 完整走通：从"考研"到"不考研"

这是任务书点名要跑的场景，逐步给出**每一行数据的变化**。

### 8.1 时间线

| # | 用户说 | 管线动作 |
| --- | --- | --- |
| T1 | "我最近在备考考研" | ADD → `M1`：`state/goal`，「用户在备考考研」，`valid_from=T1`，`policy=contextual` |
| T2 | "考数学一、英语一、传热学" | 同槽位（`user/state/goal/study`）检索到 `M1`，判为**互补/细化**，但补的是独立信息面 → **UPDATE M1**：content 变长，`keywords` 增加，`updated_at=T2` |
| T3 | 又问"传热学第三章那个公式" | 检索命中 `M1`（关键词"传热学"），`inject_count+1`；无写入 |
| T4 | "我不考研了，改找工作了" | **SUPERSEDE**：`M1` → `status='superseded'`, `valid_until=T4`, `superseded_by=M2`；同时 **ADD M2**：`state/goal`，「用户在找工作」，`valid_from=T4`, `status='active'`, `metadata.supersedes=M1` |

### 8.2 T4 之后的实际数据

```jsonc
// M1 —— 保留，但退出使用
{
  "id": "m-aaa1", "type": "state", "subtype": "goal",
  "subject_type": "user", "subject_name": "用户", "domain_key": "study",
  "content": "用户在备考研究生入学考试，科目为数学一、英语一、传热学。",
  "status": "superseded",
  "valid_from": "2026-09-01T00:00:00Z",
  "valid_until": "2026-11-14T02:10:00Z",     // ← 变更时刻
  "superseded_by": "m-bbb2",
  "updated_at": "2026-11-14T02:10:00Z",
  "metadata": "{\"reason\":\"用户明确表示不再考研\"}"
}
// M2 —— 新的当前态
{
  "id": "m-bbb2", "type": "state", "subtype": "goal",
  "subject_type": "user", "subject_name": "用户", "domain_key": "career",
  "content": "用户已放弃考研，正在找工作。",
  "status": "active",
  "valid_from": "2026-11-14T02:10:00Z", "valid_until": null, "superseded_by": null,
  "metadata": "{\"supersedes\":\"m-aaa1\",\"reason\":\"用户明确改变目标\"}"
}
```

`memory_change` 同时写入三条：`M1: supersede`、`M2: add`、以及一条 `M1→M2` 的 `reason` 记录。

### 8.3 四个必须成立的性质（P10 用例 C 的断言）

1. **M1 不是 `deleted`，也不是 `active`** —— 是 `superseded`。删了就丢失"他曾经要考研"这一事实（那件事影响过很多回答）。
2. **不是并列新增两条 active** —— 若 M1 还是 active，检索会同时命中"在考研"和"在找工作"，模型会给出自相矛盾的建议。这是"冲突是 feature"里唯一必须消灭的形态：**同一槽位的互斥现状只能有一个 active**。
3. **`valid_until` 必须有值** —— 否则 M1 在时间维度上"至今有效"，任何按时间过滤的查询都会把它当现状。
4. **`superseded_by` 指向的新条目存在且 active** —— 链条不能断（P2 §7 不变式 2）。

### 8.4 边界情况（容易做错的地方）

| 情况 | 正确处理 | 错误做法 |
| --- | --- | --- |
| 用户只是抱怨"考研好累" | **不写**（V1 不成立：抱怨不改事实） | 把 goal 改掉 |
| 用户说"考完研了"（自然结束，非否定） | `ARCHIVE` M1（`valid_until=now`，`status='archived'`），不新增 | 标 superseded 却无接替者（违反不变式 1） |
| 用户说"我考研，但也在投简历" | **两条 active 并存**，但 `domain_key` 不同（`study` / `career`），检索时按当前问题域选 | 强行合成一条 |
| 用户改主意改回来（"还是考吧"） | **ADD M3**，`supersedes=M2`；不要让 M1 复活（`status` 单向，回头路走新增） | 把 M1 改回 active（历史就乱了） |

---

## 9. 失败与并发（P7 必须实现）

| 场景 | 处理 |
| --- | --- |
| 写库失败（锁、磁盘） | **fail-open**：不抛给对话层，记日志，工具返回 `ok:false, reason`（模型看到后可以稍后重试）；绝不让记忆写失败导致本轮回答失败 |
| 两个写入并发同槽位 | `BEGIN IMMEDIATE` 串行化；决策在事务内重新读一次同槽位（避免用陈旧快照做 SUPERSEDE） |
| 工具参数非法（缺 type/subtype、type 与 subtype 不配） | 拒收并回告**合法取值清单**，让模型一次改对（不要只说"参数错误"） |
| 重复调用同一工具（模型重试） | 幂等：同槽位 + 等价 content → TOUCH，不产生第二条 |
| `id` 不存在 / 已 deleted | 拒收并提示"该记忆已不存在，请用 ADD" |

---

## 10. 自检清单（P3 完成判据）

| 判据 | 本文位置 |
| --- | --- |
| 八阶段流程图完整 | §1 |
| 七种操作各有定义/触发示例/改哪些字段/是否保留历史 | §7 |
| UPDATE 与 MERGE 严格区分（含反例与一句话记法） | §5.4 |
| "考研→不考研"完整走通（含数据与四条断言） | §8 |
| 无强制人工审核队列，且给出理由与替代方案 | §6.1 |
| 每个拒绝写入都有可复现的判据（五问表 + 判定表） | §3 |
| 与 P2 的字段/不变式一致 | §5.2、§8.2 |
| 与约束 A/B/C/D 无冲突 | §6（A）、§5.3(2)（B）、§7 SUPERSEDE（C）、§5.2 槽位含 scope 语义（D） |

---

## 11. v1.2 的两处调整

**11.1 闸门看句式，不看词表命中**

旧实现"出现问词就拒"会误伤陈述句（"为什么膝盖一冷就疼"是身体事实）。v1.2 的规则：

| 形态 | 判定 | 显式候选（模型 remember） |
|---|---|---|
| 整句以 `?`/`？` 结尾 | 拒 | **仍然拒**（结构问题） |
| 句首是疑问词（怎么/为什么/如何…） | 拒 | 放行（词表容易误伤） |
| 句首是祈使词（帮我/请把/麻烦…） | 拒 | **仍然拒**（"帮我改代码"不是事实） |
| 句首是指代词（他/这个/刚才…） | 拒 | 放行 |
| 命中时效词表（今天天气/刚吃完…） | 拒 | **仍然拒** |

**11.2 候选与落库拆开**

`memory_preview`（引擎 `preview()`）跑完归一化 + 校验 + 闸门 + 查重/冲突，但**不写库、不计数、不写丢弃日志**，
返回 `would = add | touch | supersede | ignore` 与相关条目。落库仍走 `memory_remember`。
两边共用同一套判定，测试断言同输入下 preview 的结论与 add 的 action 一致。
