# dsh-attune-memory — DeepSeek Harness 长期记忆插件（V1）

把 `../../engine` 的 Python 引擎接进 harness：

- **S1 通道**：`ctx.systemPrompt.section({name:'memory:manual', order:150})` 放"怎么用记忆"的常量手册；
- **S2 通道**：`ctx.systemPrompt.section({name:'memory:stable', order:155})` 放 `always` 层稳定事实，
  直接长在 system prompt 里（任何压缩层都只折消息历史，碰不到它，见 ADR-019）；
- **T 通道**：`agent/pre-step` 里把"本次相关"插到**最后一条真人消息之前**，监听 `compaction/end`
  在压缩后整块重建，另有"隔 8 轮允许重发"兜底；
- **写**：8 个 `memory_*` 工具为主，`session/event` 的 `turn/end` 确定性抽取为辅；
- **管理页**：`/dsh-attune-memory/`（列表 / 详情 / 时间轴 / 修改 / 忘记 / 恢复）。

## 文件

| 文件 | 作用 |
| --- | --- |
| `lib/engine.js` | 常驻 `python -m attune_memory.rpc` 子进程；一行一请求；超时、排队、崩溃后下次调用重启；全部 fail-open |
| `lib/message.js` | 构造注入消息（自造 + `deepFreeze`，必带 `source.kind='dsh-attune-memory'`）与 `isHumanMessage` |
| `lib/recall.js` | 注入主逻辑：守卫、按轮一次、内容哈希 + 轮次去重（`STALE_TURNS=8`）、压缩后强制重建、稳定块缓存 |
| `lib/tools.js` | `memory_remember/_search/_update/_supersede/_merge/_forget/_find_similar/_explain` |
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
