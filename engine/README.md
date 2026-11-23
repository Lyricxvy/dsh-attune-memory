# Memory Engine V1

DeepSeek Harness 长期记忆系统的独立内核。**只依赖 Python 标准库**（`sqlite3` / `json` / `dataclasses`），
不含任何 harness 代码、不 import 宿主、不发网络请求。P8 把它当外部进程用，P9 的界面也是通过它读写。

- 设计依据：`../MEMORY_SCHEMA_V1.md`（表结构）、`../MEMORY_WRITE_PIPELINE.md`（写决策）、
  `../MEMORY_READ_PIPELINE.md`（读取与预算）、`../MEMORY_RETRIEVAL_V1.md`（检索与打分）、
  `../MEMORY_LIFECYCLE.md`（状态机与可解释性）。
- 运行环境：Python 3.11.5（Windows）。`sqlite3` 版本随 CPython 分发，无需安装任何包。

## 目录

```
engine/
├── memory/
│   ├── const.py     枚举、阈值、预算、句式表（所有"魔法值"都在这里）
│   ├── db.py        DDL（主表 24 列 + memory_change + memory_meta + 8 索引）
│   ├── util.py      时间与 id（UTC 秒精度字符串，可字典序比较）
│   ├── text.py      归一化、切分、关键词、词表匹配
│   ├── store.py     单表读写、可重入事务、不变式检查
│   ├── search.py    结构化过滤 → 词法匹配 → 六因子打分
│   ├── write.py     写管线：七种动作的决策与落库
│   ├── read.py      读取管线：S 通道手册 / C 通道稳定层 / T 通道本次相关
│   ├── explain.py   可解释性：时间轴、版本 diff、状态理由
│   ├── manager.py   Memory 门面（add/search/list/forget/explain/read_for_context/tick…）
│   ├── rpc.py       stdio JSON-RPC，一行请求一行响应（给 Node 插件调用）
│   └── __init__.py  对外导出
└── tests/
    └── test_engine.py   主测试集（条数随版本增长，以实际输出为准）
```

## 跑测试

```
cd engine
python -m unittest discover -s tests
```

最近一次结果（2026-11-13，Python 3.11.5）：

```
Ran <N> tests in <t>s
OK
```

覆盖范围：写入七种动作（含 IGNORE 的三种触发、TOUCH 的置信度封顶、主体坍缩拒绝）、
考研→不考研的 SUPERSEDE、UPDATE 与 MERGE 的独立用例、生命周期四级（archive/restore/delete/purge）、
读取预算与 stable/context 去重、检索闸门与 Top-K、RPC 协议、全库不变式、中文切分。

## 直接当库用

```python
from attune_memory import Memory

mem = Memory("memory.db")            # 或 Memory(":memory:") 做实验
out = mem.add("用户希望先讲思路再给步骤", "profile", "interaction",
              injection_policy="always", importance="key", confidence=0.9)
print(out["action"])                 # add / touch / supersede / ignore …
print(mem.read_for_context("这道极限题怎么做", domain="study.math"))
```

`mem.add()` 返回 `{"action", "reason", "memory", "related", "merge_suggestion"}`；
`action == "ignore"` 时 `memory` 为 `None`，`reason` 直接可以回给模型看。

## 当子进程用（P8 的实际接法）

```
python -m attune_memory.rpc memory.db
```

stdin 每行一个请求，stdout 每行一个响应：

```json
{"id":1,"op":"search","args":{"query":"数学一","domain":"study.math","limit":5}}
{"id":1,"ok":true,"result":[{"id":"m-...","content":"用户主要备考数学一","score":0.324}]}
```

失败时是 `{"id":1,"ok":false,"error":"...","traceback":"..."}`，进程本身不退出。
op 白名单在 `rpc.py` 的 `_SPEC`：`add/update/supersede/merge/archive/delete/forget/restore/purge/
get/explain/list/search/find_similar/read_for_context/stats/tick`。
**多传一个参数就会报错**——静默忽略会让调用方以为生效了，这是最难查的一类 bug。

为什么是 stdio 子进程而不是 HTTP 或进程内嵌：

| 方案 | 为什么不 |
| --- | --- |
| 把 Python 嵌进 Node | 需要 native addon 或 ffi，宿主升级即碎 |
| 本地 HTTP | 要多守一个端口、加一层鉴权（否则任何网页都能读写记忆库）；每次请求还有 TCP 开销 |
| stdio 子进程 | 一次 spawn 常驻、天然背压、天然隔离；引擎崩了插件 fail-open，DSH 不受牵连 |

## v0.3.0 新增的命令

| 命令 | 作用 |
|---|---|
| `python -m eval.gates` | 四道闸一次跑（契约 / 场景 / 判分器 / 规模），CI 用它；`--fast` 供本地开发 |
| `python -m eval.bench_write_scale` | 写入规模基准：per-write 增长倍率 + 最大样本耗时 + 逐段 doubling 参考值 |
| `python -m attune_memory.memctl migrate --db <path>` | 老库补时态四列并回填（幂等，可反复跑） |
| `python -m eval.external.run ...` | 外部基线（LongMemEval / LoCoMo），产物一律标 UNVERIFIED |

时态四字段：`temporal_kind`（current/historical/event/uncertain）、`invalidation`（NULL/corrected/superseded）、
`event_date`（仅 event）、`last_verified`。`search(..., asof=...)` 能查"某一天成立的事实"。

检索的候选集也换了来源：`search()` 现在先按查询词查 `memory_gram` 倒排
（`store.candidates_by_terms`，按共现 gram 数排序，df > 200 的公共 gram 不进 `IN`），
再补 50 条"最近写入"兜底领域约定。旧实现取 `ORDER BY updated_at DESC LIMIT 200`——
库里三万条时这 200 条全是最近写的，相关记忆根本进不了候选（本地复现：501 条时
`What degree did I graduate with?` 命中 0 条；回归用例 `R-2.3.5`）。

## 几个刻意的取舍

| 取舍 | 理由 |
| --- | --- |
| 一条主表 + 时间轴 + meta，不建 User Model 副本表 | 约束 A：User Model 是查询期视图；P2 §11 的判定是"删掉除主表外所有缓存/副本，行为不变" |
| `status` 只有四值（active/superseded/archived/deleted） | `created`/`updated` 是时刻不是状态，用 `created_at` 与 `memory_change.action` 表达（P6 §1） |
| 检索闸门卡 `Relevance × Confidence × ContextMatch` | 用 Final Score 卡会让 `importance=normal` 或过期条目永远出不来，"降权"悄悄变成"消失"（P5 §3、§8） |
| `keywords` 自动兜底 | 模型忘了填也不能让这条记忆不可检索；填了就信模型的 |
| 事务可重入（`Store.tx()`） | SUPERSEDE 要"插新行 + 改旧行"原子完成，而每个方法各自开事务会撞 `BEGIN` |
| `superseded_by` 先插新行再回填 | 该列有指向 `memories(id)` 的外键，顺序反了直接 `FOREIGN KEY constraint failed` |
| 只做两条自动转换（过期 state 归档、superseded 满 180 天归档） | 自动遗忘/自动合并/自动降权都是不可逆动作，V1 一律不做（P6 §5） |

## 已知局限（属于 V2/V3，不是遗漏）

- 单字查询不参与匹配（`"熵"`），查询侧不做自由分词；中文 Bigram/FTS5 在 V2。
- 没有向量检索、没有关系图、没有后台整理任务。
- `use_count` / `inject_count` 只记账，不参与打分（正反馈会让"被注入多的"越来越容易被注入）。
