# dsh-attune-memory — DeepSeek Harness 长期记忆插件（V1）

把 `../../engine` 的 Python 引擎接进 harness：

- **S1 通道**：`ctx.systemPrompt.section({name:'memory:manual', order:150})` 放"怎么用记忆"的常量手册；
- **S2 通道**：`ctx.systemPrompt.section({name:'memory:stable', order:155})` 放 `always` 层稳定事实，
  直接长在 system prompt 里（任何压缩层都只折消息历史，碰不到它，见 ADR-019）；
- **T 通道**：`agent/pre-step` 里把"本次相关"插到**最后一条真人消息之前**，监听 `compaction/end`
  在压缩后整块重建，另有"隔 8 轮允许重发"兜底；
- **写**：`memory_*` 工具为主，`session/event` 的 `turn/end` 确定性抽取为辅；
  引擎默认 `explicit=false`（推断：置信度压 0.7、禁 `always`、禁自动取代），
  用户明说的事实要显式传 `explicit:true` 才可能自动取代旧条（deliberate opt-in）；
- **管理页**：`/dsh-attune-memory/`（列表 / 详情 / 时间轴 / 修改 / 忘记 / 恢复）。

## 文件

| 文件 | 作用 |
| --- | --- |
| `lib/engine.js` | 常驻 `python -m attune_memory.rpc` 子进程；一行一请求；超时、排队、崩溃后下次调用重启；全部 fail-open |
| `lib/message.js` | 构造注入消息（自造 + `deepFreeze`，必带 `source.kind='dsh-attune-memory'`）与 `isHumanMessage` |
| `lib/recall.js` | 注入主逻辑：守卫、按轮一次、内容哈希 + 轮次去重（`STALE_TURNS=8`）、压缩后强制重建、稳定块缓存 |
| `lib/tools.js` | `memory_remember/_preview/_search/_update/_supersede/_merge/_forget/_find_similar/_explain`；写失败三态 `WRITE_OK/WRITE_RETRYABLE/WRITE_FAILED`；`memory_forget` 只软删（无 `hard`） |
| `lib/extract.js` | `turn/end` 兜底抽取的 8 类句式（全部 `explicit:false`、`confidence 0.65`） |
| `lib/routes.js` | `/dsh-attune-memory` 前缀路由；每个 `/api/*` 先过 `connection.requestRejection` |
| `lib/ui.js` | 单文件管理页（无构建步骤） |
| `lib/index.js` | 组合：配置解析、三通道接线、工具注册、会话事件、生命周期清理 |
| `cordis.patch.yml` | bundle 补丁：`insert: [{id: dsh-attune-memory, name: dsh-attune-memory, config: {engineDir}}]` |
| `test/recall.test.mjs` | 去重/重发回归测试（8 组断言，`node test/recall.test.mjs`） |

## 配置

| 键 | 必填 | 默认 | 说明 |
| --- | --- | --- | --- |
| `engineDir` | 是 | — | 引擎目录（含 `attune_memory/` 包），也支持 `DSH_ATTUNE_MEMORY_ENGINE_DIR` |
| `dbPath` | 否 | `$DSH_HOME/attune-memory/memory.db` | SQLite 文件；自动建目录 |
| `python` | 否 | `python` | 解释器路径，也支持 `DSH_ATTUNE_MEMORY_PYTHON` |
| `engineModule` | 否 | `attune_memory.rpc` | Python 模块名。发布树的包目录叫 `attune_memory`、源码树里叫 `memory`，模块名就是「包目录名 + `.rpc`」；测试会自动探测，手工起引擎时用 `DSH_ATTUNE_MEMORY_ENGINE_MODULE` 覆盖 |
| `timeoutMs` | 否 | `5000` | 单次 RPC 超时 |
| `debug` | 否 | `false` | 打印每次 RPC |

## 安装（profile 里）

```powershell
$p = "$env:DSH_HOME\profiles\<profile>"
cmd /c mklink /J "$p\node_modules\dsh-attune-memory" "<本目录绝对路径>"
# 然后手工把 "dsh-attune-memory" 加进 package.json 的 dependencies 与 dsh.profile.bundles
```

注意两点：JSON 必须用 **UTF-8 无 BOM** 写回（`[System.IO.File]::WriteAllText` + `UTF8Encoding($false)`），
否则启动时报 `Unexpected token '\uFEFF'`；桌面 profile 由 Electron 独占，改完要重启应用。

