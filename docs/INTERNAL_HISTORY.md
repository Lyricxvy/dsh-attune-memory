# 内部版本的工作、修改与缺陷

> 这份文档记录**内部迭代阶段**（内部编号 1.0 → 1.1 → 1.2，对外统一为一个公开版本 0.1.0）：
> 每个阶段做了什么、为什么改、真实跑出来的缺陷与修法。
>
> 设计本身见 [DESIGN](DESIGN.md)；当前版本说明见 [RELEASE_NOTES_v0.1.0](RELEASE_NOTES_v0.1.0.md)。
>
> 版本映射：内部 1.0 = 首个可用形态；内部 1.1 = 九模块（契约测试/幂等/写三态/遥测）；
> 内部 1.2 = 三方审查 26 项收口；三者合并即公开的 0.1.0。

**目录**

1. 阶段工作与变更记录
2. 缺陷与修复（含 P8/P10/P11 真机问题、审查收口、遥测误报）


## 1. 阶段工作与变更记录

从 1.0 到 1.2 每个阶段的目标、改动点与验证证据。


> 说明：本文档记录内部开发阶段（对外统一为 v0.1.0）。第 11 节起的版本号已按 0.1.0 重新标注。

> 版本：1.1.0（2026-11-14）
> 基线：1.0.0（P0–P11 全部完成、GitHub 仓库已建）
> 一句话：**v1.0 解决"记得住"，v1.1 解决"知道自己有没有记住、记错了会不会说"。**

---

### 0. 一页速览

| 维度 | v1.0 | v1.1 |
|---|---|---|
| 可靠性 | 写成功/失败只有一个模糊报错；超时重试可能写两条 | 写请求带幂等键，超时重试**不会**写重复；工具返回 `WRITE_OK` / `WRITE_RETRYABLE` / `WRITE_FAILED` 三态 |
| 可观测性 | 静默失败是默认：注入了没进模型视野，没人知道 | 计数 + 读日志 + 丢弃日志 + `memory_probe`；管理页有"诊断"面板 |
| 冲突处理 | 互斥就自动取代（一条 0.65 的推断能覆盖 0.95 的事实） | 按"谁说的 + 多确定"分三档；推断永不自动取代；可疑冲突并存并标注 |
| `inject_count` | 建了列，但**从来没有地方自增**（诚实缺口） | 插件 splice 成功后回报 id，逐条自增 |
| 宿主假设 | 靠文档与人工检查 | 五项假设写成**可执行的契约测试**，宿主升级后跑一次即知有没有静默失效 |
| ACP 压缩盲区 | 只知道"看不见压缩"，没有任何判断手段 | `inject_faded_unexplained` 计数 + S1 自愈指令 + `memory_probe`；仍只是症状级证据 |

数字：引擎 **72** 个单测（v1.0 是 47）、插件 **20** 组 Node 断言（12 注入 + 3 引擎 + 2 面板 + 3 路由）、宿主契约 **5/5**、评估 **24/24**；schema 升到 **v3**（新增 3 张表，`memories` 列一个没动）；ADR 共 **24** 条（本版新增 019–024）。

---

### 1. 为什么要做 v1.1

v1.0 的验收是"代码层达标"，不是"系统层达标"。用户真实使用后，最贵的失效不是"记不住"，而是**记错了没人知道**。诊断书把这类问题归成 16 条，本质上只有五类：

1. **静默**：注入没进模型视野、写入被丢弃、进程崩了——全都没有出口。
2. **不可重试**：RPC 超时后，调用方不知道自己写没写成；重试要么写两条，要么干脆不写。
3. **一刀切**：冲突一律自动取代；失败一律同一句话；`important` 与 `always` 混在一起。
4. **只有字面**：检索必须字面撞上，领域约定（"讲数学题先给思路"）永远检索不到。
5. **没有证据**：出问题时只能靠回忆和猜。

v1.1 只做一件事：**把这些"不知道"变成"知道"**。不扩功能、不上向量、不改注入通道。

---

### 2. 变更总览（九个模块）

