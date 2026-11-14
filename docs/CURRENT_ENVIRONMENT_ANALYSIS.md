# CURRENT_ENVIRONMENT_ANALYSIS — 当前环境分析（P1）

> 目的：在写任何 SQLite / schema / 插件代码之前，用**可核对的真实证据**回答三件事：
> ① 这套 harness 到底怎么跑一轮对话；② Memory 的 Read（注入）与 Write（写入）应该挂在哪；
> ③ 上下文被压缩之后，Memory 怎么重新回到模型眼前。
>
> 证据等级：本文每条结论都标注来源。`[代码]` = 本机解包源码 + 行号；`[文档]` = 包内 README；
> `[实测]` = 本机运行结果；`[推断]` = 由上述推出、尚未直接验证（对应 `UNKNOWN_LIST.md`）。
>
> 源码基准：`<DSH_INSTALL>\resources\app.asar` 内 `dsh/`，已解包至
> `<REPO_ROOT>\_dsh_extract`（21047 文件）。
> 下文所有 `dsh-xxx/lib/index.js:NNN` 均指 `_dsh_extract\node_modules\@deepseek-ai\dsh-xxx\lib\index.js`。

---

## 0. 结论先行

| 关注点 | 落点 | 依据 |
| --- | --- | --- |
| **Read 注入（每轮，按当前用户消息检索）** | `ctx.on('agent/pre-step', async (payload, next) => …)` 里 splice `decision.messages` | `dsh-agent-loop/lib/index.js:685`；`dsh-agent/README.md:98` |
| **Read 注入（常驻、会话级稳定）** | `ctx.systemPrompt.context({ name, order, text })` | `dsh-system-prompt/lib/index.js:281`、`:326`；`dsh-agent-loop/lib/index.js:843` |
| **Write 写入（回答之后）** | `ctx.on('session/event', (session, event) => …)`，看 `turn/end` / `assistant/message` | `dsh-session/lib/index.js:1596-1605`；`dsh-agent-loop/lib/index.js:819` |
| **Write 写入（模型主动）** | `ctx.tools.register({… memory_* …})` | `dsh-tools`；两份参考实现均以工具为唯一写入口 |
| **压缩后重注入** | ① 常驻层由 `RuntimeContextProjection` 自动恢复；② 检索层用 `compaction/end` 置 pending 标记，下次 pre-step 重发 | `dsh-agent-loop/lib/index.js:297-349`、`:324`；`dsh-compaction-basic/README.md:123` |
| **用户管理界面** | `ctx.webServer.register(prefix route)` + 客户端 `settings.section` | `dsh-local-memory` 实现；`ctx.get('connection').requestRejection(req)` 做信任栅栏 |

一句话：**注入靠 pre-step 改 messages（每轮）＋ systemPrompt.context（常驻且压缩后自动恢复）；写入靠 session/event 观察 turn/end ＋ 模型工具；压缩恢复必须两层都做。**

---

## 1. 环境事实

### 1.1 本体与版本
- 桌面端是 Electron 壳，DSH 全部实现打包在 `resources\app.asar`（213,202,918 字节）内，`dsh\package.json` 声明 `@deepseek-ai/dsh-desktop-runtime` **v0.2.2-rc.1**，`"type": "module"`。`[代码]`
- asar 内共 **331** 个 `@deepseek-ai/*` 包（`_dsh_extract\node_modules\@deepseek-ai`）。asar 没有 `docs/` 目录，官方文档就是各包的 `README.md`（部分带 `README.zh.md`）。`[实测]`
- `read` 工具能直接读 asar 内路径（Electron 的 fs patch），但 `glob` / `grep` / `Get-ChildItem` 不能——ripgrep 是独立二进制，看不到 asar 虚拟层。**所以研究必须先解包**，解包脚本用 `ELECTRON_RUN_AS_NODE=1` + `fs` 递归复制，成功（`copied 21047`）。`[实测]`

