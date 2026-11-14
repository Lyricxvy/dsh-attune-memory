# dsh-attune-memory

[![tests](https://github.com/Lyricxvy/dsh-attune-memory/actions/workflows/tests.yml/badge.svg)](https://github.com/Lyricxvy/dsh-attune-memory/actions/workflows/tests.yml)
[![license: MIT](https://img.shields.io/badge/license-MIT-black.svg)](LICENSE)
[![python](https://img.shields.io/badge/python-3.11-blue.svg)](engine)

给 [DeepSeek Harness](https://github.com/deepseek-ai)（下称 DSH）用的**跨会话长期记忆**：一个独立可测的 Python/SQLite 引擎，加一个把记忆接进模型上下文的宿主插件，以及一个能在 GUI 里查看/修改记忆的面板。

> 记录的是「关于这个人稳定的事实」和「怎么帮他更顺手」，不是聊天记录。

> **隐私**：记忆全部存在**本机** SQLite（默认 `$DSH_HOME/attune-memory/memory.db`），不上传任何服务器。诊断功能会在本机保留有上限的片段：最近 200 条查询的前 80 字、被拒候选的前 120 字（最长 14 天，`memctl discarded --purge` 可清空）。细节见 `INSTALL.md` 的「隐私」一节。
>
> **版本**：**v0.1.0**（首个公开版本）。逐项状态、升级步骤与已知限制见 `docs/RELEASE_NOTES_v0.1.0.md`。
>
> **面向谁**：能改 DSH profile 的开发者/高级用户（Windows Junction + profile 配置）。**目前没有一键安装**，步骤见 `INSTALL.md`。

## 它解决什么问题

大多数人做「AI 记忆」的第一反应是把历史全存下来、每轮全塞回去。那会得到两个坏结果：上下文被稀释成噪声，而且记错了没法纠正。这个项目走的是另一条路：

| 常做的做法 | 这里的做法 |
| --- | --- |
| 存全量聊天记录 | 只存提炼后的事实，历史只是原料（`docs/MEMORY_SYSTEM_DESIGN_PRINCIPLES.md`） |
| 记忆越多越好 | 写入前先过一道价值闸门，宁可不记也不记错（`docs/MEMORY_WRITE_PIPELINE.md`） |
| 直接加一条新的 | 七种写操作：`IGNORE / ADD / UPDATE / MERGE / SUPERSEDE / ARCHIVE / DELETE`，事实变了就取代，不是并列 |
| 冲突悄悄覆盖 | 冲突是 feature：互斥标记、双条返回、`explain` 能说清为什么变（`docs/MEMORY_LIFECYCLE.md`） |
| 记成一个字符串 | 四类语义角色 `profile / domain / state / episodic` + 主体防坍缩（`person` / `project` / `org` 各有归属）+ 作用域隔离（`global / domain / task`） |
| 只会存不会解释 | 每条都有变更流水：为什么变成现在这样、什么时候、谁改的、旧版本是什么 |

## 架构

```
用户消息
   │
   ├─ S1 通道  system prompt section (order=150)  「怎么用记忆」的常量手册
   ├─ S2 通道  system prompt section (order=155)  always 层稳定事实（长在 system prompt 里）
   └─ T  通道  agent/pre-step 注入「本次相关」到最后一条真人消息之前
                     │
                     ▼
              ┌──────────────┐   stdio JSON-RPC   ┌─────────────────────┐
              │ DSH 插件      │ ◄────────────────► │ Python 引擎          │
              │ (Node, ESM)  │   一行一请求/应答    │ 词法检索 + 结构化过滤 │
              └──────────────┘                    │ SQLite (单库)        │
                     ▲                            └─────────────────────┘
                     │ 8 个 memory_* 工具                    │
                 模型自主写入 + turn/end 确定性抽取            └─ /dsh-attune-memory/ 管理页
```

三个通道是刻意分开的：**S 段（手册）**教模型何时该用记忆；**S2 段（稳定事实）**必须活下来，因为任何压缩层都只折叠消息历史，碰不到 system prompt；**T 段（相关记忆）**跟着当前问题走，插在提问之前。

## 快速开始

```bash
git clone https://github.com/Lyricxvy/dsh-attune-memory.git
cd dsh-attune-memory/engine
python -m unittest discover -s tests     # 期望全部 OK
python -m eval.run_eval                  # 5 个场景 24 格，应当全过
```

接进 harness（改 profile + 重启桌面应用）见 **[INSTALL.md](INSTALL.md)**。

## 目录

```
engine/                  Python 引擎（纯标准库，无第三方依赖）
  memory/                const / db / util / text / store / search / write / read
                         / explain / manager / rpc / memctl
  tests/                 引擎单测 + RPC 可靠性 + 隐私/交付核验
  eval/                  5 个评估场景（A 不存垃圾 … E scope 隔离）
plugin/dsh-attune-memory/  DSH 插件（Node ESM，无构建步骤）
  lib/                   engine(RPC 客户端) / recall(注入) / tools / extract / routes / ui / client（设置面板）
docs/                    18 份设计与取证文档
```

## 文档索引

| 文档 | 讲什么 |
| --- | --- |
| [MEMORY_SYSTEM_DESIGN_PRINCIPLES](docs/MEMORY_SYSTEM_DESIGN_PRINCIPLES.md) | 五条哲学、四条硬约束、13 项明确不做的事 |
| [ADR](docs/ADR.md) | 34 条架构决策，每条含被否决的备选方案与理由 |
| [CURRENT_ENVIRONMENT_ANALYSIS](docs/CURRENT_ENVIRONMENT_ANALYSIS.md) | DSH 消息生命周期取证：注入点为什么只能在这里 |
| [ADOPTION_DECISION_TABLE](docs/ADOPTION_DECISION_TABLE.md) | 36 项机制取舍（采用/改造/推迟/拒绝） |
| [MEMORY_SCHEMA_V1](docs/MEMORY_SCHEMA_V1.md) | 24 字段主表、四类角色、主体防坍缩、8 条索引 |
| [MEMORY_WRITE_PIPELINE](docs/MEMORY_WRITE_PIPELINE.md) | 写管线：价值闸门、槽位、九条决策表 |
| [MEMORY_READ_PIPELINE](docs/MEMORY_READ_PIPELINE.md) | 三通道与预算：什么时候注入什么、超了先砍谁 |
| [MEMORY_RETRIEVAL_V1](docs/MEMORY_RETRIEVAL_V1.md) | 检索：结构化过滤 + 词法匹配 + 六因子打分 |
| [MEMORY_LIFECYCLE](docs/MEMORY_LIFECYCLE.md) | 状态机、合法转移、可解释性 |
| [HOST_INTEGRATION](docs/HOST_INTEGRATION.md) | 接入实录：真实会话 seq 证据、压缩后三层恢复 |
| [MEMORY_ADMIN](docs/MEMORY_ADMIN.md) | 管理页与 CLI：信任边界、不暴露裸 SQL 的四道闸 |
| [MEMORY_EVAL_SUITE](docs/MEMORY_EVAL_SUITE.md) | 评估套件：A 不存垃圾 / B 不认错主体 / C 状态更新 / D 不污染 / E 作用域 |
| [MEMORY_V1_1_TUNING](docs/MEMORY_V1_1_TUNING.md) | 11 个只有真跑才暴露的缺陷与修法 |
| [MEMORY_PRINCIPLES_REPORT](docs/MEMORY_PRINCIPLES_REPORT.md) | 记忆原理长文：对象模型、写入/读取/检索/生命周期、宿主耦合与实测 |
| [RELEASE_NOTES_v0.1.0](docs/RELEASE_NOTES_v0.1.0.md) | 0.1.0 发布说明：26 项审查收口、安全栅栏、写入与检索改进、升级与限制 |
| [V1_1_UPDATE_REPORT](docs/V1_1_UPDATE_REPORT.md) | 内部阶段记录：九模块变更、schema v3、幂等与冲突分级 |
| [V2_V3_RESERVED](docs/V2_V3_RESERVED.md) | 刻意推迟的东西，以及什么信号才该做 |
| [UNKNOWN_LIST](docs/UNKNOWN_LIST.md) | 还不知道的、以及怎么绕行 |

## 验证状态

| 项目 | 结果 |
| --- | --- |
| 引擎单元测试 | `python -m unittest discover -s tests` —— 应全部 OK（具体条数随版本增长，以 CI 为准）|
| 评估套件 A–E | 24/24 通过 |
| RPC 冒烟（Node ↔ Python，中文往返） | 通过 |
| 真实 DSH 端到端（注入 → 回答 → 写库） | 通过 |
| 压缩后重注入 | 通过（会话日志 seq 证据见 HOST_INTEGRATION） |
| 无副本存储 | 通过（库里只有 3 张表，注入内容全部现查现拼） |
| 管理页 / 信任栅栏 | 200 / 未授权 401；栅栏不可用时 **503 fail-closed** |

## 可靠性设计（三条）

1. **写请求幂等**：每个写操作带一个 `dedupe` 键（客户端自动生成），引擎把**成功**响应写进
   `memory_request` 表并回放，所以超时重试不会写两条；失败不缓存，允许原样重试。
   这张表的记录 **保留 7 天**，由 `tick` 顺带清理（`memory_request` 不会无限增长）。
2. **失败分流**：写返回 `WRITE_OK` / `WRITE_RETRYABLE`（超时、引擎死掉——可原样重试一次）/
   `WRITE_FAILED`（参数错、被拒——重试没用）；读失败一律 fail-open，不拖垮对话。
3. **可观测**：`memctl telemetry|reads|discarded` 与管理页「诊断」面板能看到计数、最近检索、
   被丢弃的候选；`memory_probe` 能回答"注入了但没进模型视野"。

## 已知边界

- 中文检索是关键词匹配，不是语义检索；FTS5 + Bigram 排在 V2。
- 代理层（如 ACP）的压缩对宿主完全不可见，所以稳定事实放在 system prompt 里而不是消息里；若某天压缩器连 system 字段也改写，这条路会失效。
- 单用户单库；多用户/多设备同步在 V3。
- 写记忆以模型主动调用工具为主，模型的主动性会影响记忆密度。

## License

MIT，见 [LICENSE](LICENSE)。
