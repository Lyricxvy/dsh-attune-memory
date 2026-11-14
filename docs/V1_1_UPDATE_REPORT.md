# dsh-attune-memory 内部阶段 1.1 更新报告

> 说明：本文档记录内部开发阶段（对外统一为 v0.1.0）。第 11 节起的版本号已按 0.1.0 重新标注。

> 版本：1.1.0（2026-11-14）
> 基线：1.0.0（P0–P11 全部完成、GitHub 仓库已建）
> 一句话：**v1.0 解决"记得住"，v1.1 解决"知道自己有没有记住、记错了会不会说"。**

---

## 0. 一页速览

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

## 1. 为什么要做 v1.1

v1.0 的验收是"代码层达标"，不是"系统层达标"。用户真实使用后，最贵的失效不是"记不住"，而是**记错了没人知道**。诊断书把这类问题归成 16 条，本质上只有五类：

1. **静默**：注入没进模型视野、写入被丢弃、进程崩了——全都没有出口。
2. **不可重试**：RPC 超时后，调用方不知道自己写没写成；重试要么写两条，要么干脆不写。
3. **一刀切**：冲突一律自动取代；失败一律同一句话；`important` 与 `always` 混在一起。
4. **只有字面**：检索必须字面撞上，领域约定（"讲数学题先给思路"）永远检索不到。
5. **没有证据**：出问题时只能靠回忆和猜。

v1.1 只做一件事：**把这些"不知道"变成"知道"**。不扩功能、不上向量、不改注入通道。

---

## 2. 变更总览（九个模块）

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

## 3. 引擎侧细节

### 3.1 schema 升到 v3（`memories` 列一个没动）

| 表 | 用途 | 关键列 |
|---|---|---|
| `memories` | 唯一的事实表（约束 A） | 仍是 24 列 |
| `memory_change` | append-only 变更流水 | `action / old_snapshot / new_snapshot / reason / actor / at` |
| `memory_counter` | 单调计数，不衰减不重置 | `name TEXT PRIMARY KEY, value INTEGER` |
| `memory_observation` | 诊断观测（`read` / `discarded`），只给人看 | `kind / subject / detail / at`，`idx_obs_kind(kind,at)` |
| `memory_request` | 幂等回放 | `key TEXT PRIMARY KEY, op, response, created_at`，7 天 TTL |
| `memory_meta` | 键值（schema 版本、维护时间） | `k, v` |

老库升级是**自动且无损**的：`init_schema` 全部 `CREATE ... IF NOT EXISTS`，启动即建新表、版本号写回 `memory_meta`。已用真实库副本验证：1 条原有记忆完好、5 张表齐全、`schema_version=3`。

### 3.2 幂等（`engine/attune_memory/rpc.py`）

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

### 3.3 冲突三档（`engine/attune_memory/write.py`）

| 条件 | 动作 | 理由 |
|---|---|---|
| 显式 + confidence ≥ 0.8 | 自动 SUPERSEDE | 用户刚刚明确纠正过 |
| 0.6 ≤ confidence < 0.8 | 并存 + `metadata.conflict_with` | 置信不足，冲突是 feature |
| confidence < 0.6 | 不写入 + `discarded` + 计数 | 宁可不记，别制造可疑记忆 |
| `explicit=False` | 永不自动取代 | 推断不许改事实 |
| 显式给 `supersede_id` | 照做 | 明确指令优先 |

### 3.4 可观测性三件套（`ADR-024`）

- **计数**：`candidates_total`、`gate_rejected`、`op_{add,update,touch,supersede,merge}`、`search_candidates`、`search_hits`、`inject_items`、`inject_chars`、`rpc_errors`、`supersede_low_confidence`（外加插件侧 `inject_turns` / `inject_expected`）。
- **读日志**：`{candidates, dropped:[{id,why}], dropped_total}` → `memory_observation(kind='read')`，保留 200 条。
- **丢弃日志**：`value_gate(cand, store=...)` 六个分支统一走 `_discarded()`。

### 3.5 数据出口（三处一致）

| 看什么 | CLI | HTTP |
|---|---|---|
| 计数 | `python -m attune_memory.memctl telemetry` | `GET /api/stats` |
| 最近检索 | `memctl reads --limit 10` | `GET /api/reads` |
| 被丢弃候选 | `memctl discarded --limit 10` / `--purge` | `GET /api/discarded` |

---

## 4. 宿主侧细节