### 1.2 Profile 与插件装载
- `DSH_HOME=<DSH_HOME>`，`DSH_PROFILE=desktop`，`DSH_PROFILE_DIR=…\<DSH_HOME>\profiles\<profile>`。`[实测]`
- profile 的 `cordis.yml` 只有 `[]` 四行，注释写明组合方式是**补丁叠加**：`package.json` 的 `dsh.profile.bundles` 顺序 → `cordis.patch.yml` → `--patch`。`[代码]`
- 已启用 bundles（顺序）：`@deepseek-ai/dsh-base`、`@deepseek-ai/dsh-web-app`、`dshmarket`、`dsh-pet`、`dsh-session-manager`、`dsh-session-folders`、`dsh-upload-file`、`billion-context`、`dsh-plugin-wallpaper-engine`、`dsh-context`、`dsh-better-sidebar`。`[代码]` `<DSH_HOME>\profiles\<profile>\package.json`
- 因此**接入 Memory 的标准做法**（P8 采用）：装一个带 `dsh.bundle.patch` 的包 → 进 `dsh.profile.bundles`，配置改 `cordis.patch.yml` 里对应 `id` 的条目。已有的 `dsh-local-memory`（第三方）就是这个形态：`cordis.patch.yml` 里只有 `- insert: [{ id: dsh-local-memory, name: dsh-local-memory }]`。
- 运行时服务名与包名同源：`ctx.systemPrompt`、`ctx.sessions`、`ctx.tools`、`ctx.webServer`、`ctx.get('connection')`。**访问未在 `inject` 里声明的服务会运行时报 `cannot get property "X" without inject`**，而如果外层套了 `try/catch`，表现就是"插件加载成功但功能全没有"——这是 `dsh-local-memory` 源码里用血写下的注释。`[代码]` `refs\nodal\src\host\index.js`

### 1.3 关键包（本任务够用的最小面）
| 包 | 作用 | 我需要它的哪一部分 |
| --- | --- | --- |
| `dsh-agent-loop` | 一轮对话的驱动器（turn / step / 注入点 / 压缩协作） | `preStep`、`step`、`turn`、`RuntimeContextProjection` |
| `dsh-agent` | Agent 句柄与事件契约 | `inject()`、`PreStepDecision`、`agent/turn-stopping` |
| `dsh-session` | 事件溯源会话（append / deriveMessages / surfaceOp） | 生命周期事件、`session/event` 观察者 |
| `dsh-system-prompt` | prompt 组装（section / context / assemble / refreshContext） | **常驻注入与压缩恢复** |
| `dsh-compaction` + `dsh-compaction-basic` | 压缩契约与默认后端 | 压缩何时发生、如何被观察 |
| `dsh-tools` | 工具注册与执行 | 模型主动写记忆的通道 |
| `dsh-agent-instructions` | 读 `message.source.kind` | 决定我注入消息**必须**带 source |

---

## 2. 消息生命周期（真实代码）

### 2.1 一轮对话的调用链

```
用户输入
  → agent.followup(msg) / inbox 入队         [dsh-agent]
  → turn()                                    dsh-agent-loop/lib/index.js:724
      session.append("turn/start", {turn})    :731
      ├─ preStep(target, {turn, step})        :742 → :666
      │     claimed   = inbox.claim(...)                       :670
      │     selection = model.current()                         :671
      │     toolSet   = tools.resolve(...)                      :673
      │     assembly  = systemPrompt.assemble({…, modelSelection, toolSet})   :679
      │     decision  = waterfall("agent/pre-step", {messages: claimed, …})   :685
      │        └ 默认 fallback：渲染 context sections → runtimeContext.project  :690-697
      │           返回 { kind:'enter', messages: [...claimed, context] }
      │     reject → 本步取消                                  :699-702
      ├─ step/start                             :757
      └─ step(decision)                         :833 → :767
            renderedPrompt = renderPrompt(assembly)            :839  ← 变量插值在此
            prepareRequest(...) → preparedCall                 :842
            refreshContext(assembly, {…}) → sections           :843  ← 每次请求前重算
            runtimeContext.project(sections)                   :847
            systemPrompt.project(rendered, {inHistory, …})     :850  ← 决定 prompt 怎么落到 surface
            append system/message（每个 commit）               :854
            append decision.messages → user/message            :860-866
               ★ 若 message.source.kind === "runtime-context"，用刚算出的 context 替换
            llm.stream(request)                                :877
            append assistant/message (surfaceOp 追加)          :949
            tools.execute(...) → tool/call、tool/result        :961
      ├─ 若回合已该结束且 inbox 为本步空
      │     dispatch.serial("agent/turn-stopping", {turn})     :791
      └─ finally: session.append("turn/end", {turn, reason})   :819
```

