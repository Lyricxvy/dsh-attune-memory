# V1.1 调优记录：真实使用中暴露的缺陷与已实施的修复

> V1 的判据不是"代码写完了"，而是"真跑起来不添乱"。本文只记**真实跑出来的**缺陷：
> 每一条都有现场证据（会话日志 / 库里的数据 / 报错原文）、根因、已实施的修复、修完的验证。
> 没有证据的"我觉得可能有问题"不进这张表。

一共 10 条。按发现顺序排列，`ADR` 列指向决策记录（为什么这样修，而不是别的修法）。

| # | 缺陷 | 现场 | 修复落点 | ADR |
| --- | --- | --- | --- | --- |
| 1 | 同一条自述被写两遍（模型工具 + 兜底抽取） | 4 条库里有一半是重复 | `store.changes_since` / `manager.recent` / `rpc.recent` / `lib/index.js` 的 `turn/end` | ADR-014 |
| 2 | `valid_until` 写不进去，且格式不校验 | `add 不接受这些参数：['valid_until']` | `rpc._SPEC`、`write.validate()` | ADR-017 |
| 3 | 过时记忆不是降权，而是**消失** | 命中闸门卡六因子乘积 | `search()` 闸门改卡命中质量 | ADR-018 |
| 4 | `contextual` 的 profile 记忆永不注入 | 检索有 0.437 分，注入块里没有 | `read.context_block` 补 profile 分桶 | ADR-016 |
| 5 | 领域交互约定在词法检索里隐形 | 问"极限题"，约定写"讲数学题" | `read._domain_rules()` | ADR-016 |
| 6 | 压缩后强制重建提前 return，稳定块也不补 | 压缩那轮之后注入块空 | `lib/recall.js` 的 `inject()` | ADR-007 |
| 7 | 非法枚举把 SQLite 的 CHECK 报错抛给用户 | `CHECK constraint failed: importance IN (...)` | `write.update()` 前置校验 | — |
| 8 | Windows 下 RPC 中文请求整条链路崩 | `UnicodeEncodeError: '\udca6'` | `rpc.main()` 的 `reconfigure(encoding="utf-8")` | ADR-008 |
| 9 | 时效噪声词表太窄 | "刚吃完午饭，有点困"被收成记忆 | `const.TRANSIENT_PATTERNS` | — |
| 10 | 备考被写成 `profile/identity` 而不是 `state` | 真机会话里"不考数学一了"落成 identity 取代 | `read.MANUAL` 加类型选择指引 | — |
| 11 | 装进真实桌面后，压缩由代理层完成，DSH 侧的压缩通知永远不会来 | `billion-context` 把 `compaction-basic.auto` 设为 `false`，`compaction/end` 零命中 | 稳定层从 `context` 搬到 `section(order=155)`；T 通道加 8 轮重发兜底 | ADR-019 |

---

## 1. 同一条自述被写两遍

**现场**：P8 首次端到端跑完，"我叫张三，正在备考数学一，先讲思路再给步骤"这一句在库里变成了
**4 条**——两条是模型自己调 `memory_remember` 写的（conf 0.95、`always`、`domain_key=NULL`），
另外两条是 `turn/end` 兜底抽取补的（conf 0.65、`domain_key=study.math`）。

**根因**：两条写入通道互不知情。`same_slot` 的槽位包含 `domain_key`，而两条通道对领域的判断天然不同，
槽位一对不上就去重失败——靠"槽位相同"来防重复是防不住的。

**修复**（3 处）：
- `store.changes_since(since)` + `manager.recent()` + `rpc` 的 `recent` op：把"这一轮谁动过库"变成可查事实。
- `lib/index.js` 的 `turn/start` 记 ISO 时间，`turn/end` 先查 `recent`：
  **只要有 `actor==='user'` 的变更（即模型调过工具），整轮跳过兜底抽取**。
- `lib/extract.js` 不再自己塞 `domain_key`：profile/state 本来就跨域，让兜底通道自己造槽位只会制造重复。

**验证**：删库重跑同一句 → `total: 2`（identity + interaction，均 conf 0.95 / always），`problems: []`。

## 2. `valid_until` 写不进去

