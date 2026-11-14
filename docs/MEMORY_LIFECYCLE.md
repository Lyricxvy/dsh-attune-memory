# MEMORY_LIFECYCLE — 记忆生命周期与可解释性（P6）

> 上游：`MEMORY_SCHEMA_V1.md` §7（status 四值 + 五条不变式）、`MEMORY_WRITE_PIPELINE.md` §7（七种操作）。
> 本文负责：**一条记忆从生到死的全部合法路径、每条路径的触发条件、以及事后怎么解释它为什么变成现在这样。**

---

## 1. 先解决一个术语冲突：六个状态名 vs 四个状态值

任务书要求状态机包含 `created / active / updated / superseded / archived / deleted` 六个节点，而 P2 把 `status` 列限定为四个值。这不是矛盾，是**两种东西**：

| 六个节点里的 | 实际是什么 | 怎么表示 |
| --- | --- | --- |
| `created` | **瞬时状态**：行刚写入、还没被任何人读到的那一刹那 | `created_at` 时间戳 + `memory_change(action='add')`。`status` 从第一毫秒就是 `active` |
| `updated` | **瞬时状态**：内容被改、还没被再次检索到 | `updated_at` + `memory_change(action='update')`。`status` 始终是 `active` |
| `active` / `superseded` / `archived` / `deleted` | **持续状态**：会停留数天到数年，直接影响检索 | `status` 列 |

**为什么不做成六个值**：`created`/`updated` 如果能被检索、被筛选、被用户看到，就必然产生"一条 created 状态却已经在被注入"的中间态 bug。它们对"这条记忆现在算不算数"没有任何影响——检索只关心那四个持续状态。把瞬时性交给时间戳，是这类状态机最省事也最不容易错的做法。

**后果**：`status` 的合法取值只有四个；任何代码写 `status='created'` 都应该在 CHECK 约束上直接报错。P7 的测试要专门断言这一点。

---

## 2. 状态机

```
                        ┌──────────────┐
        ADD             │   created    │  (瞬时：created_at 落定即离开)
   ∅ ──────────────────▶│   status=    │
                        │   'active'   │
                        └──────┬───────┘
                               │ 同一事务内自动
                               ▼
        UPDATE / TOUCH   ┌──────────────┐   SUPERSEDE    ┌──────────────┐
   ┌───────────────────▶│    active    │───────────────▶│  superseded  │
   │                    └──────┬───────┘                └──────┬───────┘
   │  updated (瞬时)            │                               │
   │  updated_at+change        │ ARCHIVE            ARCHIVE    │
   │                           ▼  (自然结束/退场)               ▼
   │                    ┌──────────────┐                ┌──────────────┐
   └────────────────────│   archived   │◀───────────────│ (仍保留原文) │
      RESTORE           └──────┬───────┘                └──────────────┘
                               │ DELETE                 DELETE ▲
                               ▼                               │
                        ┌──────────────┐─────────────────────┘
                        │   deleted    │  RESTORE ──▶ archived/active
                        └──────┬───────┘
                               │ PURGE（物理删除，二次确认，不可恢复）
                               ▼
                              ∅
```

### 2.1 合法转移表（穷尽，代码里照此实现）

