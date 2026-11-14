# 记忆系统评估套件（P10）

> 单元测试证明"函数按写法工作"；评估套件证明"系统在真实输入下不给用户添乱"。
> 所以这里每一格都从 `attune_memory.rpc` 的 stdio 入口进（跟插件在宿主里的用法完全一致）、
> 用全新临时库、按用户真会说的话喂进去，只断言**行为**，不断言实现。

```powershell
cd engine
python -m eval.run_eval            # 五个场景全跑
python -m eval.run_eval --only ABC # 先跑绿 A/B/C（任务书要求），再补 D/E
python -m eval.run_eval --json     # 机器可读；--keep 保留临时库
```

退出码：全通过 `0`，有失败 `1`。

## 当前结果

```text
场景 A/B/C/D/E：24/24 通过，耗时 116ms
```

配套单测：`python -m unittest discover -s tests -q` → `Ran 47 tests … OK`。

---

## 1. 五条判据

### A 不存垃圾

"聊天记录 ≠ 记忆"。随口一句天气、一句"有点困"，如果收进库里，下次注入的就是噪声，
而且它会一直占着 always/contextual 的名额。

| 输入 | 期望 | 实测 |
| --- | --- | --- |
| 今天天气不错，挺适合晒被子的 | IGNORE | `时效性内容（命中「今天天气」）` |
| 刚吃完午饭，有点困 | IGNORE | `时效性内容（命中「有点困」）` |
| 这道数学题怎么做？ | IGNORE | `这是一个问题，不是一条事实` |
| 整轮前后 | `total` 不变 | `0 → 0` |

### B 不认错主体

"我朋友喜欢红色"不是**用户**的偏好。主体坍缩是 V1 最危险的错误：
它会把别人的画像写进 User Model，然后每轮注入给用户看。

| 输入 | 期望 | 实测 |
| --- | --- | --- |
| 我朋友小王很喜欢红色（subject_type=person） | 记在 person:小王 名下 | `subject=person:小王` |
| 同一句以 subject_type=user 写入 | 被拒，且提示改成 person/project/org | `content 出现「我朋友」但 subject_type=user…` |
| 用户叫张三，正在备考考研数学一 | 记在 user 名下 | `subject=user` |
| 非 user 主体 + injection_policy=always | 被拒（防污染 User Model） | `非用户主体的记忆不允许 always 注入` |

### C State 正确更新

"考研 → 不考研"必须是**取代**，不是并列新增两条互相矛盾的状态。
并列新增的后果是：两条都 active、检索时随机命中一条，用户看到的是自相矛盾的画像。

| 断言 | 实测 |
| --- | --- |
| 旧条目 `status=superseded` 且 `superseded_by=新 id` | `status=superseded superseded_by=m-mwf3aeht-5z` |
| 旧条目有 `valid_until` | `valid_until=2026-11-13T14:46:01Z` |
| 新条目 `status=active` | `status=active` |
| 同槽位只有一条 active（不是并列新增） | `active state 条数=1` |
| `explain` 能说清为什么被取代 | `已被 m-… 取代（2026-11-13T14:46:01Z 起失效）：调用方显式指定取代` |

### D 不污染上下文

"记忆越多越好"是反的。问数学题时，项目决策、过期状态、无关历史都不该出现。

种四条：数学技能（study.math）、项目决策（code.dsh, on_demand）、
过期状态（`valid_until=2026-01-01`）、无关历史（asar, on_demand），然后问
"数学一极限题怎么突破变形方向？"（domain=study.math）：

| 断言 | 实测 |
| --- | --- |
| 带上数学相关记忆 | 命中"极限" |
| 不含无关的项目决策 | 干净（无 stdio / SQLite） |
| 不含过期状态 | 干净（无"备考计划表"） |
| 不含 on_demand 历史 | 干净（无 asar） |
| 写文案时不注入数学记忆 | 干净 |

### E scope 隔离

Interaction Model 是灵魂，但它必须**分域**：数学题上的"先讲思路再给步骤"
不该在写文案时出现，写作的"先给三个方向"也不该在解数学题时出现。

| 断言 | 实测 |
| --- | --- |
| 数学上下文命中 `scope=domain, domain_key=study.math` 的交互约定 | 命中 |
| 数学上下文不含写作域约定 | 干净 |
| 写文案命中写作域约定 | 命中 |
| 写文案不打扰数学约定 | 干净 |
| 无域查询不把域约定当全局偏好 | 干净 |
| 两条约定都仍 active（互不覆盖） | ok |

---

## 2. 这套评估抓出的三个真缺陷

评估套件不是"跑个绿给自己看"，它的价值在于第一次跑就挂了两格。