要点（都来自上面的行号）：
1. **`agent/pre-step` 拿到的是本步"已认领"的完整 user 消息批次**（`payload.messages`），可以整体替换；这是唯一能同时"看到本轮用户说了什么"又"改模型输入"的位置。
2. **`assemble()` 早于 `agent/pre-step`**（:679 vs :685）。所以 `systemPrompt.context()` 的 provider **看不到本轮用户消息**——常驻内容能用它，按查询检索不能用它。
3. **`agent/turn-stopping` 是 `serial`，且在"回合将结束且 inbox 已空"时才跑**（:790-796）。它可能 steer 让回合继续，属于"改变流程"的钩子，不适合做旁路记账。
4. **`session.append(...)` 是同步的**，观察者也在同一调用栈里被通知（见 2.3）。

### 2.2 首轮 vs 后续

| 维度 | 首轮 | 后续轮 |
| --- | --- | --- |
| inbox 认领 | 第一条真人消息，通常是唯一的 user message | 可能一次认领多条（用户连发） |
| session 历史 | 空（或无 user/message） | 已有历史，可能有压缩摘要 |
| system prompt | 首个 commit 必须占用 surface node 0（`dsh-system-prompt` 的 project 决策） | 支持 `systemPromptUpdate: 'in-history'` 的 route 追加在缓存历史后，其余就地改写 node 0 |
| 注入窗口 | 用户消息之前 / 之后都可以 | 同上，但**必须按 id 去重**，否则同一块每轮重发，历史滚雪球 |

结论：Memory 在**首轮与后续用同一套 read_for_context**，只在"首轮可多给一点"上做预算差异（P4 定义），不做两套机制。首轮不是特殊路径，只是一个还没历史的位置。

### 2.3 会话事件（Write 的观察面）

`dsh-session` 在每次 append 后同步通知 `session/event` 观察者：

```js
// dsh-session/lib/index.js:1596-1605
const callbackArgs = [this, event];
const callbacks = collectSessionCallbacks(entry.emitCtx, [entry.carrier, "session/event", ...callbackArgs]);
this.log.push(event);
if (entry !== undefined && callbacks !== undefined)
    invokeContainedSessionObservers(entry.emitCtx, "session/event", entry.id, callbackArgs, callbacks);
```

插件侧签名（`dsh-local-memory`、`meow-memory` 均如此，实测一致）：`ctx.on('session/event', (session, event) => …)`，`event.type` + `event.data`。`[代码]`

可用事件词表（从 `dsh-session` / `dsh-compaction-basic` 的字面量 grep 核实，不是猜的）：
`user/message`、`assistant/message`、`assistant/attempt`、`tool/call`、`tool/result`、`tool/ptc-dispatch`、`tool/ptc-dispatch-start`、`system/message`、`developer/message`、`request/header`、`request/context`、`compaction/start`、`compaction/summary`、`compaction/summary-error`、`compaction/prune`、`compaction/end`、`turn/start`、`turn/end`、`step/start`、`step/end`、`session/created`、`session/disposed`、`session/end-seed`、`session/title`、`session/event`、`session/flush`。

**顺序保证**：`assistant/message` →（`tool/result`）→ `step/end` → `turn/end`。所以 `turn/end` 是"整轮答案已落地"的最早稳定点。

---

## 3. 接入点 A：Read（注入）

### 3.1 位置与代码

```js
// 每轮、按当前用户消息检索后注入
ctx.on('agent/pre-step', async (payload, next) => {
  const decision = await next();                       // 先让内置逻辑算完（含默认 runtime-context）
  if (decision.kind !== 'enter') return decision;      // 被上游 reject，别插手
  const users = decision.messages.filter(m => m.source?.kind === 'user');
  if (users.length === 0) return decision;             // 没有真人输入（子代理/系统消息）
  const block = await recall(users);                   // 可选：同步读引擎
  if (!block) return decision;
  const rewritten = [...decision.messages];
  rewritten.splice(rewritten.indexOf(users.at(-1)), 0, block);   // 插在最后一条真人消息之前
  return { ...decision, messages: rewritten };
});
```