| # | 模块 | 问题 | 做法 | 完成判据 | 证据 |
|---|---|---|---|---|---|
| 1 | Host Contract Tests | 宿主行为（消息形状、注入位置、section 重算、压缩恢复）全靠文档和人工确认，宿主升级后可能静默失效 | `test/contract/` 五断言 H1–H5；探针插件在 attune 之前跑，记录 `decision.messages` 形状；解析多帧 zstd 会话日志 | 宿主升级后跑一次就知道有没有失效 | `run-contract.mjs` → **5/5**；`--offline` 2/2、2 秒 |
| 2 | RPC 可靠性 | 超时/进程崩溃后不知道写没写成，重试会写重复 | 幂等键 `dedupe` 放请求信封；新表 `memory_request` 回放**成功**响应；`mayHaveApplied` 标记；只对 TIMEOUT/ENGINE_DEAD 原样重试一次 | 同键重发只落一条；崩溃后能说出"可能已生效" | `test_rpc_reliability.py` 5 例 + `engine.test.mjs` 3 例 |
| 3 | 读写失败分流 | 所有失败都是 `[记忆操作失败] xxx` | 写三态 + 读 `READ_FAILED`；映射 `EngineError.code` | 模型能区分"重试有用/没用" | `lib/tools.js` 的 `writeFailure()` |
| 4 | `inject_count` | 建了列从不自增 | 选"实现自增"：插件 splice 后回报 `injected_ids`，引擎逐 id `bump`（先判存在） | 注入即计数，不虚高 | `TestTelemetry` + 真机计数 |
| 5 | SUPERSEDE 置信分级 | 互斥就自动取代，低置信推断能覆盖高置信事实 | 三档矩阵（≥0.8 / 0.6–0.8 / <0.6）+ 推断永不自动取代 + `metadata.conflict_with` | 误覆盖只在"用户明确且高置信"时发生 | `TestSupersedeConfidence` 7 例 |
| 6 | Telemetry | 无运行时可见性 | `memory_counter` 单调计数（11 项）+ `memctl telemetry` + `/api/stats` + 诊断面板 | 能看到"候选多少、命中多少、注入多少、错多少" | 真机计数 |
| 7 | 读日志 | 不知道某轮为什么没注入 | `search(debug={})` 记每条被丢弃的原因；`memory_observation(kind=read)` 留最近 200 条 | 能回答"这条为什么没进上下文" | `memctl reads` 实测三条 |
| 8 | 丢弃日志 | "我明明说过，为什么没记住"无法解释 | `value_gate` 六个拒绝分支全记（前 120 字 + 原因），14 天、可一键清 | 每条被拒候选都有理由 | `memctl discarded` |
| 9 | ACP 盲区 / 静默失效 | 代理层压缩 DSH 侧感知不到 | 注入带 `injectId`，下一轮回头核对 → `inject_visible` / `inject_faded_compaction` / `inject_faded_unexplained`；S1 手册加自愈指令；`memory_probe` 工具 | 能区分"库里没有"与"注入了但没进视野" | `recall.test.mjs` 12 组断言 |

---

### 3. 引擎侧细节

#### 3.1 schema 升到 v3（`memories` 列一个没动）

| 表 | 用途 | 关键列 |
|---|---|---|
| `memories` | 唯一的事实表（约束 A） | 仍是 24 列 |
| `memory_change` | append-only 变更流水 | `action / old_snapshot / new_snapshot / reason / actor / at` |
| `memory_counter` | 单调计数，不衰减不重置 | `name TEXT PRIMARY KEY, value INTEGER` |
| `memory_observation` | 诊断观测（`read` / `discarded`），只给人看 | `kind / subject / detail / at`，`idx_obs_kind(kind,at)` |
| `memory_request` | 幂等回放 | `key TEXT PRIMARY KEY, op, response, created_at`，7 天 TTL |
| `memory_meta` | 键值（schema 版本、维护时间） | `k, v` |

老库升级是**自动且无损**的：`init_schema` 全部 `CREATE ... IF NOT EXISTS`，启动即建新表、版本号写回 `memory_meta`。已用真实库副本验证：1 条原有记忆完好、5 张表齐全、`schema_version=3`。

#### 3.2 幂等（`engine/attune_memory/rpc.py`）

```
客户端 call(op,args) ──► args.__dedupe = <uuid>（写操作自动生成）
                       │
                       ▼
rpc.handle: 命中 memory_request → 直接回放 response + deduped:true
           未命中 → 执行 → 成功则 request_put（失败不缓存）
```

