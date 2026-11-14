# v0.1.0 更新报告

> 一句话：这是本项目的**首个公开版本 0.1.0**。它的内容等于内部开发阶段的 1.0 → 1.1 → 1.2
> 三个里程碑的合并结果：把三方审查提出的 26 个问题逐条收口，并在真实库上跑出并修掉了
> 第 12 个缺陷（遥测误报）。仓库历史从这一版重新开始，因此内部编号不再对外使用。

---

## 1. 版本定义

| 版本 | 内容 | 状态 |
| --- | --- | --- |
| 内部 1.0 | SQLite + 词法检索 + 三通道注入 + 生命周期 + 可解释 | 已交付 |
| 内部 1.1 | 真实使用暴露的 10 个缺陷修复 + 九模块（契约测试/幂等/写三态/遥测/读日志/丢弃日志/ACP 绕行） | 已交付 |
| **0.1.0** | 三方审查 26 项收口 + 缺陷 #12 + 维护扫描/跨轮回顾/二元组召回 | **首个公开版本（本说明）** |
| V2 | 语义召回（Embedding）、FTS5 重版、语义冲突发现、异步整理 | 只划界 |

**范围声明**：0.1.0 仍然只有 SQLite + 词法/二元组检索 + 结构化过滤；
没有 Embedding、没有记忆图、没有自动遗忘。这三条是 V2/V3 的事，理由见 `V2_V3_RESERVED.md`。

---

## 2. 用户能感觉到的变化

| 变化 | 说明 |
| --- | --- |
| `memory_preview` 新工具 | 只算不写："这条如果记下去会被新增/去重/取代/忽略"先说清楚，再决定要不要 `memory_remember` |
| 写操作三态 | 工具返回首行固定为 `WRITE_OK` / `WRITE_RETRYABLE` / `WRITE_FAILED`，超时不再和"真的失败"混在一起 |
| 幂等写入 | 同一次写请求重发不会写两条；`memory_request` 保留 7 天，`tick` 清理 |
| 诊断入口 | 管理页"诊断"浮层 + `GET /api/reads`、`/api/discarded`；CLI `memctl reads / discarded / scan` |
| 维护扫描 | `Memory.scan()` / `memctl scan`：重复、可能矛盾、同槽多位、已过期、不变式问题五张清单（**只读**） |
| 自愈提醒有节制 | "看不到记忆先 memory_search"每 5 轮出现一次，空库不出现，不再每轮催模型 |
| 去重不再"永久失联" | 注入块被压缩/被代理层抹掉后，8 轮内允许重发，且会核对"上一轮注入还在不在" |
| 领域约定按 domain 生效 | 问数学题会自动带出"讲题先讲思路"这类作用域约定，即使字面不重合 |

---

## 3. 结构性变化

### 3.1 数据库（schema v1 → v3）

```
memories          24 列（不变）
memory_change     append-only 变更流水（不变）
memory_meta       键值（不变）
memory_request    幂等键 → 成功响应，7 天 TTL      ← v1.1
memory_counter    命名计数器（只观测，不参与排序）  ← v1.1
memory_observation 观测流水（read / discarded / maintenance / inject-*） ← v1.1
```

索引维持 8 条业务索引 + `idx_req_created` + `idx_obs_kind`。
升级是 `CREATE TABLE IF NOT EXISTS`，**老库直接可用，不需要迁移脚本**。

### 3.2 检索三阶段（R4）

```
候选（结构化过滤，≤200） → 资格（status/过期/never/domain） → 排序（六因子） → 预算（Top-K 4–6）
```

`search(debug=...)` 与 `read_for_context` 都会带 `stages`（candidates/eligible/ranked/gate_passed/selected），
所以"为什么这次没注入"可以从数字上回答，不靠猜。

### 3.3 写入闸门（W3）与冲突分级（W5 之后）

- 闸门看**句首**：句首疑问词、句首祈使、问号结尾都会被拦（显式记忆也要过"问号/祈使"这两关）。
- 互斥冲突三档：显式且置信度 ≥0.8 → 自动取代；0.6–0.8 → 并存并标 `metadata.conflict_with`；<0.6 → 只进丢弃日志。
- **推断（inferred）永不自动取代**——这是不变量，不是约定。

### 3.4 数值冲突（W7）

`key_values()` 从"取值集合"改为 `{属性: 取值}`，只在**同名属性**取值不同时判冲突。
"每天学 3 小时"与"数学 120 分"不再互相打架；端口/分数/中文数量各自带属性名。