- `decision.kind` 只有 `'enter'` / `'reject'`（`dsh-agent/README.md:98`），`enter` 携带"完整、已冻结的 message 批次"，接受不等于提交。`[文档]`
- 默认 fallback 已经会把 runtime-context 追加在 `claimed` **之后**（`:695`），所以我的块插在"最后一条真人消息之前"，位于 runtime-context 之前——顺序为：真人消息 → 记忆块 → 运行时上下文。这符合"记忆是给模型的参考资料、不是最新指令"的语义。
- **必须 fail-open**：任何异常 `catch` 后 `return decision`。`meow-memory` 就是这么写的（`try{return await preStepInject(...)}catch{return decision}`），否则记忆库坏了会把整个对话打死。`[代码]`

### 3.2 三个硬性要求（都有踩坑证据）

1. **注入消息必须带 `source.kind`**。`dsh-agent-loop` 自己就在 `:860` 无保护地读 `message.source.kind`；`dsh-agent-instructions` 也在 `:1280` / `:1273` 这么读。缺 `source` 会在**第一个 step 之前**抛 TypeError，界面只显示"本轮运行失败"。`[代码]`
2. **消息对象是 readonly 的**：不能改原对象，必须 `createUserMessage({...})` 造新的。`[代码]`（`dsh-local-memory` 源码注释）
3. **同一个块在同一会话只注入一次**（按内容或 id 记 seen）。注入块会进会话历史，之后每个请求都重发；不去重就会"历史像滚雪球"。`[代码]`

### 3.3 为什么不用 `systemPrompt.section` 做检索注入

`section()` 的文本在 `assemble()` 时定型。`assemble()` 与 `refreshContext()` 共用同一个上下文对象：

```js
// dsh-agent/lib/index.js:407-413
function assembleContextFor(agent, signal) {
  return { agent, scope: agent, ...(signal === undefined ? {} : { signal }) };
}
// 调用处：dsh-agent-loop/lib/index.js:679-683 / :843-846 再补 modelSelection、toolSet
```

所以 provider / section 的 `text(context)` 拿到的是 `{ agent, scope, signal?, modelSelection, toolSet }`——**它能顺着 `agent.session` 读历史**。但注意时序：

```
:670  claimed = inbox.claim(...)   ← 本轮用户消息已从 inbox 摘走
:679  assemble(...)               ← 想"按当前问题检索"就在此时
:862  append user/message          ← 本轮用户消息这时才进 session
```

即 `assemble()` 时，**本轮刚说的那句话既不在 session 里、也不在 inbox 里**，provider 根本看不到。想在 section 里做"按当前问题检索"只能拿上一条历史消息凑数，等于把上一轮的检索结果延迟到下一轮——语义错误。

结论：**section/context 只放"记忆使用规则"和"会话级稳定事实"**，这也正是两份参考实现的做法（`meow-memory:guide` order 130、`dsh-local-memory:brief` order 60）。按当前问题检索只能在 `agent/pre-step` 里做。

### 3.4 常驻注入与 KV 缓存

- 静态 section 文本恒定 → system 节点不动 → 前缀缓存不失效。`dsh-system-prompt` README 明确：渲染不变则 system 节点完全不动。
- 若 route 支持 `systemPromptUpdate: 'in-history'`，prompt 变化追加到缓存历史之后（保留前缀）；不支持的 route 会在第一个 system 节点就地改写，**从该 token 起缓存全部失效**。`[文档]`
- 结论（写入 ADR）：**常驻记忆块必须"内容稳定、按天/按事件变"，不能按轮变**。按轮变的东西一律走 pre-step 注入（它是 user 消息，本来就在变动区）。

---

## 4. 接入点 B：Write（写入）

### 4.1 两个候选与结论

| 候选 | 机制 | 判断 |
| --- | --- | --- |
| `agent/turn-stopping` | `serial` 钩子，回合关闭前 | **不采用为唯一写入口**：inbox 非空时会被跳过（`:797`），且它可能 steer 改变回合走向；记账不该有这种权力 |
| `session/event` → `turn/end` | append 后同步观察，纯旁路 | **采用**：一定能拿到 `turn/end`，不干预流程，天然 fail-open |
| 模型工具 `memory_*` | 模型自己决定写什么 | **采用**：作为"高质量写入"的主通道（见 4.3） |

### 4.2 写入必须是"两段式"

真实环境里有一个绕不过去的事实：**判断"这句话值不值得记"需要语义理解，而 V1 不允许 Embedding、也不该为每条消息额外调一次模型**。两份参考实现给出了同一个答案——