- 只缓存**成功**：失败必须允许原样重试。
- `tick` 顺带 `request_gc(keep_days=7)`。
- 超时且管道已写入时，`error.mayHaveApplied = true`，工具文案据此分成两句。

#### 3.3 冲突三档（`engine/attune_memory/write.py`）

| 条件 | 动作 | 理由 |
|---|---|---|
| 显式 + confidence ≥ 0.8 | 自动 SUPERSEDE | 用户刚刚明确纠正过 |
| 0.6 ≤ confidence < 0.8 | 并存 + `metadata.conflict_with` | 置信不足，冲突是 feature |
| confidence < 0.6 | 不写入 + `discarded` + 计数 | 宁可不记，别制造可疑记忆 |
| `explicit=False` | 永不自动取代 | 推断不许改事实 |
| 显式给 `supersede_id` | 照做 | 明确指令优先 |

#### 3.4 可观测性三件套（`ADR-024`）

- **计数**：`candidates_total`、`gate_rejected`、`op_{add,update,touch,supersede,merge}`、`search_candidates`、`search_hits`、`inject_items`、`inject_chars`、`rpc_errors`、`supersede_low_confidence`（外加插件侧 `inject_turns` / `inject_expected`）。
- **读日志**：`{candidates, dropped:[{id,why}], dropped_total}` → `memory_observation(kind='read')`，保留 200 条。
- **丢弃日志**：`value_gate(cand, store=...)` 六个分支统一走 `_discarded()`。

#### 3.5 数据出口（三处一致）

| 看什么 | CLI | HTTP |
|---|---|---|
| 计数 | `python -m attune_memory.memctl telemetry` | `GET /api/stats` |
| 最近检索 | `memctl reads --limit 10` | `GET /api/reads` |
| 被丢弃候选 | `memctl discarded --limit 10` / `--purge` | `GET /api/discarded` |

---

### 4. 宿主侧细节

#### 4.1 三通道没变

| 通道 | 位置 | 内容 | 抗压缩 |
|---|---|---|---|
| S1 | `systemPrompt.section({name:'memory:manual', order:150})` | "怎么用记忆"手册（本版加自愈指令） | 压缩只动消息历史 |
| S2 | `systemPrompt.section({name:'memory:stable', order:155})` | `always` 稳定事实 | 在 system prompt 里，任何压缩层碰不到 |
| T | `agent/pre-step` 的 `decision.messages` | 本次相关（contextual/state/domain） | 监听压缩事件重建 + 隔 8 轮兜底重发 |

#### 4.2 写三态（`lib/tools.js`）

```
WRITE_OK …          ← 成功
WRITE_RETRYABLE …   ← TIMEOUT / ENGINE_DEAD / ENGINE_ERROR / CLOSED（可原样重试一次）
WRITE_FAILED …      ← ENGINE_REJECTED / BAD_CONFIG / WRITE_FAILED（重试不会变好）
READ_FAILED …       ← 读失败，一律 fail-open，不影响对话
```

#### 4.3 静默失效检测（`lib/recall.js` + `memory_probe`）

- 每条注入消息带 `injectId`，来源里写 `form: 'recall' | 'reinject'` 与 `memoryIds`。
- 下一轮 `pre-step` 先做**核对**（早于去重）：消息还在 → `inject_visible`；不在了 → 若这段期间收到过压缩事件记 `inject_faded_compaction`，否则 `inject_faded_unexplained`。
- `inject_faded_unexplained > 0` 通常意味着请求被某个代理层重写过（ACP 那条路）。
- S1 手册末尾：看不到记忆段时**先 `memory_search`**，不要把"没命中"当"没记忆"。

#### 4.4 契约测试（`test/contract/`）

| 断言 | 守的是什么宿主行为 |
|---|---|
| H1 | 本轮真实用户消息在 `pre-step` 里可见 |
| H2 | `systemPrompt.section` 的 `text()` 每步重算（新写的事实下一轮就出现） |
| H3 | 注入消息进入最终请求，且排在真人消息**之前** |
| H4 | 压缩发生后，`form=reinject` 的注入重新出现 |
| H5 | 注入消息结构合法（`role/content/source` 齐全，不炸 pre-step） |