| # | 从 | 到 | 触发 | 谁发起 | 改动字段 | 时间轴 action |
| --- | --- | --- | --- | --- | --- | --- |
| T1 | ∅ | created→active | 新事实通过 Value Gate 且无冲突 | 管线 | 全字段；`status='active'` | `add` |
| T2 | active | updated→active | 同一条事实被修正/细化 | `memory_update` / 引擎判"细化" | `content`/`keywords`/`updated_at` | `update` |
| T3 | active | updated→active | 重复写入（等价事实再次出现） | 管线 TOUCH | `updated_at`、`confidence=max` | `touch` |
| T4 | active | superseded | 事实不再成立，被新事实取代 | `memory_supersede` / 引擎判互斥 | `status`、`valid_until=now`、`superseded_by` | `supersede` |
| T5 | active | archived | **自然结束**（目标达成、项目结项、环境换了） | 管线 / 惰性维护 | `status`、`valid_until=now`（若未填） | `archive` |
| T6 | active | deleted | 用户要求忘掉 / 隐私 / 明确错误 | `memory_forget` / 管理界面 | `status`、`metadata.deleted_reason` | `delete` |
| T7 | superseded | archived | 历史价值期满（默认 180 天，惰性维护） | 维护任务 | `status` | `archive` |
| T8 | archived | active | **恢复**：事实重新成立（"那个项目又开始了"） | `memory_restore` / 管理界面 | `status='active'`、`valid_until=NULL`、`updated_at` | `restore` |
| T9 | archived | deleted | 用户要求忘掉 | `memory_forget` | `status` | `delete` |
| T10 | superseded | deleted | 用户要求忘掉（连同历史一起？不——只删这一条） | `memory_forget` | `status` | `delete` |
| T11 | deleted | active / archived | **恢复**（误删） | `memory_restore` | `status` | `restore` |
| T12 | 任意非 `deleted` | ∅ | **物理清除**，仅 `purge()` | 管理界面二次确认 / `memory_purge` | 行消失 + 级联 `memory_change` 保留（脱敏） | `purge` |

### 2.2 非法转移（必须被拒绝，且报错要能说人话）

| 从 | 到 | 为什么非法 | 正确做法 |
| --- | --- | --- | --- |
| **superseded → active** | 复活旧事实 | 历史会自相矛盾（同一槽位两个"当前态"） | **新增一条** `ADD`，`metadata.supersedes=旧id`；旧的保持 superseded |
| **deleted → superseded / archived** | 已删除的东西不该再参与状态演进 | 删除是用户意志，不能被系统流程"升级" | 先 `restore` 回 active，再走正常流程 |
| **created → superseded / deleted** | 瞬时态不能跳过 active | 逻辑上不存在 | 先落 active（同一事务），再在同一事务里转 |
| **archived → superseded** | 归档条目本就不参与现状 | 若确实被取代，先 restore 再 supersede，或直接删 | 见 T8 |
| **任何 → created** | 没有 time travel | — | — |

**报错示例**（P7 要照这个写）：
```
拒绝：记忆 m-aaa1 当前为 superseded，不能回到 active。
同一件事的"现在"只能有一条。若用户改主意，请新增一条（memory_remember 带 supersedes=m-aaa1）。
```

---

## 3. 完整示例：Python → TypeScript

任务书点名的第二个场景（第一个是 P3 的考研→不考研）。它比考研更硬，因为**改变的是领域事实，不是用户状态**。

### 3.1 时间线

| # | 事件 | 管线动作 |
| --- | --- | --- |
| T1 | 用户："这个项目后端用 Python 写的" | `ADD` → `M1`：`domain/fact`，「项目后端使用 Python」，`domain_key='code.proj'`，`policy='contextual'`，`confidence=0.95` |
| T2 | 用户："Python 3.11" | 同槽位检索到 M1，判为**细化**（同一事实补细节，不冲突）→ `UPDATE M1`：content 改为「项目后端使用 Python 3.11」 |
| T3 | 用户："算了，全迁到 TypeScript 了" | 同槽位存在 M1，内容互斥（Python vs TypeScript 是同一属性的不同取值）→ `SUPERSEDE` |

### 3.2 T3 之后的数据