- `dsh-local-memory`：写记忆**完全由模型调工具**完成，代码里没有任何自动抽取；它把"什么时候该写"写进常驻提示词（`prompt.js:58-74` 的"收工自检"：改了代码/bug/发布/装插件/定约定 → 自检"有没有下次还能复用、且从代码里看不出来的结论" → 有就 `memory_save`，没有就跳过，别为凑数而存）。
- `meow-memory`：同样是工具驱动（`memory_remember` 必填 content/project/keywords/importance），另有 `agent/turn-stopping` 触发的"反思轮"（连续工具 step ≥7 才触发，让模型自评）。

**V1 采用**：`session/event` 上的 `turn/end` 只做**确定性抽取**（用户在最后一条消息里显式陈述的偏好/状态，如"我不考研了""以后都用中文"），产出 candidate 后走同一套 Memory Manager 决策；其余靠模型工具。两条路共用写管线（P3），不各自为政。

### 4.3 工具注册

```js
ctx.tools.register({ name: 'memory_save', description, parameters, async execute(args, ctx) {…} })
```

- 逐个 `try/catch` 注册，重名不拖垮插件（`dsh-local-memory` 的做法）。
- 工具描述是"给模型的接口文档"，必须写清：**什么时候用、什么时候不用**、`scope` 怎么填、`importance` 与 `injection_policy` 的区别（防止模型以为"重要"= "每轮注入"）。

---

## 5. 接入点 C：压缩后重注入

### 5.1 压缩实际做了什么

`dsh-compaction-basic`（默认后端）：
- 触发式：`floor(min(W × thresholdRatio(0.8), W − O − headroomTokens(65536)))`，保留最近 `retainRatio`（默认 16% of `W−O`）原文。`[文档]` `dsh-compaction-basic/README.md:64-71`
- 手动：`/compact`（需要 `@deepseek-ai/dsh-command-compact`）。`[文档]` 同上 `:92-96`
- 表面变更**只有一次**，是一条带 `surfaceOp: { op:'replace', startSeq, endSeq }` 的 `user/message`（把摘要渲染成一条 user 消息）。`compaction/start`、`compaction/summary`、`compaction/end` 都是 log-only，不上 surface。`[文档]` `dsh-compaction/README.md`
- **被遮蔽的事件仍在原始日志里**，所以 replay 确定；但 `deriveMessages()` 之后模型看不到它们。`[文档]`
- 上限：压缩不了 system prompt / tools / session prefix，也拆不开单个巨大 tool call。`[文档]` `:12`

**对我的直接后果**：任何"只注入一次、之后靠历史"的记忆块，压缩之后就从模型眼前消失了。首轮快照派（`meow-memory`）因此必须显式做压缩后重注入。

### 5.2 官方内建的恢复机制（最重要发现）

`dsh-agent-loop` 里有一个 `RuntimeContextProjection`（`:297-349`），它维护"最后一次被保留进 surface 的 runtime-context 快照"：

```js
retained;   // undefined = 从来没有；null = 有但已被压缩掉
constructor(ctx, session) {
  // 从最新往旧扫，找到仍在 surface 上的、属于我的 user/message → retained = {seq, text}
  ctx.on("session/event", (subject, event) => {
    if (subject !== session) return;
    if (event.type === "user/message" && isOwned(event.data)) this.retained = { seq: event.seq, text: textOf(event.data) };
    else if (this.retained && isReplacementSurfaceEvent(event) &&
             event.sourceEventSeqs?.includes(this.retained.seq) === true) this.retained = null;   // :324
  });
}
project(current, sections) {                       // :333
  if (this.retained === undefined && current.length === 0) return;
  const snapshot = current.length === 0 ? CLEARED : current;
  if (this.retained?.text === snapshot) return;    // 没变就不发
  return createUserMessage({ content:[{type:'text',text:snapshot}],
    source: sections.length === 0 ? { kind: SOURCE } : { kind: SOURCE, form: 'snapshot', sections } });
}
```

其中 `SOURCE = "runtime-context"`、`CLEARED = "Current runtime context: none. Earlier runtime-context snapshots no longer apply."`（`:212-213`），`isOwned(m) = m.source.kind === SOURCE`。