跑法：`node plugin/dsh-attune-memory/test/contract/run-contract.mjs`（真模型，约 40 秒）；`--offline` 只做结构断言，2 秒，可进构建门禁。

---

### 5. 五项决策（ADR）

| ADR | 决定 | 主要否决 |
|---|---|---|
| 020 | 用可执行契约测试守宿主假设，不用 mock | mock 只能证明"我以为的契约"；只写文档会漂移 |
| 021 | 遥测只观测、不改行为；ACP 用"下一轮回头核对"取证 | 用消失率自动加倍注入（正反馈）、只写日志（无法对齐审计）、断言式报警（ACP 不可控） |
| 022 | 幂等键放请求信封、引擎回放成功响应 | 调用方自带 id（模型会忘）、内容哈希（合法重复被吞）、不做幂等 |
| 023 | 冲突三档 0.8 / 0.6，推断永不自动取代 | 一律取代、一律并存、让模型自己选 |
| 024 | 计数 + 读日志 + 丢弃日志，全部只读 | 只写日志文件、全量记录候选与分数、完全不记丢弃 |

ADR-019（稳定事实从 `context` 搬到 `section`）是 v1.1 的开端：ACP 压缩不走会话事件，`retained=null` 的自动恢复在这台机器上永远不会触发。

---

### 6. 验证证据

#### 6.1 测试

| 层 | 命令 | 结果 |
|---|---|---|
| 引擎单测 | `cd engine && python -m unittest discover -s tests -q` | **Ran 72 tests / OK** |
| RPC 可靠性 | `python -m unittest tests.test_rpc_reliability` | 5/5（幂等、跨进程、失败不缓存、GC、并发不串写） |
| 插件：注入与核对 | `node plugin/dsh-attune-memory/test/recall.test.mjs` | 12/12 |
| 插件：RPC 客户端 | `node plugin/dsh-attune-memory/test/engine.test.mjs` | 3/3（超时 `mayHaveApplied`、幂等、自杀重启） |
| 评估套件 | `cd engine && python -m eval.run_eval` | **24/24**（167 ms） |
| 宿主契约 | `node plugin/dsh-attune-memory/test/contract/run-contract.mjs` | **5/5** |
| 契约离线子集 | 同上 `--offline` | 2/2（2 秒） |

#### 6.2 真机（重启后核验）

| 项 | 结果 |
|---|---|
| S1 手册（含自愈指令） | 出现在 system prompt，841 字符 |
| `memory_probe` | 可用：库 1 条 active、S1 841 字符、S2 0 字符、记账 0/0/0、最近观测 1 条 `read` |
| 库迁移 | `schema_version=3`，`memory_counter` / `memory_observation` / `memory_request` 已建，原有 1 条记忆完好 |
| 计数与观测 | `search_candidates=1`，观测里有 `{"dropped":[],"candidates":1,"hits":0,...}` |
| 管理页 / 栅栏 | `/dsh-attune-memory/` → 200 / 20214 字节；`/api/reads` 无凭据 → 401 |
| 读链路 | `memory_search("可视化界面")` → 命中 `m-mwft6kub-1q`，score 0.305 |

#### 6.3 遥测样本（契约测试库）

```
candidates_total 2 / inject_items 8 / inject_chars 360
inject_turns 3 / inject_expected 3 / search_candidates 5 / search_hits 3
观测 3 条 read：{"candidates":2,"hits":1,"injected":1,"groups":{"profile":1},"chars":75}
```

---

#### 6.4 本轮同步修掉的两个面板 bug

打包前跑面板链路测试时发现两处会直接让"保存"按钮失败的缺陷，属于真实用户会碰到的：

1. **`subtype` 被路由层放了进来，引擎却拒收**：面板允许改子类型，`routes.js` 把它塞进 `fields`，而 `write.update()`
   的白名单没有 `subtype` → 点了保存返回 400「update 不允许改这些字段」。修法：`subtype` 纳入白名单，
   但**必须属于该记忆 type 的子类型集合**；`type` / `subject_type` 仍然禁止（那变了就是另一条事实）。
2. **关键词数组直接绑进 SQLite**：面板把关键词拆成 `["李四","考研","2027"]`，引擎把 list 原样绑参 →
   `Error binding parameter 6: type 'list' is not supported`。修法：写层统一 `join_keywords()` 归一化成空格串。

