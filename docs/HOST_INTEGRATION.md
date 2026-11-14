# 记忆系统接入 DSH 说明（P8）

本文记录"长期记忆插件"如何接进 DeepSeek Harness：接了哪几处、为什么接在那里、怎么验证、怎么装、出问题怎么退。

结论先给：**三个读通道 + 两个写入口**，全部落在公开 API 上，没有改任何 DSH 源码。

| 方向 | 通道 | 接入点 | 内容 |
|---|---|---|---|
| 读 | S1（手册） | `ctx.systemPrompt.section({name:'memory:manual', order:150})` | "怎么用记忆"的常量说明，压缩永远碰不到 |
| 读 | S2（稳定层） | `ctx.systemPrompt.section({name:'memory:stable', order:155})` | `injection_policy='always'` 的画像事实，直接长在 system prompt 里 |
| 读 | T（本轮相关） | `agent/pre-step` 里 splice 一条消息 | contextual / state / domain / historical 层 |
| 写 | A（主） | 8 个 `memory_*` 工具 | 模型自己判断该记什么 |
| 写 | B（兜底） | `session/event` 的 `turn/end` | 确定性句式抽取，模型没动手时补位 |

> **v1.1 修订（ADR-019）**：S2 原来是 `ctx.systemPrompt.context({order:200})`，靠 `RuntimeContextProjection` 在压缩后重发。实机发现桌面 profile 装了 `billion-context`（ACP 代理层），它在请求体重写历史、不发会话事件，还把 `compaction-basic` 的 `auto` 关掉了 —— 那条自动恢复路径在真实环境里不触发。改成 section 后，稳定层不再依赖任何"压缩通知"。详见 `UNKNOWN_LIST.md` U-003 与 `ADR.md` ADR-019。

## 1. 组成

```
plugin/dsh-attune-memory/          # Cordis 插件（Node，ESM，无第三方依赖）
├── package.json            # name / type:module / main:lib/index.js / dsh.bundle.patch
├── cordis.patch.yml        # 唯一的 insert 行 + engineDir 配置
└── lib/
    ├── index.js            # apply()：注册 section/context、工具、事件钩子
    ├── engine.js           # 引擎子进程客户端（stdio JSON-RPC，串行队列）
    ├── recall.js           # 三个读通道的判定与注入
    ├── extract.js          # 8 条确定性句式规则
    ├── message.js          # 造注入消息（深冻结对象、必带 source.kind）
    └── tools.js            # 8 个 memory_* 工具定义

engine/                     # 记忆引擎（Python 3.11，纯标准库）
├── memory/{const,db,util,text,store,search,write,read,explain,manager,rpc}.py
└── tests/{test_engine.py,rpc_smoke.js}
```

两个进程，一条管道：插件负责"什么时候读/写"，引擎负责"记什么、怎么判、怎么查"。插件崩了不影响回答，引擎崩了下次调用重启。

## 2. 三个读通道

### 2.1 S：用法手册（`systemPrompt.section`）

```js
ctx.systemPrompt.section({ name: 'memory:manual', order: 150, text: () => recall.manual })
```

- `order: 150` 落在 DSH 自己的 section 之间（harness identity −1000、first-party guidance 在其后、harness source 10000）；用 `< 10000` 是刻意的——它属于"行为准则"，不属于"事实清单"。
- 文本来自引擎的 `MANUAL` 常量（`engine/attune_memory/read.py`），**永不变化**：不会引起 prompt 准入（`systemPromptUpdate`），也不会让 KV 前缀失效。
- 为什么放 system 段：压缩区间永远从"第一个不是 `system/message` 的 surface 节点"开始（`dsh-compaction-basic/README.md:123`），所以这段文字**在所有压缩之后都还在**。它是整个系统里唯一不会被压缩影响的记忆相关内容。

### 2.2 S2：稳定层（`systemPrompt.section`）

```js
ctx.systemPrompt.section({ name: 'memory:stable', order: 155, text: () => recall.stableText })
```

- `text` 是**函数**：`dsh-system-prompt/lib/index.js:389` 每次 `assemble()` 重新求值，所以库一变，下一轮 system prompt 就跟着变，不需要任何缓存失效逻辑。
- 位置 `order: 155` —— 紧跟在手册（150）后面，仍在一个 `system/message` 节点里。**这是它能活过所有压缩的原因**：`compaction-basic` 的每个区间都从"第一个不是 `system/message` 的 surface 节点"开始；代理层的 ACP 只重写 messages 数组，也不碰 system 字段。
- 代价：文本一变，system 节点就得重写（KV 前缀从那里失效）。所以这段文本**绝不能含时间词**，`stable_block()` 只输出 `- 类别：内容`。
- 准入：`injection_policy='always'` ∧ `status='active'` ∧ `confidence ≥ 0.85` ∧ 正文 ≤ 80 字 ∧ 最多 6 条 ≤ 500 字。
- 真机证据（`.verify/live-s2.db`，headless 单轮）：`system/message` 里出现 `## 长期记忆怎么用` 与 `[长期记忆 · 稳定事实]\n- 身份：用户叫李四，正在备考 2027 年考研。`，而该会话**没有任何 `source.kind='dsh-attune-memory'` 的 user 消息**——记忆只从 system prompt 进来。模型据此答出"李四"。