**现场**：想让"这份备考计划已作废"立刻过期，`add` 报
`add 不接受这些参数：['valid_until']；可用：[...]`。

**根因**：引擎的 `Candidate` 有 `valid_until` 字段、`check_row` 也认它，但 RPC 的参数白名单漏了；
而且没有任何格式校验——`valid_until` 是拿去跟 `now` 比字符串的，写错一次，这条记忆要么永不过期，
要么立刻消失。

**修复**：`rpc._SPEC` 的 `add` / `supersede` 加 `valid_until`；`write.validate()` 校验它是合法 ISO，
且必须晚于 `valid_from`（`valid_until(...) 必须晚于 valid_from(...)`）。

**验证**：`Ran 47 tests … OK` 里的 `TestValidUntil`（三条：可写入过去区间 / 非 ISO 被拒 / 时间倒置被拒）。

## 3. 过时记忆不是降权，而是消失

**现场**：P7 第一次跑测试，`importance=normal` 的记忆怎么都检索不出来。

**根因**：命中闸门卡的是**六因子乘积**（`Final >= MIN_SCORE`）。`importance=normal` 只有 0.5，
完全相关 + 高置信 + 新鲜的条目上限也只有 ~0.3，门槛 0.30 正好把"正常重要性"的记忆全砍了；
过期 state 更惨（Freshness 触底 0.2、冲突 0.5），分数 ≈0.05，永远出不来——设计里的"过时降权"
在实现里变成了"过时消失"。

**修复**：闸门改卡**命中质量** `Relevance × Confidence × ContextMatch >= 0.15`，
重要性、新鲜度、冲突惩罚只参与排序。注释里写清了为什么：重要性只该调排序，不该决定生死。

**验证**：`Ran 47 tests … OK`；过期 state 出现在结果里并带 `⚠` 说明。

## 4. `contextual` 的 profile 记忆永不注入

**现场**：库里有一条"张三做数学一极限题总卡在变形方向"（`profile/skill`，`contextual`），
`search` 给它 0.437 分，但真实会话的注入块里从来没有它。

**根因**：`read.context_block` 只把命中分进 `state`/`domain`/`episodic` 三桶，
`profile` 那一支只有一行注释"按普通条目处理"，没有代码——bucket 不接，就永远不渲染。

**修复**：新增 `profile_items` 分桶，`take("profile", …)` 排在 state 之后、domain 之前；
`const.BUDGET` 加 `profile_max_items: 2` / `profile_max_chars: 160`
（C 通道装不下的 profile 才落到这里，这类"卡在哪"比领域约定更值钱）。

**验证**：`test_contextual_profile_reaches_context_channel`；真机会话
`seq=8 type=user/message source=dsh-attune-memory/recall` 正文含"总是想不到变形方向"。

## 5. 领域交互约定在词法检索里隐形

**现场**：P10 的 E 场景挂了一格——问"这道极限题怎么下手"，`scope=domain, domain_key=study.math`
的约定"讲数学题时先讲思路再给步骤"没进注入块。

**根因**：两句话没有一个共同词。V1 是词法检索，relevance=0 → 直接被闸门挡掉。
而这恰恰是 Interaction Model 最该起作用的地方：**约定是"在这个领域里怎么帮我"，不是"这句话里有什么词"**。

**修复**：`read._domain_rules()`——领域被明确识别时，按 domain 精确取该域下
`scope=domain` 的 `profile/interaction` 记忆（`DOMAIN_RULE_MIN_CONFIDENCE=0.8`），
作为补位追加，不占直接命中的名额，无领域时不补。**只在读管线里做，不动 `search()` 的语义**：
模型主动搜记忆时，词法命中仍是唯一依据。

**验证**：E 场景 6/6 通过；`TestDomainRules` 两条（有域命中 / 无域不命中）。

## 6. 压缩后强制重建提前 return

**现场**：压缩发生的那一轮之后，注入块是空的——本该出现的稳定块也没了。

**根因**：`inject()` 里 `if (!text) return decision` 在"压缩后但本轮无关键词命中"时直接放弃，
而强制重建的意义正是"不管有没有命中，都要把稳定事实补回去"。