**推论（写进 ADR-006）**：只要把常驻记忆挂到 `ctx.systemPrompt.context()`，它就会：
1. 与运行时上下文合并进同一条 `source.kind = "runtime-context"` 的快照消息；
2. 文本不变 → 不重复 append；
3. **被压缩遮蔽 → `retained = null` → 下一个 step 自动重新 append**（`:847` → `:866`）。

这条路径不需要我维护任何 pending 状态，是官方语义。`agent-loop/README.md:120` 的描述 "restores removed runtime context" 指的就是它。

### 5.3 检索层（按轮注入）的恢复

按轮注入的块不在 `runtime-context` 名下，不会被上面那套自动恢复。必须自己做：

```
ctx.on('session/event', (session, event) => {
  if (event.type === 'compaction/start' || event.type === 'compaction/summary') clearSeen(session.id);
  if (event.type === 'compaction/end' && !event.data?.error) markReinject(session.id);
});
```

- 清 seen 的理由：被压缩掉的块，其 id 仍记在"已注入"集合里 → 之后永远不再注入（这正是 `dsh-local-memory` 的缺陷：它的 `injectedBySession` 只在 sessionId 上记最后一次文本，全代码没有任何 compaction 处理，见 §8.3）。
- 置 pending 而不是立刻注入：`compaction/*` 期间会话正被重写，此刻拿到的 surface 是中间态；下一次 `agent/pre-step` 才是稳定时机。

### 5.4 端到端验证方案（P8 必做）

1. 造 8–12 轮对话，塞入足量工具输出把压力推过阈值，或直接 `/compact`。
2. 压缩后观察三件事：
   - a) 摘要消息出现（`<compacted-summary>` 包裹的 user 消息）；
   - b) 我的常驻块**自动重新出现**（`source.kind === 'runtime-context'`，无需我动手）；
   - c) 我的检索块在下一轮 pre-step 重新注入（logs 里能看到 pending 被消费）。
3. 反证：压缩前把引擎里那条被注入过的记忆 `forget`，压缩后不应再出现——证明恢复的是"按当前库状态重建"，不是"重放旧文本"。

---

## 6. 其余可观测面（P8/P9 会用）

| 能力 | 用法 | 依据 |
| --- | --- | --- |
| 取当前 cwd | `ctx.get('sessions').get(agent.id).header.cwd`，兜底 `agent.cwd` / `process.cwd()` | `dsh-local-memory` |
| 管理 UI 后端 | `ctx.webServer.register({ kind:'prefix', path, handler })`；**每个请求先过 `ctx.get('connection').requestRejection(req)`** | `dsh-local-memory` |
| 管理 UI 前端 | 单文件客户端 `window.__ModuleLoader__.load({ id, factory })`，挂 `settings.section` | 同上 |
| 会话结束清理 | `ctx.on('session/disposed', …)` / `ctx.effect(() => () => cleanup)` | `dsh-agent`、`dsh-session` |
| 禁用能力 | 不要用 `session.ownEvents()` / `eventAt()` / `snapshotEvents()`——`dsh-session` README 明确"new production calls are prohibited"；插件也**不得**调 `appendPluginRecord`（仅 `packages/experimental/` 内部可用） | `dsh-session/README.md` |

---

## 7. 并存的两个"压缩层"（必须写清，否则 P8 会误判）

本机上除了 `dsh-compaction-basic`，还装了 **`billion-context` 0.1.212**（本会话的上下文管理功能就由它提供，工具调用里能看到 `search_context` / `acp_status` 这类工具）。

- 它的压缩发生在**代理层**（改写发往模型的请求体），不经过 `session.append`，因此**不会产生 `compaction/end`**，插件的 `session/event` 观察者完全看不见。
- 已核实：`billion-context/dist/index.js` 里 `surfaceOp` / `agent/pre-step` / `compaction/end` / `session/event` 命中数均为 0 —— 它不走 DSH 的会话事件通道。`[实测]`

设计含义（写进 ADR）：
1. **不能假设"注入一次就长期有效"**。任何记忆块都可能被任一层吃掉。
2. 因此 V1 的记忆注入**按轮重建、按内容去重**，而不是"首轮快照 + 长期驻留"。这既对抗 ACP 折叠，也对抗 `dsh-compaction`。
3. P8 的"压缩后重注入"验证以 `dsh-compaction`（可观测、可断言）为主证据；ACP 层的表现只做旁证记录，不承诺可控（那是代理的行为，不是我的接入点）。