两条都补了回归测试（`TestUpdateEdgeCases` 4 例），所以引擎单测从 68 涨到 **72**。

### 7. 升级与回滚

**升级**
1. 用 `build-release.ps1` 重建发布树（本版已重建：94 个文件 / 1.4 MB，`docs/` 17 篇）。
2. 重启应用（插件在启动时加载；库在首次调用时自动迁移到 v3）。
3. 自检：`memctl telemetry` 有输出、管理页出现"诊断"、`memory_probe` 返回正常。

**回滚**
- 代码：`git -C release/dsh-attune-memory checkout v1.0.0`（或上一个提交）后重启。
- 数据库：v3 只**新增**表，v1.0 的代码读 v3 库不会报错（它不查新表）；但新表数据会留在库里，需要的话手工 `DROP`。
- 兜底：迁移前建议 `Copy-Item memory.db memory.db.bak`。

---

### 8. 已知局限（诚实清单）

1. **`inject_faded_unexplained` 只是症状级证据**，不能证明一定是 ACP；报告里必须保持这个措辞（ADR-021/024）。
2. **headless 跨进程核对不到**：`#lastInject` 在内存里，一行一进程；真机长会话才有数据，单测覆盖逻辑。
3. **`inject_count` 依赖插件回执**：不经过插件 splice 的读取（如 `memory_search`）只记 `use_count`。
4. **丢弃日志保存用户原话前 120 字**（14 天、可 purge）——对"不存聊天记录"的有意识例外，已写进 INSTALL 隐私节。
5. **0.6 以下的真实纠正会被丢弃**，只能从丢弃日志发现；缓解是手册要求纠正时用 `memory_supersede` 显式指定旧 id。
6. **契约测试要真调模型**，不是 CI 里的离线单测；`--offline` 只覆盖结构。
7. **检索仍是词法 + 结构化过滤**，领域约定靠 `scope=domain` 精确匹配补位，不做语义相似（V2）。

---

### 9. 数字盘点

| 项 | v1.0 | v1.1 |
|---|---|---|
| 引擎 Python | 2 4xx 行 | **3 286 行** |
| 插件 JS | ~1 6xx 行 | **2 916 行**（含 client 面板） |
| 测试 | 47 引擎 + 8 插件 | **72 引擎 + 20 插件 + 5 契约** |
| schema | v1（3 表） | **v3（6 表）** |
| ADR | 19 条 | **24 条** |
| 文档 | 16 篇 | **17 篇** |
| 发布包 | 58 项 / 288 KB | **66 项 / 325 KB** |

---

### 10. 下一步

- **短期**：按 Pro 的三关验收——真实使用 1–2 周，盯两个信号：①长对话触发压缩后是否"失忆"（看 `inject_faded_*` 与 `memory_probe`）；②是否要反复提醒模型才用记忆（看 `memory_remember` 调用频次与丢弃日志）。
- **中期（V2 边界内）**：中文 Bigram/FTS5 提升召回、类型化半衰期与自动遗忘、冲突发现可视化、异步 Consolidation。
- **不做**：Embedding 唯一方案、自动人格推断、不可逆归并、按工作区拆库、存全量聊天记录。

---

### 11. 0.1.0 更新（三方审查 26 项）

#### 11.1 第一批：安全与文档（S1/P1/S2/X2）

| 项 | 状态 | 落点 |
|---|---|---|
| S1 管理 API fail-open | **修复** | `routes.js` fail-closed（无信任栅栏 → 503），ADR-025，两条 503 断言 |
| P1 数字不一致 | **修复** | README 改 CI badge；INSTALL/engine README 写"以 CI 为准"；标题旧名修正 |
| S2 首屏 Privacy | **修复** | README 顶部隐私段 + 安装对象声明 |
| X2 幂等 TTL | **文档化** | README「可靠性设计（三条）」：`memory_request` 保留 7 天、`tick` 清理 |

#### 11.2 第二批：写入质量与检索结构（W1/W6/W2/W3/W5/R4）