## 可靠性边界：`__dedupe` 到底保证什么（三层，别混）

| 层 | 谁负责 | 保证什么 | 不保证什么 |
| --- | --- | --- | --- |
| 传输重试 | `engine.js` 的幂等键 + `rpc.handle` 的 `memory_request` 回放 | **同一次逻辑写入**被重发（超时/进程重启/客户端重试）时，第二次起原样返回第一次的结果，库里只有一条；失败不缓存，可安全重试 | 不保证"两次独立调用写同一件事"只落一条 |
| 语义去重 | `write.consider` 的「同槽位等价 → touch」 | 同一槽位里**归一化文本相同**或关键词 Jaccard ≥ 0.8 的条目不会重复落库，只加置信度 | 不同措辞的近义重复（"喜欢喝茶" vs "爱喝茶"）拦不住 |
| 冲突取代 | `write.consider` 的三档 + `explicit` | 显式且置信度 ≥ 0.8 才自动 supersede；0.6–0.8 并存并标注 `conflict_with`；< 0.6 丢弃并记账；推断**永不**自动取代 | 推断式误标（模型自称 explicit）无法验证 |

- 幂等键由 `engine.js` 在**调用时生成**（同一次调用的重试复用它）；不要跨调用复用同一个键，否则第二次真实写入会被当成重复而丢弃。
- 幂等回放保留 7 天，由 `tick` 清理（`memory_request` 表）。
- 兜底抽取每轮记四个计数（`memory_counter`）：`extract_attempt`（产生了几个候选）、
  `extract_written`（真写进几条）、`extract_blocked`（被门禁拦下几条）、`extract_failed`
  （出错几次），另有 `extract_skipped`（本轮模型已自行写入、整轮跳过）。
  `memory_probe` 与诊断面板直接读这些数字 —— 抽取再也不会"默默死掉"。
- 传输层只缓存**成功**结果：引擎明确拒绝（参数错、校验失败）不写缓存，所以改对了再调不受影响。

## 兼容边界：引擎与插件必须同版本（`describe` 是硬要求）

插件启动后第一次调用引擎时会先 `describe`（拿 ops 的 `params/required/writes`），再和本地写操作名单比对：

| 自检结果 | 写操作 | 读操作 | 含义 |
|---|---|---|---|
| `ok` | 放行 | 放行 | 契约一致（正常状态） |
| `mismatch` | **禁用**（`BAD_CONFIG`） | 放行 | 插件与引擎的写操作集合不同 → 会写重或写错 |
| `unavailable` | **禁用**（`BAD_CONFIG`） | 放行 | 引擎不支持 `describe`，契约无从校验 |

- **没有"退回本地名单继续写"这条路径**：校验失败就用未经校验的假设继续写，等于没有校验（第三方评审第 4 条指出的绕过点）。
- 因此 v0.2.0 起**引擎与插件必须同版本发布**：`describe` 是引擎的硬要求，旧引擎配新插件会进入 `unavailable`——读得出来，写不进去。升级时请一起换。
- `probe()` / `memory_probe` 会带出 `contract` 状态，排查"为什么写不进去"先看它。

## 三条硬约束（改代码前先看）

1. **注入消息必须带 `source.kind`** —— `dsh-agent-instructions` 会无保护地读它，缺了直接 TypeError，
   整轮在第一个 step 前就死，界面只显示"本轮运行失败"。
2. **消息是深冻结的** —— 不能改原对象，只能造新的。
3. **必须去重** —— 注入块会进历史、每个请求都重发；不去重就是滚雪球。
   但去重不能是"永不再注入"：听不见的压缩层（代理 ACP）会把块折掉而不通知，
   所以记的是轮次，`STALE_TURNS=8` 轮后允许重发；`compaction/end` 到达时立即清空。

## 排错

- `MISSING_CREDENTIAL`：验证用的 headless profile 默认关掉了桌面端凭据，用 `--patch` 叠加层打开。
- 工具全部消失但插件"加载成功"：多半是 `inject` 里漏了服务名（Cordis 只允许访问声明过的服务）。
- 中文乱码/`UnicodeEncodeError`：引擎侧已 `reconfigure(encoding='utf-8')`；宿主侧确保
  `PYTHONIOENCODING=utf-8`。
