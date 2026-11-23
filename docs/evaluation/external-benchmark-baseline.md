# 外部基准 · 基线冻结发现（2026-11-20）

> 性质：V1-0 基线冻结的执行记录 + 对《外部基准-首轮发现.md》的一处事实修正。
> 触发：哥哥拍板「立即运行，固定版本/口径，保留 13.4% 复现记录」。

## 1. LoCoMo 基线：已冻结，无需重跑

- 引擎代码指纹 c14bd981b99cadcb（22 个 .py 文件），与首轮 locomo-bow/run.json 完全一致 → 引擎自 2026-11-19 未变。
- 数据集 sha256 79fa87e9… 与 registry.json 登记的 locomo10 一致 → 数据未变。
- 产物完整：.ext-runs/locomo-bow/ 的 run.json + predictions.jsonl + ingest.json + report.md 均在。
- 结论：R@5 13.4% / R@10 15.2% / R@20 16.9% 基线有效，复现记录已存档。

## 2. LongMemEval-S：一处事实修正 + 一处性能瓶颈

### 2.1 修正：首轮文档「共享池」假设是错的

《外部基准-首轮发现.md》§8 曾写：「正确做法是所有题共用一个 session 池（LongMemEval 的 500 个 haystack session 本来就是全题共享的），入库一次，之后每题只做检索」。

数据核查（.ext-dl/longmemeval_s，500 题）推翻了这一假设：

- 每题 haystack_session_ids 数量为 39–66 个（各不相同），不是 500；
- 前 20 题的 haystack session 集合互不相同（share-same = False）；
- registry.json 里「每题 500 个历史 session」的描述同样与实际数据不符。

LongMemEval-S 每题有**独立的** haystack（39–66 session，答案 session + 干扰 session）。不存在「500 session 全题共享池」。正确跑法就是每题独立建库、在自身 haystack 内检索——这正是 run.py 现有逻辑。首轮的「慢」不是「重复灌库的设计缺陷」，而是「每题 haystack 本就独立」的必然。

### 2.2 性能瓶颈：完整 haystack 在 O(n²) 写入下跑不动

- 完整 haystack（39–66 session ≈ 1400–2300 块）在引擎的 O(n²) 线性查重（find_similar）下，单题入库 > 3 分钟。
- smoke 实测（--n 2 --haystack 0）：第 1 题（54 session）2 分 45 秒仍未完成入库（predictions 0 行），已终止。
- 推算：500 题全量 > 25 小时，不可行。

这直接印证台账 #38「缺 500–5000 条规模基准」与设计书 V1-7「数据一致性/索引」：引擎在 2000 块/库规模就退化到不可用。**基线如实记录这一「改造前」性能现状**，它正是后续 V1 要解决的规模问题之一。

## 3. 务实基线口径（已启动）

| 参数 | 首轮 lme-auto | 本轮 lme-baseline-full |
|---|---|---|
| --n | 20 | 20（固定） |
| --haystack | 20（迷你池，random baseline 不合理） | **0（完整 39–66 session，可读）** |
| --keywords | auto | **bow（与 LoCoMo 基线一致）** |
| --seed | 20261119 | 20261119（固定） |
| --k | 20 | 20 |

唯一实质变量是 haystack（迷你 → 完整）与 keywords（auto → bow）。跑完可对比出「池子大小」与「关键词喂足」对召回的影响。预计 1–2 小时（20 题 × 完整 haystack），后台进行。

## 4. 待定

- 若需要更大的 n（如 50/100 题）以压低抽样噪声，须先解决 O(n²) 写入瓶颈（正是台账 #38 / V1-7 的内容），否则耗时不可接受。是否先做引擎写入索引优化再补大 n 基线，等哥哥定。