> 历史说明（v1.1 之前）：这里原本是 `systemPrompt.context({order:200})`，DSH 把它合并成一条 `source.kind="runtime-context"` 的快照消息，靠 `RuntimeContextProjection`（`dsh-agent-loop/lib/index.js:324` 置 `retained=null`、`:333` 重发）在压缩后自动回来。机制本身是对的（P8 用它验证过 seq=9→34→40 的重发），但它在 `billion-context` 关掉 `compaction-basic.auto`、又在代理层自行压缩的环境里不会被触发，所以换成 section。

### 2.3 T：本轮相关（`agent/pre-step`）

```js
ctx.on('agent/pre-step', async (payload, next) => {
  const decision = await next()
  return await recall.inject(payload, decision)
})
```

注入方式（`lib/recall.js`）：

```js
const note = createMemoryMessage(body, { form: 'recall', memoryIds, domain })
const messages = [...decision.messages]
messages.splice(messages.indexOf(lastHumanMessage), 0, note)
return { ...decision, messages }
```

六条判定顺序，任何一条不过就原样返回：`kind==='enter'` → 非子 agent 会话 → 有真人消息 → 长度 ≥4 且不以 `/` 开头 → 本 turn 未注入过 → 内容未注入过（或处于压缩后重建状态）。

> **v1.1 修订（ADR-019）**：最后一条从"本会话注入过就永不再注入"改成"记轮次，`STALE_TURNS=8` 轮后允许重发"。原因是听不见的压缩层（代理 ACP）可能把注入块折掉而不发任何通知；永久去重会把"被折掉"变成"永久失忆"。8 轮的重发窗口既排除每轮重复，也保证内容最终会回来。

真机证据（`.verify/live-s2.db`，headless 单轮，问题"数学一的极限题我总是想不到变形方向"）：

```text
seq=7  system/message  （手册 + 稳定块）
seq=8  user/message  source=dsh-attune-memory/recall   ← 本次注入
       [长期记忆 · 本次相关] - 技能：李四做数学一极限题时总是想不到变形方向…
seq=9  user/message  source=user                ← 真人问题
seq=10 user/message  source=runtime-context     ← DSH 自己的快照
```

注入块排在真人问题之前、runtime-context 快照之前，位置符合设计。

三条硬约束（都是踩过的坑，写在 `recall.js` 顶部注释里）：

1. **必须带 `source.kind`**：`dsh-agent-instructions/lib/index.js:1280` 会无保护地读 `message.source.kind`，缺了直接 TypeError，整轮在第一个 step 前就失败，界面上只写"本轮运行失败"。
2. **必须造新对象**：`createUserMessage()` 内部 `deepFreeze(structuredClone(...))`，`decision.messages` 里的对象改不了，只能整体新建数组再 splice。
3. **必须去重**：注入块会进历史并被之后每个请求重发，不去重就是滚雪球。

这三条与"8 轮重发"一起由 `plugin/dsh-attune-memory/test/recall.test.mjs` 的 8 组断言守住（`node plugin/dsh-attune-memory/test/recall.test.mjs`）。

插入位置是**最后一条真人消息之前**，不是数组最前：背景要贴着问题；数组最前会让人（和模型）以为那是上一轮对话的结尾。

## 3. 两个写入口

### 3.1 A：模型工具（主）

`memory_remember / _search / _update / _supersede / _merge / _forget / _find_similar / _explain`。

工具描述里写清了 `_update` 与 `_supersede` 的区别（同一件事换说法 vs 事实本身变了），因为模型分不清这两个词就一定会乱用。每个 `execute` 内部 try/catch，失败返回 `[记忆操作失败] …` 文本而不是抛错——记忆坏了不该炸掉整轮回答。

### 3.2 B：确定性抽取（兜底）

挂在 `session/event` 的 `turn/end`：

```js
ctx.on('session/event', (session, event) => { ... })
```

选这个钩子的原因：`agent/turn-stopping` 只在 `inbox.nextStep.length === 0` 时派发（`dsh-agent-loop/lib/index.js:791`），有后续 step 就静默不触发，不能当唯一写入口。