| 项 | 状态 | 落点 |
|---|---|---|
| W1 类型判据入不变量 | **修复** | `valid_until` 仅 state；`always` 白名单；`type_hint` 留痕，ADR-026 |
| W6 always 收紧 | **修复** | 只允许 `profile/{identity,interaction,constraint}` 且 `scope=global` |
| W2 候选 ≠ 记忆 | **修复** | `preview()/memory_preview` 只算不写；抽取通道也改走 preview→add，ADR-032 |
| W3 闸门句式化 | **修复** | 句首判断 + 问号硬拦 + 显式候选跳过词表，ADR-027 |
| W5 抽取 interaction 收窄 | **修复** | 推断的 global interaction 降 `on_demand`；抽取器跟随 domain |
| R4 检索三阶段 | **修复** | `stage_eligibility/ranking/budget` + `debug.stages` |

#### 11.3 第三批：审查收口（W7/W4/O2/M1/M2/R2/M3/O3）

| 项 | 状态 | 落点 / 证据 |
|---|---|---|
| W7 数值冲突按属性 | **修复** | `key_values()` 返回 `{属性: 取值}`，只比同名属性；`TestKeyValuePrecision` 3 例，ADR-028 |
| W4 主体双侧拦 | **修复** | 第一人称→必须 user；第三方→不得挂 user 且必须有 `subject_name`；4 例，ADR-029 |
| O2 自愈指令频控 | **修复** | 手册拆 `MANUAL_BASE`+`SELF_HEAL`，每 5 轮一次且空库不放；ADR-030 |
| M1 维护扫描 | **修复** | `Memory.scan()` 只读产出重复/矛盾/同槽多位/过期/不变式 + `memctl scan` + RPC `scan`；ADR-031 |
| M2 跨轮回顾 | **修复** | 最近 5 句真人原话滚动缓冲，逐句抽不到时用最近三句拼接再抽；ADR-032 |
| R2 中文二元组召回 | **修复（词法级）** | `bigrams()` + 覆盖率兜底；拉丁词改词边界匹配；ADR-033 |
| M3 Formation | **部分** | Assembly 完整；Formation 只到"共享二元组的主题簇候选"（`scan().formations`） |
| O3 A/B 证据 | **小样本已跑** | 见 §12.1；仅方向性，混入工作区文件干扰 |

#### 11.4 交付缺陷 #12（真实库跑出来的）

`memory_probe` 报"注入 19 次、可见 0 次、原因不明消失 8 次"——查实是核对位置错了：
`decision.messages` 只有本步新消息，上一轮注入在**会话历史**里。改为以
`session.deriveMessages()` 为主证据后误报归零，ADR-034。只影响遥测，不影响注入本身。

---

### 12. 0.1.0 收尾记录

#### 12.1 O3 小样本 A/B（2026-11-14）

- 方法：`contract` profile，同一句"明天该复习什么？两句话就够。"；A 侧开记忆（库里只种两条：`用户叫张三，正在备考考研数学一`、`用户备考 2027 考研数学一，考试在 12 月`），B 侧 `dsh-attune-memory disabled: true`。
- 结果：A 答"距离 12 月初试只剩一个多月……重点盯 1^∞ 的 e^{v·ln u} 还原"，B 完全没提考试时间。
  "12 月初试"只存在于库里，工作区笔记只有"极限七种未定式"，**可判定为记忆注入带来的差异**。
- 混杂因素：B 侧仍能读到工作区文件（提到 833 传热学、W2 专题计划），所以这一轮只能证明"注入的事实在回答里出现了"，
  不能证明"没有记忆时回答质量更差"。要得到可用 A/B，需要在**干净工作区**跑多轮。
- 结论：方向对，样本不足；真实使用 1–2 周后再统计（指标见 `V2_V3_RESERVED.md` O3）。

#### 12.2 真实库上的第一次维护（2026-11-14）

`memctl scan` 在用户真实库上首跑即抓到三类问题，全部按"可逆优先"处理：
1. 同槽位两条 active 的 `state/goal`（备考 vs 已完成的可视化任务）→ 后者改记 `state/activity` + `on_demand`；
2. 一次性任务指令被当成 `profile/interaction` → 软删除（可恢复）；
3. `superseded_by` 指向已删条目 → 整条任务链归档。
处理后 `stats().problems == []`，5 条 active。

#### 12.3 数字

引擎单测 **107 通过**；插件断言 recall 12 组 / engine 3 组 / extract 4 组 / routes.stub 11 项 / panel 1 组；
eval 24/24；ADR **34 条**；发布树 68 项。



