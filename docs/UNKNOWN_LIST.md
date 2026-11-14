# UNKNOWN_LIST — 待确认清单（P1）

> 规则（来自任务书红线②）：**不臆造 harness 机制**。每条结论必须三选一——真实代码 / 官方文档 / 用户求证。
> 查不到的登记在这里，写明"阻塞什么""怎么确认""当前怎么绕"。
> 本清单在后续阶段持续更新；已确认的条目移到文末"已关闭"。

状态图例：`待确认`（尚无证据）/ `可绕行`（有临时方案，不阻塞当前阶段）/ `阻塞`（不确认不能往下走）。

---

## U-001 当前 route 的上下文窗口 `W` 与输出上限 `O`

- **问题**：`deepseek-flash` 通过 `deepseek-account` provider 接入，其 `W`（context window）与 `O`（output cap）的实际数值未知。压缩阈值是 `floor(min(W × 0.8, W − O − 65536))`，没有这两个数就算不出"要造多长的对话才会触发压缩"。
- **阻塞**：P8 的压缩后重注入验证（需要可控地跨过阈值）。
- **已知**：`compaction-basic` 在 base 里是**无 config 挂载**（`dsh-base/cordis.patch.yml:355-356` 只有 id + name），即全用默认值 `thresholdRatio 0.8` / `headroomTokens 65536` / `retainRatio 16%`。`token-meter` 与 `command-compact` 均已挂载。
- **绕行**：P8 不靠算，靠 `/compact` 手动触发（`command-compact` 已挂载，`dsh-compaction-basic/README.md:92-96`），必要时再用长对话触发自动阈值做旁证。
- **确认方式**：运行时读 `ctx.tokenMeter` / adapter capacity，或从 `request/header` 的 envelope 取 `maxTokens`。
- **状态**：`可绕行`

## U-002 `compaction/end` 的 `event.data` 结构

- **问题**：判断"这次压缩成功还是失败"需要 `data.error` 的准确形状（是 `{ message, code }`？还是字符串？失败时是否仍有 `data`？）。
- **阻塞**：不阻塞（可先按"`data?.error` 为真即失败"处理，这与 `meow-memory` 的做法一致）。
- **已知**：`meow-memory` 用 `const err = event.data?.error` 判断；`dsh-compaction` README 只说事件是 log-only、不上 surface。
- **确认方式**：P8 冒烟测试里把 `compaction/*` 事件的 `JSON.stringify(event.data)` 打到日志。
- **状态**：`可绕行`

## U-003 ACP（`billion-context`）折叠我的注入块后的实际表现

- **问题**：`billion-context` 0.1.212 在代理层改写请求体。我的注入块被它折叠后，模型实际还能看到多少？它会不会连"防注入声明"一起折掉？
- **阻塞**：不阻塞 V1 功能，但影响"注入是否长期有效"的设计假设（已在 ADR 里按"按轮重建"规避）。
- **已知**：已核实它**不走** DSH 会话事件通道（`dist/index.js` 里 `surfaceOp` / `agent/pre-step` / `compaction/end` / `session/event` 命中数均为 0），因此我对它**没有**任何接入点，只能观测不能控制。
- **v1.1 补充（已解释）**：它是**模型 API 层的代理**（中文 README 自述"架在任意编程助手与其模型 API 之间"、把 base URL 指向 proxy），注入 `compress/decompress/search_context/acp_status` 四个工具、由模型自己决定何时压缩，压缩发生在服务端重写请求体 —— 既不写会话事件、也不产生 `surfaceOp`，DSH 侧零感知。更关键的是它的 `dsh.bundle.patch.yml` 把 `compaction-basic` 的 `auto` 设成 `false`、并用 `bili-compaction-gate` 在 preset 里把 `dsh-compaction-basic`/`dsh-command-compact` 置 `disabled: true`：**DSH 自己的压缩在这台机器上根本不会自动跑**，我原来依赖的 `compaction/end` 与 `retained=null` 两条恢复路径都不会触发。
- **v1.1 绕行（已实施，ADR-019）**：稳定事实从 `systemPrompt.context`（历史里的快照消息，谁都能折）搬到 `systemPrompt.section(order=155)`（system prompt 的一部分）。代理层只重写 messages、不动 system 字段；DSH 的 compaction 也只从第一个非 `system/message` 节点开始。T 通道另加"同一段内容隔 8 轮允许重发"（`STALE_TURNS`），作为"折叠了但没人通知我"的兜底。
- **残留风险**：如果代理层某天连 system 字段也一起压缩，本节所有绕行都会失效——那时唯一可靠的位置只剩工具调用（每轮由模型主动 `memory_search`）。
- **状态**：`已解释 + 已绕行`（原为"待确认"）