去重规则（P8 实测修出来的）：`turn/start` 记 ISO 时间，`turn/end` 先查引擎的 `recent(since)`；**只要这一轮存在 `actor === 'user'` 的变更（模型自己调过工具），整轮跳过抽取**。工具写记 `user`，抽取写记 `inferred`，判据是确定性事实，不需要猜相似度。

8 条句式规则（identity / preference / interaction / request-style / goal / constraint / person / explicit），一条消息最多 3 条，全部 `explicit:false`、`confidence:0.65`，且**不注入 `domain_key`**——兜底通道自己造槽位会和主通道撞车。

## 4. 压缩后重注入（压舱石）

设计上分三层，互不依赖：

| 层 | 谁负责恢复 | 机制 |
|---|---|---|
| S 手册 | 无人需要负责 | 在 system 段，压缩碰不到 |
| C 稳定层 | DSH 自己 | `retained = null` → `project()` 重发 |
| T 相关层 | 本插件 | 监听 `compaction/end` → 置 `reinjectPending` → 下一轮 `pre-step` 整块重建（`form:'reinject'`、`afterCompaction:true`），同时清空去重集合 |

T 层重建时会把"稳定块 + 相关块"合成一条。若这轮问题一条都没命中，**仍然会注入稳定块**——压缩之后本会话的注入块全被遮蔽了，"没有新命中"不等于"不需要背景"。

### 验证（真实会话，非模拟）

用一份把自动压缩阈值压到 2% 的叠加层（`.verify/compact-patch.yml`）跑了两轮真会话，会话日志里的证据：

```
compaction/start | turn=2
compaction/summary
user/message     | (replace，seq=82，kind=compact-checkpoint)
compaction/end   | turn=2
step/start       | turn=2 step=1
user/message     | seq=85 kind=dsh-attune-memory form=reinject afterCompaction=true
                   ids=m-mwf2bbj3-10,m-mwf2bbj4-2j
user/message     | seq=86 kind=user      ← 真人问题
user/message     | seq=87 kind=runtime-context form=snapshot
```

顺序说明一切：压缩先把整段历史换成一个摘要，紧接着同一个 step 里，记忆块被重新造出来并插在真人问题**之前**。第二轮的问题是"先歇会儿，帮我列三条明天复习的建议"——一个和记忆毫无关系的句子，稳定层依然回来了。

同一份日志里还验证了当时 C 通道的自动恢复（v1.1 已改为 S2，此处保留作为 DSH 原生机制的证据）：

```
seq=9  runtime-context snapshot（含 [长期记忆 · 稳定事实]）
seq=34 compaction replace（遮蔽 seq=9）
seq=40 runtime-context snapshot（自动重发，仍含稳定块）
```

v1.1 的 S2 证据则在 `.verify/live-s2.db` 那轮：稳定块直接出现在 `system/message` 的正文里，会话里没有对应的 user 消息。

## 5. 约束 A 验证（没有第二份画像数据）

```
1. 库里的表：['memories', 'memory_change', 'memory_meta']
2. 稳定块 id：['m-mwf2bbj3-10', 'm-mwf2bbj4-2j']
   相关块 id：['m-mwf2bx9m-1h']
3. 两通道 id 都来自同一张 memories 表：True
4. 除 memory_ 前缀的辅助表外没有别的表：[]
5. 软删 m-mwf2bbj3-10 后还在稳定块里？False
6. 新稳定块：'[长期记忆 · 稳定事实]\n- 交互方式：…'
```

判定标准是 P0 里定下的那句话：**"删掉除主表外所有缓存/副本，行为不变"**。`memory_change` 是生命周期审计（P6 要求），`memory_meta` 是键值配置，都不是画像副本；稳定块与相关块是每次查询现算的视图，删一条记忆两个通道同时变——没有双写，也没有需要同步的缓存。

## 6. 装进 profile

DSH 的 profile 由三部分拼成：`package.json` 的 `dsh.profile.bundles`（顺序即加载顺序）+ `cordis.patch.yml`（补丁层）+ 命令行 `--patch`。新增插件就是三件事：

```jsonc
// <DSH_HOME>/profiles/<name>/package.json
{
  "dependencies": { "dsh-attune-memory": "link:../../../../work/…/plugin/dsh-attune-memory" },
  "dsh": { "profile": { "bundles": [ /* …, */ "dsh-attune-memory" ] } }
}
```

```yaml
# plugin/dsh-attune-memory/cordis.patch.yml
- insert:
    - id: dsh-attune-memory
      name: dsh-attune-memory
      config:
        engineDir: "<REPO_ROOT>/engine"
```

三个坑：