### 4.1 三通道没变

| 通道 | 位置 | 内容 | 抗压缩 |
|---|---|---|---|
| S1 | `systemPrompt.section({name:'memory:manual', order:150})` | "怎么用记忆"手册（本版加自愈指令） | 压缩只动消息历史 |
| S2 | `systemPrompt.section({name:'memory:stable', order:155})` | `always` 稳定事实 | 在 system prompt 里，任何压缩层碰不到 |
| T | `agent/pre-step` 的 `decision.messages` | 本次相关（contextual/state/domain） | 监听压缩事件重建 + 隔 8 轮兜底重发 |

### 4.2 写三态（`lib/tools.js`）

```
WRITE_OK …          ← 成功
WRITE_RETRYABLE …   ← TIMEOUT / ENGINE_DEAD / ENGINE_ERROR / CLOSED（可原样重试一次）
WRITE_FAILED …      ← ENGINE_REJECTED / BAD_CONFIG / WRITE_FAILED（重试不会变好）
READ_FAILED …       ← 读失败，一律 fail-open，不影响对话
```

### 4.3 静默失效检测（`lib/recall.js` + `memory_probe`）

- 每条注入消息带 `injectId`，来源里写 `form: 'recall' | 'reinject'` 与 `memoryIds`。
- 下一轮 `pre-step` 先做**核对**（早于去重）：消息还在 → `inject_visible`；不在了 → 若这段期间收到过压缩事件记 `inject_faded_compaction`，否则 `inject_faded_unexplained`。
- `inject_faded_unexplained > 0` 通常意味着请求被某个代理层重写过（ACP 那条路）。
- S1 手册末尾：看不到记忆段时**先 `memory_search`**，不要把"没命中"当"没记忆"。

### 4.4 契约测试（`test/contract/`）

| 断言 | 守的是什么宿主行为 |
|---|---|
| H1 | 本轮真实用户消息在 `pre-step` 里可见 |
| H2 | `systemPrompt.section` 的 `text()` 每步重算（新写的事实下一轮就出现） |
| H3 | 注入消息进入最终请求，且排在真人消息**之前** |
| H4 | 压缩发生后，`form=reinject` 的注入重新出现 |
| H5 | 注入消息结构合法（`role/content/source` 齐全，不炸 pre-step） |

跑法：`node plugin/dsh-attune-memory/test/contract/run-contract.mjs`（真模型，约 40 秒）；`--offline` 只做结构断言，2 秒，可进构建门禁。

---

## 5. 五项决策（ADR）

| ADR | 决定 | 主要否决 |
|---|---|---|
| 020 | 用可执行契约测试守宿主假设，不用 mock | mock 只能证明"我以为的契约"；只写文档会漂移 |
| 021 | 遥测只观测、不改行为；ACP 用"下一轮回头核对"取证 | 用消失率自动加倍注入（正反馈）、只写日志（无法对齐审计）、断言式报警（ACP 不可控） |
| 022 | 幂等键放请求信封、引擎回放成功响应 | 调用方自带 id（模型会忘）、内容哈希（合法重复被吞）、不做幂等 |
| 023 | 冲突三档 0.8 / 0.6，推断永不自动取代 | 一律取代、一律并存、让模型自己选 |
| 024 | 计数 + 读日志 + 丢弃日志，全部只读 | 只写日志文件、全量记录候选与分数、完全不记丢弃 |

ADR-019（稳定事实从 `context` 搬到 `section`）是 v1.1 的开端：ACP 压缩不走会话事件，`retained=null` 的自动恢复在这台机器上永远不会触发。

---

## 6. 验证证据

### 6.1 测试

| 层 | 命令 | 结果 |
|---|---|---|
| 引擎单测 | `cd engine && python -m unittest discover -s tests -q` | **Ran 72 tests / OK** |
| RPC 可靠性 | `python -m unittest tests.test_rpc_reliability` | 5/5（幂等、跨进程、失败不缓存、GC、并发不串写） |
| 插件：注入与核对 | `node plugin/dsh-attune-memory/test/recall.test.mjs` | 12/12 |
| 插件：RPC 客户端 | `node plugin/dsh-attune-memory/test/engine.test.mjs` | 3/3（超时 `mayHaveApplied`、幂等、自杀重启） |
| 评估套件 | `cd engine && python -m eval.run_eval` | **24/24**（167 ms） |
| 宿主契约 | `node plugin/dsh-attune-memory/test/contract/run-contract.mjs` | **5/5** |
| 契约离线子集 | 同上 `--offline` | 2/2（2 秒） |