---

## 8. 与两份参考实现的差异（细节见 `ADOPTION_DECISION_TABLE.md`）

### 8.1 meow-memory 0.32.7（快照注入派）
- 七层 SQLite（soul/user/project/fact/lesson/topic/rules），首轮插一条 plugin snapshot，第二轮起按关键词 top-2 命中。
- 静态 `section` order 130 放"记忆手册"，服务未就绪时每秒重试最多 20 次。
- 压缩后：`compaction/end` → pending → 下次 pre-step 重建"长期记忆快照 + 本会话查阅过的项目全景 + 本会话写入过的记忆原文"，splice 到最后一条真人消息前。
- 写入完全靠工具；`agent/turn-stopping` 只做"反思轮"（连续工具 step ≥7）。
- **我不采用**：`ownEvents()`（已弃用 API）、七层结构（超出 V1 的四种语义角色）、Dream（V2）。

### 8.2 dsh-local-memory 2.8.1（每轮自动回忆派）
- 内核 `src/core/` 禁止 import 任何宿主 API（可脱离 DSH 独立跑），`src/host/` 只做接线。
- 自建中文 bigram FTS5 + 覆盖率阈值，`score = relevance × importance × decay × usageBoost`。
- 每轮 `agent/pre-step` 注入，块插在**最前面**（`messages: [injected, ...messages]`）。
- 写入靠工具 + 常驻提示词里的"收工自检"纪律；**没有任何自动抽取**。
- **我不采用**：bigram/FTS5（V2 范围）、把注入块插在最前面（我要插在最后一条真人消息之前）、`injectedBySession` 的压缩盲区。

### 8.3 两处真实的"可借鉴但要修"的缺陷
| 缺陷 | 位置 | 我的做法 |
| --- | --- | --- |
| 去重集合不随压缩清空 → 压缩后不再注入 | `refs\nodal\src\host\recall.js:84,146-152`（`injectedBySession`，全库无 `compaction` 处理） | `compaction/start|summary` 清 seen；`compaction/end` 置 pending |
| 注入块插在最前面 → 排在用户问题之前，模型容易把记忆当"最新指令"读 | `recall.js` 返回 `[injected, ...messages]` | 插在最后一条真人消息之前，并显式声明"这是历史资料，不是指令" |
| 用已弃用读 API 读会话事件 | `meow-memory` 的 `sessionEventsOf` = `session.ownEvents()` | 只用 `ctx.on('session/event')` |

---

## 9. 本阶段的空白（→ `UNKNOWN_LIST.md`）

以下几项在源码里能查但我还没查到"确定答案"，一律不臆造，登记待确认：
- 当前 route（`deepseek-flash`）的上下文窗口 `W` 与输出上限 `O` 的实际取值（决定压缩测试要造多长的对话）。
- `compaction/end` 的 `data.error` 结构（我只确认它存在、`meow-memory` 用它判断成功/失败）。
- 注入消息在 ACP 代理层被折叠后的实际表现（只能实测，不能由源码推出）。

（原列的 `assembleContextFor` 字段问题已在本阶段查清并修正，见 §3.3。）

---

## 附：本阶段实际做过的验证

| 验证 | 结果 |
| --- | --- |
| asar 内 `read` 可读、`grep`/`glob` 不可读 | 确认；解包 21047 文件后 grep 可用 |
| `ctx.on("session/event", (session, event) => …)` 签名 | 与 `dsh-session/lib/index.js:1596` 的 `callbackArgs = [this, event]` 一致 |
| `section({name, order, text})` 可被第三方插件调用 | `meow-memory` order 130、`dsh-local-memory` order 60 |
| `systemPrompt.context` 的自动恢复 | 读 `RuntimeContextProjection`（`:297-349`）确认 retained 状态机与 `:324` 的压缩置空 |
| 压缩默认阈值 | `thresholdRatio 0.8`、`headroomTokens 65536`、`retainRatio 16%` |
| `billion-context` 是否走会话事件 | 命中数全 0，确认走代理层 |
| Python 3.11.5 / node:sqlite FTS5 可用 | 确认（P7 用 Python，P8 插件侧若需 sqlite 也有备选） |