```jsonc
// M1 —— 旧的，保留但退出使用
{
  "id": "m-p1", "type": "domain", "subtype": "fact",
  "subject_type": "project", "subject_name": "proj", "domain_key": "code.proj",
  "content": "项目后端使用 Python 3.11。",
  "confidence": 0.95, "importance": "important",
  "status": "superseded",
  "valid_from": "2026-03-01T00:00:00Z",
  "valid_until": "2026-11-14T03:00:00Z",     // ← 被取代的时刻
  "superseded_by": "m-p2",
  "created_at": "2026-03-01T10:00:00Z",
  "updated_at": "2026-11-14T03:00:00Z"
}
// M2 —— 新的当前态
{
  "id": "m-p2", "type": "domain", "subtype": "fact",
  "subject_type": "project", "subject_name": "proj", "domain_key": "code.proj",
  "content": "项目后端已全部迁移到 TypeScript。",
  "confidence": 0.95, "importance": "important",
  "status": "active",
  "valid_from": "2026-11-14T03:00:00Z",
  "valid_until": null, "superseded_by": null,
  "created_at": "2026-11-14T03:00:00Z",
  "updated_at": "2026-11-14T03:00:00Z",
  "metadata": "{\"supersedes\":\"m-p1\",\"reason\":\"用户明确说已迁移\"}"
}
```

`memory_change` 三条：
```
1 | m-p1 | supersede | old_snapshot=M1前态 | new_snapshot=M1后态 | reason="项目后端从 Python 迁移到 TypeScript" | actor=model_tool | src=evt-000841
2 | m-p2 | add       | null                | new_snapshot=M2      | reason="记录迁移后的技术栈"               | actor=model_tool | src=evt-000841
3 | m-p1 | link      | ...                 | ...                  | reason="m-p1 被 m-p2 取代"                | actor=system    | src=null
```

### 3.3 为什么不是 UPDATE

如果 T3 走 `UPDATE M1`（把 content 改成"用 TypeScript"），会发生什么：

1. **`id` 不变，历史断链**：三个月后用户问"这个项目以前用什么"，`explain(m-p1)` 只能看到"现在是 TypeScript"+一条 update 记录——**旧值还在 `old_snapshot` 里，但语义上丢了**（引擎不知道"Python"是一个被否定的选项，还是一个笔误修正）。
2. **没法表达"这是一次迁移"**：迁移是有时间点的、影响后续所有回答的事件；笔误修正是无时间的。两者的差别必须在数据里，而不是在 reason 文本里。
3. **检索会漏掉关键信息**：用户问"为什么当初选 Python"，`UPDATE` 之后的库里没有任何 `active` 条目提到 Python，只有历史快照。`SUPERSEDE` 保留了 M1 整行，`memory_search("Python")` 能把 M1 作为"已过时"结果带出来（带标注），这才是对的。

**一句话**：`UPDATE` 改的是**说法**，`SUPERSEDE` 改的是**事实**。技术栈迁移是事实变了。

### 3.4 边界情况

| 情形 | 正确动作 | 错误动作 |
| --- | --- | --- |
| "Python 只是写脚本，后端还是 Python" | 不动作（用户澄清的是范围，不是否定） | 立刻 supersede |
| "一半迁完了，还有一半 Python" | `ADD` 一条并行事实（`metadata.partial=true`），M1 改 `UPDATE` 缩小范围 | 把 M1 标 superseded（事实还有一半成立） |
| "又迁回 Python 了" | `ADD M3`（`supersedes=m-p2`），**不复活 M1** | 把 M1 改回 active |
| 用户从没说过迁移，模型自己"猜"项目换了技术栈 | **不许**（推断不能 supersede 显式记忆，P3 §4） | —— |

---

## 4. 可解释性：四个问题必须有答案

用户/开发者会问的四个问题，以及它们分别落在哪：

| 问题 | 数据来源 | 示例回答 |
| --- | --- | --- |
| **为什么变？** | `memory_change.reason` + 新条目的 `metadata.reason` | "因为你在 11 月 14 日说'全迁到 TypeScript 了'" |
| **何时变？** | `memory_change.at`（精确到秒）+ 旧条目的 `valid_until` | "2026-11-14 03:00:00 UTC" |
| **什么触发？** | `memory_change.actor` + `source_message_id` | "由模型工具 `memory_supersede` 触发，来源消息 evt-000841" |
| **旧版/新版各是什么？** | `memory_change.old_snapshot` / `new_snapshot`（整行 JSON） | 可逐字对比 |