**修复**：改成 `if (!text && !(force && this.#stableText))` 才返回；
`body` 在强制重建且无命中时就是稳定块本身；并把稳定块缓存同步提到判断之前。

**验证**：真实会话 `seq=85 kind=dsh-attune-memory form=reinject afterCompaction=true`，
而那一轮用户问的是"先歇会儿，帮我列三条明天复习的建议"（与记忆无关）——仍然重建了。

## 7. 非法枚举把 SQLite 的 CHECK 报错抛给用户

**现场**：`importance="super-high"` 一路走到 SQLite，用户看到
`CHECK constraint failed: importance IN ('key','important','normal','low')`。

**根因**：`write.update()` 没有前置校验，只有 DB 层 CHECK 兜底。

**修复**：`update()` 先校验 `importance`/`injection_policy`/`scope`，报
`importance 只能是 key/important/normal/low 之一，收到 'super-high'`；`confidence` 限 0–1；
`content` 长度 4–300。既不放裸表结构，也让模型能自己改对。

**验证**：`.verify/p9-routes-test.mjs` 第 6 项。

## 8. Windows 下 RPC 中文整条链路崩

**现场**：`UnicodeEncodeError: 'utf-8' codec can't encode character '\udca6'`，所有记忆读写全挂。

**根因**：Windows 上 `sys.stdin/stdout` 默认跟控制台代码页（cp936）走，而 Node 按 UTF-8 读写管道，
中文请求被解成孤立代理字符。

**修复**：`rpc.main()` 开头对 stdin/stdout 各调一次
`stream.reconfigure(encoding="utf-8", errors="replace")`（带 `AttributeError/ValueError` 兜底）。

**验证**：`engine/tests/rpc_smoke.js`（Node 侧真路径，6 条请求含中文）exit 0。

## 9. 时效噪声词表太窄

**现场**：P10 的 A 场景"刚吃完午饭，有点困"被 ADD 进库。

**根因**：`TRANSIENT_PATTERNS` 里只有"我有点困"，没有"有点困"。

**修复**：补一过性身体/情绪说法（有点困/有点累/有点饿/有点烦/刚吃完/正在吃/待会儿…），
**只收"明显是此刻"的说法**，不收"在忙项目"这种可能是长期状态的说法。

**验证**：A 场景 4/4 通过。

## 10. 备考被写成 `profile/identity` 而不是 `state`

**现场**：V1.1 复验的第一轮（"我叫张三，正在备考考研数学一"），模型写的是
`type=profile, subtype=identity`；第二轮"我改主意了，不考数学一了，改考计算机 408"，
模型正确地做了 supersede，但整条链路落在 identity 槽位上。

**根因**：手册只说了"写什么"，没说"放哪一类"。`state` 是**有起止的计划/活动**，
`profile` 是**他是谁/他喜欢什么/他要你怎么回答**；"正在备考"明显是前者，
但模型手里没有这条判据，于是挑了更眼熟的 `profile`。

**修复**：`read.MANUAL` 补一段类型选择指引（"有起止的计划/活动用 `state`"……
"事实变了用 `memory_supersede` 换掉旧条目，不要新加一条并列的"）。

**验证**：见下面的真机复验——取代链完整、交互偏好未被误改、`problems: []`。

## 11. 装进真实桌面后，压缩换人做了（稳定层搬进 system prompt）

**现场**：按 Pro 意见把插件装进 desktop profile 后，我核对这台机器到底谁在压缩，结论是"不是我原来假设的那个"。
`profiles\desktop\node_modules\billion-context\dsh.bundle.patch.yml` 里有两行关键配置：

```yaml
- id: compaction-basic
  config: { auto: false }        # DSH 自带自动压缩关掉
insert:
  - { id: bili-native, name: billion-context }
  - { id: bili-compaction-gate, name: billion-context/dsh-compaction-gate }
```