## 2. 缺陷与修复

只有真跑起来才暴露的缺陷，以及每个缺陷的根因与修法。


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

### 1. 同一条自述被写两遍

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

### 2. `valid_until` 写不进去

**现场**：想让"这份备考计划已作废"立刻过期，`add` 报
`add 不接受这些参数：['valid_until']；可用：[...]`。

**根因**：引擎的 `Candidate` 有 `valid_until` 字段、`check_row` 也认它，但 RPC 的参数白名单漏了；
而且没有任何格式校验——`valid_until` 是拿去跟 `now` 比字符串的，写错一次，这条记忆要么永不过期，
要么立刻消失。

**修复**：`rpc._SPEC` 的 `add` / `supersede` 加 `valid_until`；`write.validate()` 校验它是合法 ISO，
且必须晚于 `valid_from`（`valid_until(...) 必须晚于 valid_from(...)`）。

**验证**：`Ran 47 tests … OK` 里的 `TestValidUntil`（三条：可写入过去区间 / 非 ISO 被拒 / 时间倒置被拒）。

### 3. 过时记忆不是降权，而是消失

**现场**：P7 第一次跑测试，`importance=normal` 的记忆怎么都检索不出来。

**根因**：命中闸门卡的是**六因子乘积**（`Final >= MIN_SCORE`）。`importance=normal` 只有 0.5，
完全相关 + 高置信 + 新鲜的条目上限也只有 ~0.3，门槛 0.30 正好把"正常重要性"的记忆全砍了；
过期 state 更惨（Freshness 触底 0.2、冲突 0.5），分数 ≈0.05，永远出不来——设计里的"过时降权"
在实现里变成了"过时消失"。

**修复**：闸门改卡**命中质量** `Relevance × Confidence × ContextMatch >= 0.15`，
重要性、新鲜度、冲突惩罚只参与排序。注释里写清了为什么：重要性只该调排序，不该决定生死。

**验证**：`Ran 47 tests … OK`；过期 state 出现在结果里并带 `⚠` 说明。

### 4. `contextual` 的 profile 记忆永不注入

**现场**：库里有一条"张三做数学一极限题总卡在变形方向"（`profile/skill`，`contextual`），
`search` 给它 0.437 分，但真实会话的注入块里从来没有它。

**根因**：`read.context_block` 只把命中分进 `state`/`domain`/`episodic` 三桶，
`profile` 那一支只有一行注释"按普通条目处理"，没有代码——bucket 不接，就永远不渲染。

**修复**：新增 `profile_items` 分桶，`take("profile", …)` 排在 state 之后、domain 之前；
`const.BUDGET` 加 `profile_max_items: 2` / `profile_max_chars: 160`
（C 通道装不下的 profile 才落到这里，这类"卡在哪"比领域约定更值钱）。

**验证**：`test_contextual_profile_reaches_context_channel`；真机会话
`seq=8 type=user/message source=dsh-attune-memory/recall` 正文含"总是想不到变形方向"。

### 5. 领域交互约定在词法检索里隐形

**现场**：P10 的 E 场景挂了一格——问"这道极限题怎么下手"，`scope=domain, domain_key=study.math`
的约定"讲数学题时先讲思路再给步骤"没进注入块。

**根因**：两句话没有一个共同词。V1 是词法检索，relevance=0 → 直接被闸门挡掉。
而这恰恰是 Interaction Model 最该起作用的地方：**约定是"在这个领域里怎么帮我"，不是"这句话里有什么词"**。

**修复**：`read._domain_rules()`——领域被明确识别时，按 domain 精确取该域下
`scope=domain` 的 `profile/interaction` 记忆（`DOMAIN_RULE_MIN_CONFIDENCE=0.8`），
作为补位追加，不占直接命中的名额，无领域时不补。**只在读管线里做，不动 `search()` 的语义**：
模型主动搜记忆时，词法命中仍是唯一依据。

**验证**：E 场景 6/6 通过；`TestDomainRules` 两条（有域命中 / 无域不命中）。

### 6. 压缩后强制重建提前 return

**现场**：压缩发生的那一轮之后，注入块是空的——本该出现的稳定块也没了。