### 6.2 真机（重启后核验）

| 项 | 结果 |
|---|---|
| S1 手册（含自愈指令） | 出现在 system prompt，841 字符 |
| `memory_probe` | 可用：库 1 条 active、S1 841 字符、S2 0 字符、记账 0/0/0、最近观测 1 条 `read` |
| 库迁移 | `schema_version=3`，`memory_counter` / `memory_observation` / `memory_request` 已建，原有 1 条记忆完好 |
| 计数与观测 | `search_candidates=1`，观测里有 `{"dropped":[],"candidates":1,"hits":0,...}` |
| 管理页 / 栅栏 | `/dsh-attune-memory/` → 200 / 20214 字节；`/api/reads` 无凭据 → 401 |
| 读链路 | `memory_search("可视化界面")` → 命中 `m-mwft6kub-1q`，score 0.305 |

### 6.3 遥测样本（契约测试库）

```
candidates_total 2 / inject_items 8 / inject_chars 360
inject_turns 3 / inject_expected 3 / search_candidates 5 / search_hits 3
观测 3 条 read：{"candidates":2,"hits":1,"injected":1,"groups":{"profile":1},"chars":75}
```

---

### 6.4 本轮同步修掉的两个面板 bug

打包前跑面板链路测试时发现两处会直接让"保存"按钮失败的缺陷，属于真实用户会碰到的：

1. **`subtype` 被路由层放了进来，引擎却拒收**：面板允许改子类型，`routes.js` 把它塞进 `fields`，而 `write.update()`
   的白名单没有 `subtype` → 点了保存返回 400「update 不允许改这些字段」。修法：`subtype` 纳入白名单，
   但**必须属于该记忆 type 的子类型集合**；`type` / `subject_type` 仍然禁止（那变了就是另一条事实）。
2. **关键词数组直接绑进 SQLite**：面板把关键词拆成 `["李四","考研","2027"]`，引擎把 list 原样绑参 →
   `Error binding parameter 6: type 'list' is not supported`。修法：写层统一 `join_keywords()` 归一化成空格串。

两条都补了回归测试（`TestUpdateEdgeCases` 4 例），所以引擎单测从 68 涨到 **72**。

## 7. 升级与回滚

**升级**
1. 用 `build-release.ps1` 重建发布树（本版已重建：94 个文件 / 1.4 MB，`docs/` 17 篇）。
2. 重启应用（插件在启动时加载；库在首次调用时自动迁移到 v3）。
3. 自检：`memctl telemetry` 有输出、管理页出现"诊断"、`memory_probe` 返回正常。

**回滚**
- 代码：`git -C release/dsh-attune-memory checkout v1.0.0`（或上一个提交）后重启。
- 数据库：v3 只**新增**表，v1.0 的代码读 v3 库不会报错（它不查新表）；但新表数据会留在库里，需要的话手工 `DROP`。
- 兜底：迁移前建议 `Copy-Item memory.db memory.db.bak`。

---

## 8. 已知局限（诚实清单）

1. **`inject_faded_unexplained` 只是症状级证据**，不能证明一定是 ACP；报告里必须保持这个措辞（ADR-021/024）。
2. **headless 跨进程核对不到**：`#lastInject` 在内存里，一行一进程；真机长会话才有数据，单测覆盖逻辑。
3. **`inject_count` 依赖插件回执**：不经过插件 splice 的读取（如 `memory_search`）只记 `use_count`。
4. **丢弃日志保存用户原话前 120 字**（14 天、可 purge）——对"不存聊天记录"的有意识例外，已写进 INSTALL 隐私节。
5. **0.6 以下的真实纠正会被丢弃**，只能从丢弃日志发现；缓解是手册要求纠正时用 `memory_supersede` 显式指定旧 id。
6. **契约测试要真调模型**，不是 CI 里的离线单测；`--offline` 只覆盖结构。
7. **检索仍是词法 + 结构化过滤**，领域约定靠 `scope=domain` 精确匹配补位，不做语义相似（V2）。

---

## 9. 数字盘点

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

## 10. 下一步