1. **`dsh plugin add` 在本机不可用**：它要调 `pnpm`，bundled pnpm 的 `.cmd` shim 在中文路径下解析失败（`系统找不到指定的路径`）。改用 `cmd /c mklink /J <profile>\node_modules\dsh-attune-memory <plugin dir>` 做目录联接，再手写 `package.json` 的 `dependencies` 与 `bundles`。
2. **`Set-Content -Encoding UTF8` 会写 BOM**，`readProfileManifest` 报 `<anonymous_script>:1 \uFEFF{ SyntaxError`。手写时用 `[System.IO.File]::WriteAllText(path, json, New-Object System.Text.UTF8Encoding($false))`。
3. **`desktop` profile 被 Electron 独占**：`dsh --profile desktop` 直接报 `profile "desktop" is managed exclusively by the Electron application`。验证要用自建 profile（`dsh memtest --from-default-profile headless --dump-config` 生成），真正上线则是在 desktop 的 `package.json` 里加依赖，重启应用。

验证组合是否正确：

```
dsh --profile memtest --dump-config | Select-String -Context 0,3 "dsh-attune-memory"
# == dsh-attune-memory
# - id: dsh-attune-memory
#   name: dsh-attune-memory
#   config:
#     engineDir: D:/path/to/dsh-attune-memory/engine
```

## 7. 配置

| 配置项 | 默认 | 说明 |
|---|---|---|
| `engineDir` | 必填 | 引擎目录（含 `attune_memory/` 包） |
| `dbPath` | `$DSH_HOME/attune-memory/memory.db` | 单库，不按工作区拆（ADR-009） |
| `python` | `python` | 建议填绝对路径；插件用 `PYTHONUTF8=1`，RPC 侧也强制 UTF-8 |
| `timeoutMs` | `5000` | 单次 RPC 超时；超时按 `TIMEOUT` 拒绝，插件 fail-open |
| `user` | `default` | 多用户预留位 |

## 8. 验证清单（都实际跑过）

| 项 | 命令 | 结果 |
|---|---|---|
| 引擎单元测试 | `cd engine; python -m unittest discover -s tests` | `Ran 42 tests … OK` |
| stdio RPC 冒烟 | `node engine/tests/rpc_smoke.js` | exit 0，6 条请求全通 |
| 端到端写入 | `dsh --profile memtest --patch … "我叫张三…"` | 模型调工具写 2 条，无重复，`problems: []` |
| 端到端读取 | 新会话问"你记得我什么" | 答出两条画像，未编造 |
| T 通道 | 问"我极限题总是卡在变形方向" | 会话日志 `source=dsh-attune-memory/recall` |
| 压缩后重注入 | 两轮 + 2% 阈值叠加层 | 会话日志 `form=reinject afterCompaction=true` |
| 约束 A | 表清单 + 软删影响面 | 只有主表，两通道同时变 |

## 9. 失败模式

| 情况 | 表现 | 处理 |
|---|---|---|
| 引擎进程没起来 | 工具返回 `[记忆操作失败] …`，本轮无注入 | fail-open，回答正常 |
| RPC 超时 | `TIMEOUT` | 同上；下一次调用自动重启进程 |
| 库文件被锁 | `WRITE_FAILED` | 同上；引擎侧 `busy_timeout=5000` |
| 检索到无关内容 | 无 | 命中闸门 0.15 + 每轮只注入一次 + 去重 |
| 压缩把注入块遮蔽 | 下一轮整块重建 | `compaction/end` → `reinjectPending` |
| 插件 dispose | 子进程 `stdin.end()` 后 kill | `ctx.effect` 里注册的清理 |

## 10. 卸载

1. 从 `package.json` 的 `dsh.profile.bundles` 里删掉 `dsh-attune-memory`（依赖行可留）。
2. 重启应用。
3. 记忆库在 `$DSH_HOME/attune-memory/memory.db`，删不删都不影响 DSH 本身——这是 ADR-009 选单库、且不放进工作区的直接好处。

## 11. 已知限制

- **ACP 代理层压缩看不见**（`billion-context` 在请求体层改写，不走会话事件）：它抹掉的内容我无从感知。v1.1 的应对是把稳定层搬进 system prompt（压缩碰不到），T 通道再加一条"同一段内容隔 8 轮允许重发"的兜底——代价是被折叠后最多有 8 轮看不到那条上下文。
- **system prompt 变化会丢一次前缀缓存**：S2 的文本一变，system 节点就重写（这是从 `context` 改到 `section` 换来的代价）。所以 `stable_block()` 刻意不含时间词、条数与字数都卡得很紧。
- **去重集合在内存里**：插件重启后首轮会重复注入一次（最多一轮冗余）。
- **工具写入依赖模型主动性**：抽取值只是补位，模型完全不调工具时只能靠 8 条句式规则。
