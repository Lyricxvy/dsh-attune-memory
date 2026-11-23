# W-0.2 基线核对与差异结论

> STATUS: **UNVERIFIED** — 本对照由被测方自己生成，未经第三方审计。
> 复算命令见文末；产物都在 `.ext-runs/` 下。

## 1. 一句话结论

**v0.3.0 在两种可读配置下都没有劣化，并且在两处都显著变好**：

| 配置 | 基线（v0.2.0 冻结快照） | 新版（v0.3.0） | 差值 |
|---|---|---|---|
| LongMemEval `--haystack 0`（20 题 / 34592 块） | R@5 **10.0%** / Hit@5 10.0% | R@5 **27.5%** / Hit@5 30.0% | **+17.5pp** |
| LongMemEval `--haystack 20`（20 题 / 迷你池） | R@5 **42.5%** / Hit@5 50.0% | R@5 **77.5%** / Hit@5 90.0% | **+35.0pp** |
| LoCoMo `--keywords bow`（1977 题 / 6097 块） | R@5 **13.4%** / R@20 16.9% | R@5 **24.4%** / R@20 33.1% | **+11.0pp**（@20 +16.1pp） |

随机基线：`haystack=0` → 10.1%，`haystack=20` → 25.0%，LoCoMo → 0.9%/3.5%（@5/@20）。
新版两处都**高于随机基线**，基线版本在 `haystack=0` 下与随机持平（10.0% vs 10.1%）。

判定（`eval/external/compare.py`）：三处均 **"不劣化"**——
`haystack=0` 变好 4 / 变差 0 / 持平 16；`haystack=20` 变好 11 / 变差 0 / 持平 9；
LoCoMo 变好 462 / 变差 85 / 持平 1430（1977 题）。

## 2. 为什么基线这么低（以及这次修了什么）

`--haystack 0` 时每题要面对 39–66 个 session、约 1700 个块，20 题合计入库 34592 条。
旧实现 `search.build_candidates()` 取 **`ORDER BY updated_at DESC LIMIT 200`**——
候选永远是"最近写的 200 条"，与问题词毫无关系的也进来了，相关的旧记忆根本不在候选集里。

本地复现（501 条就够）：

```text
写入 1 条 "I graduated with a Master of Science in Computer Science."
再写 500 条 grocery/laundry 噪声
query "What degree did I graduate with?"  →  命中 0 条
```

修法（W-2.3 收尾）：`store.candidates_by_terms()` 按查询词查 `memory_gram` 倒排，
按共现 gram 数排序取 200 条；df > 200 的公共 gram 不进 `IN`（否则那条链接近整张表）；
`search()` 里再补 50 条"最近写入"兜底领域约定。回归用例
`test_v030_stage2.TestIndexedCandidates`（R-2.3.5）。

同时修了判分工具的一处口径错误：`eval/external/report.py` 之前**忽略 `run.json` 里的
`haystack`**，把 `haystack=20` 的运行按全量语料重建 gold/n_units 判分，
把 42.5% 算成 1.7%。现在 `--haystack` 不传就用 `run.json` 的值。

## 2.5 LoCoMo 的 85 题变差（必须逐题交代）

85/1977 = **4.3%** 的题在 @20 上变差，全部集中在候选集换序，不是"检索崩了"：

- 85 题**新版都有 20 条返回**（无一为空），所以不是候选为空导致的消失；
- 分布在全部 10 个会话（conv-47 13、conv-48 11、conv-50 11、conv-41 10、conv-44 10、conv-42 9、conv-30 6、conv-26 5、conv-43 5、conv-49 5），
  没有单个会话异常；
- 每个 case 有 5882 units / 6097 chunks，而候选上限 `MAX_CANDIDATES=200`。
  旧实现取"最近写的 200 块"，新实现取"命中 gram 最多的 200 块 + 50 条最近"——
  **两边都是 200 的子集**，交集之外的题就会一升一降：某条 gold 块在旧窗口里排第 ~40、
  在新候选集里排第 200 名外，就成了"变差"。净效果仍是 +11.0pp / +16.1pp。

这是 `MAX_CANDIDATES=200` 这个容量上限的直接代价，不是回归；要彻底消除得把候选上限
提到全量（或上向量召回），属 v0.4.0 Raw-history RAG 的范围（计划书 §六 V1-3）。

## 3. 失败样例（新版 `haystack=0`，Recall@20 = 0 的前 5 条）

| 题号 | 问题 | 标准答案 units | 实际 top-10 |
|---|---|---|---|
| `0977f2af` | What new kitchen gadget did I invest in before getting the Air Fryer? | 2 个 | `['2d8a12f3']`（错） |
| `6c49646a` | What is the total distance I covered in my four road trips? | 2 个 | `[]` |
| `af8d2e46` | How many shirts did I pack for my 5-day trip to Costa Rica? | 1 个 | `[]` |
| `bbf86515` | How many days before the 'Rack Fest' did I participate in the 'Turbocharged Tuesdays' event? | 2 个 | `[]` |
| `36b9f61e` | What is the total amount I spent on luxury items in the past few months? | 3 个 | `['sharegpt_EGgKdBw_0','889efc00_2','b283f9eb_5','56911dc5_4','sharegpt_QN26oUg_0']`（全错） |

共性：**问题用词与原文几乎不重合**（问 "How many shirts did I pack" / "total distance" /
"total amount"），词法检索拿不到入口；"总数/合计/多少天后"这类需要跨 session 聚合的问题，
单条 memory 也不是答案单位。这正是 v0.4.0 Raw-history RAG 要解的问题，不属于 v0.3.0 范围
（计划书 §六 V1-3 明确本版不做）。

## 4. 产物与复算

| 文件 | 内容 |
|---|---|
| `.ext-runs/lme-baseline-full/` | 基线 `haystack=0`（冻结快照 `/tmp/baseline-engine`，sha `c14bd981b99c…`），20/20 |
| `.ext-runs/lme-post-w23/` | 新版 `haystack=0`（sha `19f2e03fa3d8…`），20/20，1967s |
| `.ext-runs/lme-auto/` | 基线 `haystack=20` |
| `.ext-runs/locomo-bow/` | 基线 LoCoMo（1977 题，`bow`） |
| `.ext-runs/locomo-bow-post/` | 新版 LoCoMo（1977 题，453s） |
| `.ext-runs/lme-post-h20/` | 新版 `haystack=20`，1483s |
| `*.report.txt` | 四份 report 文本（STATUS=UNVERIFIED） |

```bash
cd engine
python -m eval.external.report  --run-dir ../.ext-runs/lme-post-w23 --bench longmemeval --data ../.ext-dl/longmemeval_s
python -m eval.external.compare --base ../.ext-runs/lme-baseline-full --new ../.ext-runs/lme-post-w23 \
       --bench longmemeval --data ../.ext-dl/longmemeval_s
python -m eval.external.compare --base ../.ext-runs/lme-auto --new ../.ext-runs/lme-post-h20 \
       --bench longmemeval --data ../.ext-dl/longmemeval_s
```

## 5. 结论边界

- 20 题，样本 <50：**只能当方向性证据**，置信区间 ±10pp 量级；不作为 v0.3.0 的对外指标。
- `haystack=20` 是迷你池，不可与论文横向比较；`haystack=0` 才是完整语料。
- 两处都是 **A-R 检索模式**（能否把答案 session 排进前 K），不代表回答质量。
- 本次基线是用**冻结快照**跑的（`/tmp/baseline-engine`，22 个 .py），跨会话续跑用
  `--resume`；`code_fingerprint()` 只哈希 `eval/` 以外的文件，故换判分工具不影响指纹。