### 3.5 主体判定（W4）双侧拦

- 第一人称开头（且不含第三方标记）却挂 `person` → 报错；
- 含"我朋友/我同事…"却挂 `user` → 报错；
- 非 user 主体必须有 `subject_name`。
主体坍缩现在两个方向都是**可读报错**，不是静默写错。

### 3.6 中文召回（R2）

`text.bigrams()` 给 CJK 段切二元组；词表与片段都未命中时用"查询二元组覆盖率 × 2.0"兜底。
拉丁词改**词边界**匹配，`sq` 不再命中 `sqlite`。这是纯内存实现，FTS5 重版留给 V2。

---

## 4. 三方审查 26 项最终状态

| 项 | 状态 | 落点 |
| --- | --- | --- |
| S1 管理 API fail-open | 修复 | `routes.js` fail-closed（503），ADR-025 |
| S2 首屏 Privacy | 修复 | README 隐私段 + 安装对象声明 |
| W1 类型判据/`valid_until` | 修复 | 仅 state 可带；`type_hint` 留痕，ADR-026 |
| W2 候选 ≠ 记忆 | 修复 | `preview()` + `memory_preview`；抽取通道走 preview→add，ADR-032 |
| W3 闸门句式化 | 修复 | 句首判断，ADR-027 |
| W4 主体保守 | 修复 | 双侧拦，ADR-029 |
| W5 interaction 作用域收窄 | 修复 | 推断的 global interaction 降 `on_demand` |
| W6 always 收紧 | 修复 | 仅 `profile/{identity,interaction,constraint}` 且 global |
| W7 数值冲突 | 修复 | 按属性比，ADR-028 |
| R1 同义改写召回 | **未做** | 需语义模型，V2 第一顺位（§0.1） |
| R2 中文分词 | 修复（词法级） | 二元组兜底，ADR-033 |
| R3 不假装语义 | 修复 | 检索说明与文档去掉"理解语义"的表述 |
| R4 评分三阶段 | 修复 | `stage_*` + `debug.stages` |
| X1 契约测试 | 已有 | H1–H5 真机契约，ADR-020 |
| X2 幂等 TTL | 文档化 | README「可靠性设计」 |
| X3 读写失败分流 | 修复 | `READ_FAILED` vs 写三态 |
| X4 inject_count | 修复 | `mark_injected()` 自增（只认插件回报的 id） |
| O1 运行时可见 | 修复 | `memory_probe` + 管理页诊断 |
| O2 ACP 盲区 | 修复 + 频控 | S2 进 system prompt；自愈指令 5 轮一次，ADR-030 |
| O3 A/B 证据 | 小样本 | `.verify/ab-test.mjs`，仅方向性（§6.1） |
| M1 维护扫描 | 修复 | `scan()`，ADR-031 |
| M2 跨轮回顾 | 修复 | 5 句滚动缓冲 + 三句拼接重抽，ADR-032 |
| M3 Formation/Assembly | 部分 | Assembly 完整；Formation 只到主题簇候选 |
| P1 数字统一 | 修复 | 发布文档改"以 CI 为准" |
| P2 安装对象 | 文档化 | 面向能改 DSH profile 的开发者；一键安装未做 |
| P3 文档同步 | 修复 | 版本/工具数/路由名统一 |

---

## 5. 真实库首跑：维护扫描抓到并修掉的三件事

`memctl scan` 在用户真实库（`$DSH_HOME/attune-memory/memory.db`）首跑结果：

```
扫描 6 条 active：重复 0 ｜ 矛盾 0 ｜ 同槽多位 3 ｜ 过期 0 ｜ 不变式问题 1
```

| 问题 | 处理 |
| --- | --- |
| 同一 `state/goal` 槽位两条 active（备考 + 已完成的可视化任务） | 后者改记 `state/activity` + `on_demand`，槽位冲突解除 |
| 一次性任务指令被记成 `profile/interaction` | 软删除（可恢复），任务应由 `state` 承载 |
| `superseded_by` 指向已删条目（悬挂链） | 整条任务链归档，`superseded_by` 清空 |
| 处理后 | 5 条 active，`stats().problems == []` |

---

## 6. 证据

### 6.1 小样本 A/B（O3，1 轮）

- 方法：`contract` profile，同一句"明天该复习什么？"；A 侧开记忆（只种两条备考事实），B 侧 `disabled: true`。
- 结果：A 答"距离 12 月初试只剩一个多月"；B 完全没提。该事实只存在于库里 → 可判定注入生效。
- 混杂：B 侧仍能读工作区文件，样本仅 1 轮；结论只作方向性证据。