`bili-compaction-gate` 还会在 preset 层把 `@deepseek-ai/dsh-compaction-basic` 与
`@deepseek-ai/dsh-command-compact` 置 `disabled: true`。也就是说：**DSH 侧不会自动压缩，也不会发 `compaction/end`**；
真正折历史的是 `billion-context`——它是模型 API 层的代理（"把 base URL 指向 proxy"），在服务端重写请求体，
由模型自己调 `compress` 工具触发。字符串计数也印证了：`dist/index.js` 里 `surfaceOp`、`agent/pre-step`、
`compaction/start|summary|end`、`session/event` 命中数**全为 0**，`dsh-native.js` 里唯一的 `session/event`
只处理 `session/title`。

**后果**：我原来依赖的两条恢复路径——`compaction/end` 置 pending、`RuntimeContextProjection` 的
`retained=null` 自动重发——在这台机器上**都不会触发**。稳定事实（`always` 层）放在 `systemPrompt.context`
里，会变成历史中的一条快照消息；被代理折掉之后永久消失，而且没有任何报错。这是"静默失效"，正撞哲学②。

**修复（ADR-019）**：
1. 稳定层从 `ctx.systemPrompt.context({order:200})` 改为 `ctx.systemPrompt.section({order:155})`。
   section 在 system prompt 里，DSH 每个请求重渲染；代理只重写 messages 数组，不碰 system 字段；
   DSH 自己的 compaction 也从"第一个非 `system/message` 节点"开始。三层压缩都碰不到它。
2. T 通道的去重从"每会话每内容一次"改为"记轮次，`STALE_TURNS=8` 轮后允许重发"——
   对任何"听不见的压缩层"都成立：被折掉最多 8 轮后会重新出现，而不是永远消失。

**验证**：新建 `.verify/live-s2.db` 种一条 `always` 记忆（"用户叫李四，正在备考 2027 年考研"），
headless 跑一轮"你还记得我叫什么名字？只回答名字"。会话日志里：

```text
seq=7  system/message  role=system  chars=3499
       章节顺序：## 长期记忆怎么用 / [长期记忆 · 稳定事实]
       [长期记忆 · 稳定事实]
       - 身份：用户叫李四，正在备考 2027 年考研。
seq=8  user/message    source=user            ← 真人问题
seq=9  user/message    source=runtime-context ← DSH 自己的快照
（没有任何 source=dsh-attune-memory 的 user 消息）
模型回答：李四
```

**稳定块只从 system prompt 进来，历史里没有它的副本**——这正是"压缩层想折也折不到"的形态。

## 12. 真机复验（V1.1 收尾）

两轮真实会话（新库 `.verify/live-p11.db`、真模型 `deepseek-flash`）：

1. "我叫张三，正在备考考研数学一，习惯先讲思路再给步骤。记一下。"
   → 回答确认两条："你在备考考研数学一""讲题时我会先说清思路…再列具体步骤"。
2. "我改主意了，不考数学一了，改考计算机 408。帮我记一下。"
   → 回答"原来「数学一」那条已被取代，旧记录会保留在记忆历史里""「讲题先讲思路」这条偏好保留了"。

库里最终三行，与回答一致：

```text
m-mwf3esuh-1p  profile identity    active      key        always   用户叫张三，正在备考考研计算机 408（…），不再考数学一。
m-mwf3eljd-15  profile identity    superseded  key        always   用户叫张三，正在备考考研数学一。
m-mwf3elje-28  profile preference  active      important  always   张三学习/讲数学题时习惯先讲思路、再给具体步骤。
```

`stats.problems: []`——**取代链完整**：旧条留档并指向新条，新条 active，无关的交互偏好没被误改。
这条链路正是哲学 ④ 要的东西：记忆不是"越记越多"，而是"越用越准"。

---

## 这轮调优**没有**做的事（以及为什么）

- **没有加自动遗忘**。样例只有几十条，"该忘哪些"缺乏证据；误删不可逆，V2 再说。
- **没有上 Embedding**。第 5 条的修法是"按领域补位"，比换检索范式便宜得多；语义检索留给 V2。
- **没有把下限抬到 `important`**。那等于承认"normal 的记忆不配被检索到"，与哲学 ② 相悖。
- **没有做人工审核队列**。写入管线已经有 IGNORE/降级/supersede 三档，再加一道人工确认
  只会让用户放弃记录。

这些点到为止的判断都写进了 `V2_V3_RESERVED.md`，V2 的第一件事是把它们重新评估一遍。