### 4.1 `explain(id)` 的输出契约（P7 实现，P9 展示）

```jsonc
{
  "id": "m-p1",
  "current": { /* 当前整行 */ },
  "state_reason": "这条记忆已被 m-p2 取代：项目后端从 Python 迁移到了 TypeScript。",
  "timeline": [
    { "at": "2026-03-01T10:00:00Z", "action": "add",       "actor": "model_tool",  "source_message_id": "evt-000120", "summary": "创建：项目后端使用 Python 3.11。" },
    { "at": "2026-04-02T09:00:00Z", "action": "update",    "actor": "model_tool",  "source_message_id": "evt-000233", "summary": "补充版本号：Python → Python 3.11。" },
    { "at": "2026-11-14T03:00:00Z", "action": "supersede", "actor": "model_tool",  "source_message_id": "evt-000841", "summary": "被 m-p2 取代：用户说已全部迁移到 TypeScript。" }
  ],
  "versions": [
    { "at": "2026-03-01T10:00:00Z", "content": "项目后端使用 Python。" },
    { "at": "2026-04-02T09:00:00Z", "content": "项目后端使用 Python 3.11。" }
  ],
  "related": [ { "id": "m-p2", "relation": "superseded_by", "content": "项目后端已全部迁移到 TypeScript。" } ]
}
```

**`state_reason` 是生成的一句话，不是存下来的字段**——它由 `status` + 关联条目 + 最近一条 change 拼出来。存它等于存副本（违反约束 A 的精神：可推导的东西不物化）。

### 4.2 可解释性的最低保证（P7 测试断言）

| 保证 | 断言 |
| --- | --- |
| 每条记忆至少有一条 `memory_change` | `SELECT count(*) FROM memory_change WHERE memory_id=?` ≥ 1 |
| 每次状态变化都有一行 change | `status` 变化前后必对应一条 `supersede/archive/delete/restore` |
| `old_snapshot` 能还原旧内容 | 取最新一条 `action='update'` 的 `old_snapshot.content` 等于改之前的 `content` |
| `source_message_id` 可追溯 | 非 `system` actor 的 change 必须带 `source_message_id` |

---

## 5. 自动转换（惰性维护，V1 只做这几件）

参考实现的经验：**自动遗忘一旦误判就是不可逆的数据损失**。所以 V1 的自动转换范围被压到最小，且全部可解释。

| 转换 | 触发 | 为什么安全 |
| --- | --- | --- |
| T5：`active` state 过期 → `archived` | `type='state' AND valid_until < now AND status='active'`，且已持续 7 天 | 事实自己写明了"到某天为止"，归档只是承认它 |
| T7：`superseded` 满 180 天 → `archived` | `status='superseded' AND updated_at < now-180d` | 被取代的旧值，半年后不再需要参与检索（但仍可 explain） |

**明确不做的自动转换**：
- profile/domain 长期未命中 → 归档（"自动遗忘"，红线）
- 合并"看起来重复"的条目（不可逆归并，红线）
- 降低 `confidence` 或 `importance`（悄悄改语义，红线）
- 删除任何东西

**执行方式**：不用 `setInterval`（宿主可能休眠/退出/连开几天）。改为**惰性**：每次 `add`/`search` 时检查 `memory_meta.last_maintenance_at`，超过 7 天才跑一次，跑完写回。失败也写回时间戳（否则每次写入都重试同一次失败）。

---

## 6. 删除的三层语义（别把"忘掉"做成"删库"）

| 层 | 操作 | 数据 | 可恢复 | 谁可以做 |
| --- | --- | --- | --- | --- |
| L1 退出使用 | `ARCHIVE` | 行在，`status='archived'` | 是（`restore`） | 管线自动 |
| L2 忘记 | `DELETE`（软删） | 行在，`status='deleted'`；检索完全不可见 | 是（`restore`） | 用户 / `memory_forget` |
| L3 物理清除 | `PURGE` | 行消失 | **否** | 仅管理界面，二次确认，且要求输入记忆 id |