## U-004 模型是否会遵守常驻手册里的"收工自检"纪律

- **问题**：写入主要靠模型主动调 `memory_save`。模型不照做，评估用例 A（不存垃圾）/ C（State 正确更新）就过不了。
- **阻塞**：P10 评估。
- **已知**：`dsh-local-memory` 与 `meow-memory` 都把这个当主通道，且都把纪律写进常驻 section；前者还额外写了"别问用户要不要存，直接存"。
- **确认方式**：P10 用真实对话跑用例 A/B/C，统计漏写率。
- **状态**：`待确认`

## U-005 `session/event` 在插件全局 ctx 上的可见范围与 `event.data` 形状

- **问题**：`user/message` 的 `event.data.content` 是 block 数组还是字符串？`source.kind` 一定是 `'user'` 吗？子代理的会话会不会同样广播过来（需要过滤）？
- **阻塞**：P8 的 `turn/end` 确定性抽取（要读用户原话）。
- **已知**：`dsh-session/lib/index.js:1596` 的 `callbackArgs = [this, event]`，即 `(session, event)`；`event.type` + `event.data`。`user/message` 的 `source.kind === 'user'` 表示真人（两份参考实现都这么判断）。
- **绕行**：抽取时用 `session.header.origin === 'subagent'` 直接跳过子代理会话；`content` 用"block 数组取 text，兜底 `String(content)`"的双分支读取。
- **确认方式**：P8 冒烟日志里打印一条 `user/message` 的完整 `data`。
- **状态**：`可绕行`

## U-006 P9 管理界面的客户端 bundle 何时生效

- **问题**：插件客户端代码（`window.__ModuleLoader__.load`）在 Web GUI 里是否需要重启/刷新才生效；本 profile 开着 HMR（`dsh-base` 里 `id: hmr`），但 HMR 只对 dev 模式下的特定包生效。
- **阻塞**：P9 验收方式（改完要不要重启）。
- **确认方式**：P9 实测：刷新页面 → 看 `settings.section` 是否出现；不行则重启 DSH。
- **状态**：`待确认`

## U-007 是否存在"另一个已在跑的记忆插件"造成双写

- **问题**：如果用户的 profile 里已经装了 `dsh-local-memory` / `meow-memory` 之类，两个插件会同时注入、同时写库。
- **阻塞**：不阻塞开发，但会导致 P10/P11 的观测被污染。
- **已知**：当前 `dsh.profile.bundles` 里**没有**任何 memory 插件（只有 dshmarket/dsh-pet/session-*/upload-file/billion-context/wallpaper-engine/dsh-context/better-sidebar），`dsh-base` 的模块 id 列表里也没有 memory 相关项。
- **状态**：`已确认无冲突`

---

## 已关闭

| # | 问题 | 结论 | 证据 |
| --- | --- | --- | --- |
| C-001 | `systemPrompt.context()` 的 `text(context)` 能拿到什么？ | `{ agent, scope, signal?, modelSelection, toolSet }`，可顺 `agent.session` 读历史 | `dsh-agent/lib/index.js:407-413`；`dsh-agent-loop/lib/index.js:679-683, 843-846` |
| C-002 | 压缩后常驻上下文会不会自动回来？ | 会。`RuntimeContextProjection` 在 replace 覆盖 `retained.seq` 时置 `retained = null`，下一 step 重新 append | `dsh-agent-loop/lib/index.js:297-349`（尤其 `:324`） |
| C-003 | `/compact` 是否可用？ | 可用，`command-compact` 已挂载 | `dsh-base/cordis.patch.yml:360-361` |
| C-004 | 注入消息缺 `source` 会怎样？ | 整轮在第一个 step 前抛 TypeError | `dsh-agent-loop/lib/index.js:860`；`dsh-agent-instructions/lib/index.js:1280` |
| C-005 | `session.ownEvents()` 能不能用？ | 不能。已弃用，新代码禁用 | `dsh-session/README.md`（"new production calls are prohibited"） |