### 6.2 测试

| 项 | 命令 | 结果 |
| --- | --- | --- |
| 引擎单测 | `cd engine && python -m unittest discover -s tests` | **107 通过** |
| 评估套件 | `python -m eval.run_eval` | **24/24** |
| 插件回归 | `node plugin/dsh-attune-memory/test/recall.test.mjs` | 12 组 |
| 引擎客户端 | `node .../test/engine.test.mjs` | 3 组 |
| 抽取器 | `node .../test/extract.test.mjs` | 4 组 |
| 管理路由 | `node .../test/routes.stub.test.mjs` | 11 项（含 503 fail-closed） |
| 面板 | `node .../test/panel.test.mjs` | 1 组 |
| 宿主契约 | `node .../test/contract/run-contract.mjs` | H1–H5 |

### 6.3 缺陷 #12（本轮最后修的）

`memory_probe` 报"注入 19 次 / 可见 0 次 / 原因不明消失 8 次"。根因：核对位置取了
`decision.messages`（只有本步新消息），而上一轮注入早已进**会话历史**。改为
`session.deriveMessages()` 为主证据后误报归零（ADR-034）。**只影响遥测，不影响注入**。

---

## 7. 升级步骤

发布树就是运行中的安装源（desktop profile 的 `node_modules/dsh-attune-memory`
是目录联接指向 `release/dsh-attune-memory/plugin/dsh-attune-memory`），所以：

```powershell
# 1) 重新构建发布树（源码 → 脱敏 → 改名 → 覆盖，不动 .git/.github）
powershell -NoProfile -ExecutionPolicy Bypass -File .publish\build-release.ps1

# 2) 重启 DeepSeek Harness（插件在启动时加载）
```

重启后自检三条：

1. 工具表里能看到 `memory_preview`；
2. `memctl scan` 输出 `不变式问题: 0`；
3. `memory_probe` 的"可见/消失"数字不再一边倒。

**数据兼容**：新表用 `CREATE TABLE IF NOT EXISTS`，老库直接打开即用；
升级前建议 `Copy-Item` 一份 `memory.db` 备份（本机惯例放 `.verify/`）。

---

## 8. 已知限制（诚实清单）

| 限制 | 影响 | 出口 |
| --- | --- | --- |
| 同义改写召回（R1） | "存过但换个说法搜不到" | V2 Embedding 召回（只做召回，不替代过滤） |
| 生成式 Formation | 只能看到"这些像同一主题"，不能自动归纳成一条 | V2，且必须先有可逆的确认路径 |
| ACP 代理层压缩 | 代理若连 system 字段也重写则无解 | `memory_probe` + 自愈指令 + 真机核对 |
| 一键安装 | 需要手改 profile | P2，等插件市场形态稳定 |
| 诊断日志含用户原话片段 | 前 120 字 / 14 天 / 可 `--purge-discarded` | README 隐私段已声明 |

---

## 9. ADR 索引（本版本新增）

| ADR | 主题 |
| --- | --- |
| 025 | 管理 API fail-closed |
| 026 | 类型判据与 always 收紧 |
| 027 | 句式闸门 + 交互作用域收窄 + 三阶段检索 |
| 028 | 数值冲突只在同一属性上判定 |
| 029 | 主体判定双侧都拦 |
| 030 | 自愈指令按轮频控 |
| 031 | 维护扫描只读、判定确定性 |
| 032 | 抽取先预览再落库 + 跨轮拼接 |
| 033 | 中文二元组兜底召回 |
| 034 | 注入可见性核对以会话历史为准 |

---

## 10. 文件清单（本版本改动）

```
engine/attune_memory/{const,db,util,text,store,search,write,read,explain,manager,rpc,memctl}.py
engine/tests/{test_engine,test_rpc_reliability}.py
engine/eval/run_eval.py
plugin/dsh-attune-memory/lib/{index,recall,tools,routes,ui,extract,engine,message}.js
plugin/dsh-attune-memory/test/{recall,engine,extract,routes.stub,routes,panel}.test.mjs
plugin/dsh-attune-memory/test/contract/{run-contract,setup-profile}.mjs + probe/
docs/（16 篇，含 ADR 34 条）
```

**版本号**：`plugin/dsh-attune-memory/package.json` → `0.1.0`；发布包 `dsh-attune-memory-v0.1.0.zip`。