**为什么 L2 不直接物理删**：用户说"忘掉"通常指"别再提"，不是"帮我做数据销毁"。软删立刻达到效果（检索不到、注入不到），又留了误删恢复的路。真正需要物理销毁的场景（隐私合规）走 L3，且要明确告知不可恢复。

**P9 的界面必须区分这三层**：`忘记` 按钮 = L2（默认），`永久删除` 藏在详情页的"危险操作"里 = L3。

---

## 7. P9 界面表现（生命周期在这里必须"看得见"）

| 界面位置 | 显示什么 | 为什么 |
| --- | --- | --- |
| 列表行 | 状态徽章：`生效`(绿) / `已取代`(灰) / `已归档`(蓝灰) / `已删除`(红，默认不显示) | 一眼看出哪些还在用 |
| 列表默认过滤 | 只显示 `active`（提供"显示全部"开关） | 默认视图要干净；历史是主动查看的东西 |
| 详情页顶部 | `状态：已取代 → 被 m-p2 取代`（可点击跳转） | 取代关系比状态词更重要 |
| 详情页时间轴 | `explain()` 的 `timeline` 渲染成竖排时间线，每条显示时间/动作/来源消息 | 这是"可解释"的正面呈现 |
| 详情页版本 | `versions` 列表，可逐版本对比（diff 视图） | 回答"以前是什么" |
| 操作按钮 | `编辑`(UPDATE) / `忘记`(DELETE) / `恢复`(RESTORE，仅非 active 时) / `永久删除`(PURGE，危险区) | 四个操作对应四种状态转移，不多不少 |
| 合并 | 仅当 `memory_find_similar` 返回 `merge_suggestion` 时出现 | MERGE 不自动，必须有人点 |

**一个刻意的设计**：详情页**不给"改状态"下拉框**。状态不是用户直接设置的东西，是操作的结果。给下拉框等于允许用户造出非法状态（比如把 superseded 拖回 active）。

---

## 8. 不变式与断言（P7 测试清单）

| # | 不变式 | 断言方式 |
| --- | --- | --- |
| 1 | `status` 只有四个合法值 | 插入 `status='created'` 必须失败 |
| 2 | `superseded` ⇒ `superseded_by` 非空 且 `valid_until` 非空 | 直接 UPDATE 造一条违反的，必须失败 |
| 3 | `superseded_by` 指向的条目 `status IN (active, archived)` | 造一条指向 superseded 的，必须失败 |
| 4 | 同一槽位最多一条 `active`（仅对互斥属性成立） | 考研→不考研后，同槽位 active 数 = 1 |
| 5 | `deleted` 的行检索不到 | `search()` 结果里不含它 |
| 6 | `deleted` 的行 `explain()` 仍可见 | 能查到删除理由与时间 |
| 7 | 非法转移被拒且报错可读 | `superseded → active` 抛出含"请新增一条"的提示 |
| 8 | 每次状态变化都有 change 行 | 转移后 `count(change)` 增加 |
| 9 | `purge` 后行消失、change 保留（脱敏） | `select * from memories where id=?` 为空；change 里 `old_snapshot` 为 `null` |

---

## 9. 自检清单（P6 完成判据）

| 判据 | 位置 |
| --- | --- |
| 六个节点都有定义，且与 P2 的四个 status 不冲突（并解释为什么） | §1 |
| 状态机图 + 穷尽的合法转移表（含触发条件与发起者） | §2.1 |
| 非法转移被显式拒绝并给出正确做法 | §2.2 |
| "Python → TypeScript" 示例完整（数据 + 三条 change + 为什么不是 UPDATE） | §3 |
| 可解释性四问都有数据落点，`explain()` 有输出契约 | §4 |
| 自动转换范围最小化，且每条都有"为什么安全" | §5 |
| 删除分三层，不把"忘掉"做成"删库" | §6 |
| P9 界面表现具体到每个控件 | §7 |