**根因**：`inject()` 里 `if (!text) return decision` 在"压缩后但本轮无关键词命中"时直接放弃，
而强制重建的意义正是"不管有没有命中，都要把稳定事实补回去"。

**修复**：改成 `if (!text && !(force && this.#stableText))` 才返回；
`body` 在强制重建且无命中时就是稳定块本身；并把稳定块缓存同步提到判断之前。

**验证**：真实会话 `seq=85 kind=dsh-attune-memory form=reinject afterCompaction=true`，
而那一轮用户问的是"先歇会儿，帮我列三条明天复习的建议"（与记忆无关）——仍然重建了。

### 7. 非法枚举把 SQLite 的 CHECK 报错抛给用户

**现场**：`importance="super-high"` 一路走到 SQLite，用户看到
`CHECK constraint failed: importance IN ('key','important','normal','low')`。

**根因**：`write.update()` 没有前置校验，只有 DB 层 CHECK 兜底。

**修复**：`update()` 先校验 `importance`/`injection_policy`/`scope`，报
`importance 只能是 key/important/normal/low 之一，收到 'super-high'`；`confidence` 限 0–1；
`content` 长度 4–300。既不放裸表结构，也让模型能自己改对。

**验证**：`.verify/p9-routes-test.mjs` 第 6 项。

### 8. Windows 下 RPC 中文整条链路崩

**现场**：`UnicodeEncodeError: 'utf-8' codec can't encode character '\udca6'`，所有记忆读写全挂。

**根因**：Windows 上 `sys.stdin/stdout` 默认跟控制台代码页（cp936）走，而 Node 按 UTF-8 读写管道，
中文请求被解成孤立代理字符。

**修复**：`rpc.main()` 开头对 stdin/stdout 各调一次
`stream.reconfigure(encoding="utf-8", errors="replace")`（带 `AttributeError/ValueError` 兜底）。

**验证**：`engine/tests/rpc_smoke.js`（Node 侧真路径，6 条请求含中文）exit 0。

### 9. 时效噪声词表太窄

**现场**：P10 的 A 场景"刚吃完午饭，有点困"被 ADD 进库。

**根因**：`TRANSIENT_PATTERNS` 里只有"我有点困"，没有"有点困"。

**修复**：补一过性身体/情绪说法（有点困/有点累/有点饿/有点烦/刚吃完/正在吃/待会儿…），
**只收"明显是此刻"的说法**，不收"在忙项目"这种可能是长期状态的说法。

**验证**：A 场景 4/4 通过。

### 10. 备考被写成 `profile/identity` 而不是 `state`

**现场**：V1.1 复验的第一轮（"我叫张三，正在备考考研数学一"），模型写的是
`type=profile, subtype=identity`；第二轮"我改主意了，不考数学一了，改考计算机 408"，
模型正确地做了 supersede，但整条链路落在 identity 槽位上。

**根因**：手册只说了"写什么"，没说"放哪一类"。`state` 是**有起止的计划/活动**，
`profile` 是**他是谁/他喜欢什么/他要你怎么回答**；"正在备考"明显是前者，
但模型手里没有这条判据，于是挑了更眼熟的 `profile`。

**修复**：`read.MANUAL` 补一段类型选择指引（"有起止的计划/活动用 `state`"……
"事实变了用 `memory_supersede` 换掉旧条目，不要新加一条并列的"）。

**验证**：见下面的真机复验——取代链完整、交互偏好未被误改、`problems: []`。

### 11. 装进真实桌面后，压缩换人做了（稳定层搬进 system prompt）

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

### 12. 真机复验（V1.1 收尾）

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

### 这轮调优**没有**做的事（以及为什么）

- **没有加自动遗忘**。样例只有几十条，"该忘哪些"缺乏证据；误删不可逆，V2 再说。
- **没有上 Embedding**。第 5 条的修法是"按领域补位"，比换检索范式便宜得多；语义检索留给 V2。
- **没有把下限抬到 `important`**。那等于承认"normal 的记忆不配被检索到"，与哲学 ② 相悖。
- **没有做人工审核队列**。写入管线已经有 IGNORE/降级/supersede 三档，再加一道人工确认
  只会让用户放弃记录。

这些点到为止的判断都写进了 `V2_V3_RESERVED.md`，V2 的第一件事是把它们重新评估一遍。