- **短期**：按 Pro 的三关验收——真实使用 1–2 周，盯两个信号：①长对话触发压缩后是否"失忆"（看 `inject_faded_*` 与 `memory_probe`）；②是否要反复提醒模型才用记忆（看 `memory_remember` 调用频次与丢弃日志）。
- **中期（V2 边界内）**：中文 Bigram/FTS5 提升召回、类型化半衰期与自动遗忘、冲突发现可视化、异步 Consolidation。
- **不做**：Embedding 唯一方案、自动人格推断、不可逆归并、按工作区拆库、存全量聊天记录。

---

## 11. 0.1.0 更新（三方审查 26 项）

### 11.1 第一批：安全与文档（S1/P1/S2/X2）

| 项 | 状态 | 落点 |
|---|---|---|
| S1 管理 API fail-open | **修复** | `routes.js` fail-closed（无信任栅栏 → 503），ADR-025，两条 503 断言 |
| P1 数字不一致 | **修复** | README 改 CI badge；INSTALL/engine README 写"以 CI 为准"；标题旧名修正 |
| S2 首屏 Privacy | **修复** | README 顶部隐私段 + 安装对象声明 |
| X2 幂等 TTL | **文档化** | README「可靠性设计（三条）」：`memory_request` 保留 7 天、`tick` 清理 |

### 11.2 第二批：写入质量与检索结构（W1/W6/W2/W3/W5/R4）

| 项 | 状态 | 落点 |
|---|---|---|
| W1 类型判据入不变量 | **修复** | `valid_until` 仅 state；`always` 白名单；`type_hint` 留痕，ADR-026 |
| W6 always 收紧 | **修复** | 只允许 `profile/{identity,interaction,constraint}` 且 `scope=global` |
| W2 候选 ≠ 记忆 | **修复** | `preview()/memory_preview` 只算不写；抽取通道也改走 preview→add，ADR-032 |
| W3 闸门句式化 | **修复** | 句首判断 + 问号硬拦 + 显式候选跳过词表，ADR-027 |
| W5 抽取 interaction 收窄 | **修复** | 推断的 global interaction 降 `on_demand`；抽取器跟随 domain |
| R4 检索三阶段 | **修复** | `stage_eligibility/ranking/budget` + `debug.stages` |

### 11.3 第三批：审查收口（W7/W4/O2/M1/M2/R2/M3/O3）

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

### 11.4 交付缺陷 #12（真实库跑出来的）

`memory_probe` 报"注入 19 次、可见 0 次、原因不明消失 8 次"——查实是核对位置错了：
`decision.messages` 只有本步新消息，上一轮注入在**会话历史**里。改为以
`session.deriveMessages()` 为主证据后误报归零，ADR-034。只影响遥测，不影响注入本身。

---

## 12. 0.1.0 收尾记录

### 12.1 O3 小样本 A/B（2026-11-14）

- 方法：`contract` profile，同一句"明天该复习什么？两句话就够。"；A 侧开记忆（库里只种两条：`用户叫张三，正在备考考研数学一`、`用户备考 2027 考研数学一，考试在 12 月`），B 侧 `dsh-attune-memory disabled: true`。
- 结果：A 答"距离 12 月初试只剩一个多月……重点盯 1^∞ 的 e^{v·ln u} 还原"，B 完全没提考试时间。
  "12 月初试"只存在于库里，工作区笔记只有"极限七种未定式"，**可判定为记忆注入带来的差异**。
- 混杂因素：B 侧仍能读到工作区文件（提到 833 传热学、W2 专题计划），所以这一轮只能证明"注入的事实在回答里出现了"，
  不能证明"没有记忆时回答质量更差"。要得到可用 A/B，需要在**干净工作区**跑多轮。
- 结论：方向对，样本不足；真实使用 1–2 周后再统计（指标见 `V2_V3_RESERVED.md` O3）。

### 12.2 真实库上的第一次维护（2026-11-14）

`memctl scan` 在用户真实库上首跑即抓到三类问题，全部按"可逆优先"处理：
1. 同槽位两条 active 的 `state/goal`（备考 vs 已完成的可视化任务）→ 后者改记 `state/activity` + `on_demand`；
2. 一次性任务指令被当成 `profile/interaction` → 软删除（可恢复）；
3. `superseded_by` 指向已删条目 → 整条任务链归档。
处理后 `stats().problems == []`，5 条 active。

### 12.3 数字

引擎单测 **107 通过**；插件断言 recall 12 组 / engine 3 组 / extract 4 组 / routes.stub 11 项 / panel 1 组；
eval 24/24；ADR **34 条**；发布树 68 项。