### 缺陷 A-1：时效词表太窄

`刚吃完午饭，有点困` 被当成事实 ADD 进库（词表里只有 `我有点困`，没有 `有点困`）。
修法：`const.TRANSIENT_PATTERNS` 补一过性身体/情绪状态（有点困/有点累/有点饿/刚吃完/正在吃…），
**只收"明显是此刻"的说法**，不收 `在忙项目` 这种可能是长期状态的说法。

### 缺陷 D-1：`valid_until` 根本写不进去

想让一条 state 立刻过期（"这份计划已经作废"），`add` 直接被参数白名单拒掉——
引擎的 `Candidate` 有 `valid_until` 字段，但 RPC 的 `_SPEC` 漏了它。
修法：`_SPEC` 的 `add`/`supersede` 加 `valid_until`，并在 `write.validate()` 里校验
它是合法 ISO、且必须晚于 `valid_from`（格式错一次，这条记忆就永远不过期或立刻过期）。

### 缺陷 E-1：领域约定在词法检索里是隐形的

问"这道极限题怎么下手"，约定写的是"讲数学题时先讲思路再给步骤"——**没有一个共同词**。
纯词法检索给它的 relevance 是 0，直接被命中闸门挡掉，于是 Interaction Model 最该起作用的地方
恰好永远不生效。修法：`read.py` 新增 `_domain_rules()`——领域被明确识别时，
把该域下 `scope=domain` 的 `profile/interaction` 记忆按 domain 精确匹配补一次
（`DOMAIN_RULE_MIN_CONFIDENCE=0.8`，不占直接命中的名额，无域查询时不补）。
这条改动只在**读管线**里做，不动 `search()` 的语义：模型主动搜记忆时，
词法命中仍然是唯一依据。

---

## 3. 覆盖关系

| 判据 | 对应任务书 | 对应的硬约束 / 哲学 |
| --- | --- | --- |
| A 不存垃圾 | P10-A | 哲学 ②：Memory ≠ 越多越好 |
| B 不认错主体 | P10-B | 约束 A：User Model 是逻辑视图，别人的事不能坍缩进来 |
| C State 更新 | P10-C | 约束 C：冲突是 feature，不悄悄覆盖 |
| D 不污染 | P10-D | 哲学 ⑤：Interaction Model 要分域；P4 预算与裁剪 |
| E scope 隔离 | P10-E | 约束 D：scope ∈ {global, domain, task} |

## 4. 真机验证（模型层，A + B 的合体）

引擎层能断言"输入的这句话会不会被收下"，但断言不了"模型会不会去调工具、会不会挑对主体"。
所以另跑了一次真实会话（headless profile + 真模型 `deepseek-flash`，独立库 `.verify/live-p10.db`）：

> 用户：今天天气不错，适合晒被子。另外我朋友小王特别喜欢红色，买什么都挑红的。你回一句话确认就行。

模型的推理里自己做了分诊：

> "`小王特别喜欢红色，买什么都挑红的` — that's a stable fact about a person (friend), which could be worth
> remembering with subject_type=person… Also weather — don't record."

回答："好天气适合晒被子，晒完的被子会带着太阳味；关于小王喜欢红色这件事，我也记下了。"

落库结果（`memctl list`）：

```text
id            type     subtype     状态    重要性   注入        内容
m-mwf3bxuz-10 profile  preference  active  normal   on_demand   用户的朋友小王特别喜欢红色，买东西都优先挑红色的。
```

```json
{"subject_type": "person", "subject_name": "小王", "confidence": 0.9, "injection_policy": "on_demand"}
```

三件事同时成立：天气那句话**没有**变成记忆；朋友的事记在了 `person:小王` 名下而不是用户画像里；
`on_demand` 而不是 `always`（朋友的偏好不该每轮塞给用户看）。`stats.problems` 为空。

## 5. 这套评估**不**覆盖什么

- **模型会不会主动调用 `memory_remember`**。这属于"模型行为"，只能真机跑（见
  `HOST_INTEGRATION.md` §5 的端到端记录），评估套件里不假装能测。
- **压缩后的重注入**。它需要真实压缩事件，已由 P8 的会话日志单独验证（`seq=82/85`）。
- **并发与多进程**。V1 是单宿主单进程，`BEGIN IMMEDIATE` 已经够用。

## 6. 怎么加一格

在 `engine/eval/run_eval.py` 里加一个 `scenario_x(rpc) -> list[Case]`，注册进 `SCENARIOS`。
`Case(name, ok, detail)` 的 `detail` 要能让人不看代码就知道为什么挂——
这是评估报告，不是断言堆。
