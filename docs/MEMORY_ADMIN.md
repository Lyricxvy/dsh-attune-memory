# 记忆管理功能（P9）

> 目标不是"给数据库配个 CRUD 后台"，而是让用户能**看懂一条记忆为什么会是现在这样**，
> 并且能安全地纠正它。所以这里只有四个动作：看、改、忘记、恢复；"物理删除"藏在忘记之后
> 再做一次确认。状态不是可选项——它是事实变化的**结果**，不是用户在界面上挑的一个值。

---

## 0. 两个入口，一套能力

| 入口 | 位置 | 适合 |
| --- | --- | --- |
| 命令行 `memctl` | `engine/attune_memory/memctl.py`，`cd engine && python -m attune_memory.memctl <cmd>` | 脚本、排查、离线库 |
| 管理页 | `http://<GUI 地址>/dsh-attune-memory/`（插件路由 `/dsh-attune-memory`） | 浏览器里看时间轴、点着改 |

两者调用的是**同一组引擎 API**（`list / get / explain / update / forget / restore / purge / stats / tick`）。
页面不是第二套逻辑，只是第二层皮：HTTP 处理器逐个转成 `engine.call(op, args)`。

---

## 1. 操作矩阵

| 动作 | CLI | HTTP | 引擎 API | 说明 |
| --- | --- | --- | --- | --- |
| 列出 | `list [--status] [--type] [--domain] [--limit]` | `GET /api/list` | `list()` | 默认只列 `active` |
| 详情 | `get <id>` | `GET /api/get?id=` | `get()` | 原始行（字段全展示） |
| 解释 | `explain <id>` | `GET /api/explain?id=` | `explain()` | 时间轴 + 历史版本 + 关系 |
| 修改 | `update <id> --content ... --reason ...` | `POST /api/update` | `update()` | 同一件事换说法；事实变了要用 supersede |
| 忘记 | `forget <id> [--hard]` | `POST /api/forget {hard?}` | `forget()` / `purge()` | 默认软删，可恢复 |
| 恢复 | `restore <id>` | `POST /api/restore` | `restore()` | 只对 archived/deleted 有效 |
| 概况 | `stats` | `GET /api/stats` | `stats()` | 含不变式自检结果 |
| 维护 | `tick` | `POST /api/tick` | `tick()` | 只做两条自动归档，绝不删 |

**没有**"把状态改成 superseded"的入口。原因见 `MEMORY_LIFECYCLE.md` §7：删掉状态下拉框，
是因为状态由写管线根据事实变化决定；允许手改状态，等于允许用户手工制造一条"被取代但没有
取代者"的孤条目，而那正是约束 C 要防的东西。

---

## 2. 手工演示（真实库副本）

演示库：把线上 `$DSH_HOME/attune-memory/memory.db` 复制到 `.verify/p9demo.db` 后操作，避免污染真实数据。

### 2.1 list —— 先看有什么

```text
$ python -m attune_memory.memctl --db .verify/p9demo.db list
id                        type      subtype       状态          重要性       注入
------------------------  --------  ------------  ----------  --------  ----------
m-mwf2bbj3-10             profile   identity      active      key       always      用户叫张三，正在备考考研数学一。
m-mwf2bbj4-2j             profile   interaction   active      key       always      张三不喜欢先给结论：回答问题要先讲思路/解题切入点，再给具体步骤和结论。
m-mwf2bx9m-1h             profile   skill         active      important contextual  张三自述做数学一极限题时"总是想不到变形方向"，属于结构性判断缺失…
m-mwf2qut0-1t             profile   preference    active      normal    contextual  张三（考研数学一）偏好把知识点做成"长篇、多例子、带判断流程"的系统性笔记…
```

### 2.2 explain —— 这条为什么是现在这样

```text
$ python -m attune_memory.memctl --db .verify/p9demo.db explain m-mwf2bbj3-10
条目 m-mwf2bbj3-10
  内容    : 用户叫张三，正在备考 2027 考研数学一（数一）。
  类型    : profile/identity  主体=user
  状态    : active（当前生效中。最近一次变更：新增（2026-11-13T14:18:44Z））
  注入策略: always   重要性: key   置信度: 0.95
  作用域  : global   领域: -
  有效期  : 2026-11-13T14:18:44Z → 现在
  被注入 0 次，被检索 11 次

变更历史（2 条）:
  2026-11-13T14:18:44Z  add  by user  理由: 新增
      content: None → '用户叫张三，正在备考考研数学一。'；status: None → 'active'；…
  2026-11-13T14:36:06Z  update  by tool  理由: 用户补充了年份与科目全称
      content: '用户叫张三，正在备考考研数学一。' → '用户叫张三，正在备考 2027 考研数学一（数一）。'

历史版本（1 次内容变更）:
  2026-11-13T14:36:06Z  用户叫张三，正在备考考研数学一。 → 用户叫张三，正在备考 2027 考研数学一（数一）。
```

四个问题一次答全：**现在是什么**（current）、**为什么是这个状态**（state_reason）、
**什么时候被谁改成这样**（timeline 的 `at`/`actor`/`reason`）、**旧版长什么样**（versions 的 from→to）。
这些数据全部来自 `memory_change` 流水与主表，`state_reason` 是唯一"现算"的文字（不落库，避免副本不一致）。

### 2.3 update —— 改一条，留痕

```text
$ python -m attune_memory.memctl --db .verify/p9demo.db update m-mwf2bbj3-10 \
    --content "用户叫张三，正在备考 2027 考研数学一（数一）。" --reason "用户补充了年份与科目全称"
已改 m-mwf2bbj3-10（active）：用户叫张三，正在备考 2027 考研数学一（数一）。
```

改完 `explain` 立刻多出一条 `update by tool` 的变更记录与一个历史版本——**改动本身也是可解释的**。

### 2.4 forget —— 软删，可恢复

```text
$ python -m attune_memory.memctl --db .verify/p9demo.db forget m-mwf2bx9m-1h --reason "用户说这条记岔了"
已忘记 m-mwf2bx9m-1h（状态 deleted，可用 restore 恢复）

$ python -m attune_memory.memctl --db .verify/p9demo.db list --status deleted
m-mwf2bx9m-1h  profile  skill  deleted  important  contextual  张三自述做数学一极限题时…

$ python -m attune_memory.memctl --db .verify/p9demo.db restore m-mwf2bx9m-1h
已恢复 m-mwf2bx9m-1h（状态 active）：张三自述做数学一极限题时"总是想不到变形方向"…
```

忘记之后这条记忆立刻从 `search`/`read_for_context` 里消失（`build_candidates` 只取 `active`），
但主表行与时间轴都还在——这就是"可纠正"与"不可逆"的分界线。

### 2.5 purge —— 物理删除是两步

```text
$ python -m attune_memory.memctl --db .verify/p9demo.db forget m-xxxx --hard
已物理删除 m-xxxx（不可恢复）
```

引擎层 `purge()` 只接受 `status='deleted'` 的条目；CLI 与 HTTP 的 `--hard` 都是"先 forget 再 purge"，
没人能一步从 active 直接抹掉。

---

## 3. 管理页

### 3.1 路由表

| 方法 | 路径 | 作用 |
| --- | --- | --- |
| GET | `/dsh-attune-memory/` | 单文件管理页（17KB 内联 HTML/CSS/JS，无构建步骤） |
| GET | `/dsh-attune-memory/api/list?status&type&limit` | 列表 |
| GET | `/dsh-attune-memory/api/get?id=` | 原始行 |
| GET | `/dsh-attune-memory/api/explain?id=` | 时间轴 + 版本 + 关系 |
| GET | `/dsh-attune-memory/api/stats` | 概况 |
| POST | `/dsh-attune-memory/api/update` | `{id, content?, importance?, injection_policy?, confidence?, reason?}` |
| POST | `/dsh-attune-memory/api/forget` | `{id, reason?, hard?}` |
| POST | `/dsh-attune-memory/api/restore` | `{id}` |
| POST | `/dsh-attune-memory/api/tick` | 跑一次惰性维护 |

### 3.2 页面结构

左栏：状态筛选（生效中 / 已归档 / 已忘记 / 已被取代 / 全部）+ 类型筛选 + 列表（内容两行截断、
状态徽章、注入策略徽章）。右栏：内容编辑框、重要性/注入策略下拉、置信度、
修改理由输入；下面是"为什么是现在这样"、变更历史表、历史版本表、关系列表。
按钮随状态变化：active 给"保存修改 + 忘记"，deleted/archived 给"恢复"，deleted 额外给"物理删除"。

### 3.3 信任栅栏（必须做对的一处）

```js
const code = connection.requestRejection(req);   // 过不去 → 403/401，直接结束
```

每个 `/api/*` 请求都先过栅栏（`dsh-client-connection/lib/index.js:737`）。**页面本身不过栅栏**：
它是纯静态 HTML，不含任何记忆数据；如果页面也被拦，用户从书签或新标签直达会拿到 401，
而页面本身泄露不了任何东西——数据都在栅栏后面。

实测（真实宿主，临时 profile `memweb` + 端口 41999）：

```text
GET /dsh-attune-memory/                        → 200, text/html, 17068 字节, 含"记忆库"
GET /dsh-attune-memory/api/list（无浏览器凭据） → 401
```

没有这一关，用户随手打开的任意网页都能 `fetch('http://127.0.0.1:55665/dsh-attune-memory/api/...')`
读写本机记忆库。

### 3.4 怎么打开

管理页挂在宿主已有的 HTTP 服务上，所以它跟 GUI 同源、同端口、同一份 Cookie：

```
http://127.0.0.1:55665/dsh-attune-memory/
```

首次直连需要浏览器已经通过 GUI 的启动 URL（带 `?token=`）换到过会话 Cookie；正常使用 GUI 的
浏览器里已经有这个 Cookie，直接打开即可。

---

## 4. "不暴露裸 SQL"是怎么保证的

不是靠自觉，是靠四道闸：

1. **RPC 白名单**：`engine/attune_memory/rpc.py` 的 `_SPEC` 逐 op 列出允许的参数，多传一个参数直接报错
   （`search 不接受这些参数：['lt']；可用：[...]`）。静默忽略会让调用方以为生效了。
2. **字段白名单**：`write.update()` 只允许改 `content/keywords/importance/injection_policy/confidence/
   domain_key/scope/metadata/subject_name/valid_until`；`type/subtype/subject_type` 变了必须新增一条。
3. **枚举前置校验**（P9 修的真实缺陷）：以前传 `importance="super-high"` 会一路走到 SQLite，
   用户看到的是 `CHECK constraint failed: importance IN ('key','important','normal','low')`——
   这既是裸表结构泄露，用户也看不懂。现在在 `update()` 里先校验，报
   `importance 只能是 key/important/normal/low 之一，收到 'super-high'`。
4. **错误分类**：`CandidateError`/`InvariantError`/`RpcError`/`KeyError` 直接给原因、不附 traceback；
   其它异常才带 traceback（那是真 bug，需要栈）。`get` 查不到返回 `null` 而不是抛错，
   这样 HTTP 层能正确回 404 而不是 400。

---

## 5. 验证记录

| 项目 | 命令 | 结果 |
| --- | --- | --- |
| 引擎单测 | `cd engine && python -m unittest discover -s tests -q` | `Ran 72 tests … OK` |
| 路由层（假 ctx + 真引擎） | `node .verify/p9-routes-test.mjs` | 9 项全通过 |
| 真实宿主挂载 | `dsh memweb --port 41999` + `GET /dsh-attune-memory/` | 200 / 17068 字节 |
| 真实宿主栅栏 | `GET /dsh-attune-memory/api/list`（无 Cookie） | 401 |
| CLI 演示 | 见 §2 | list/get/explain/update/forget/restore/stats 全通 |

路由层 9 项覆盖：页面可直开、API 挡不可信来源、列表、详情+解释、修改留版本、非法字段被拒并回告
合法值、忘记后可恢复、物理删除后 404、未知路由 404。

### 5.1 v1.1 新增的观测出口

| 看什么 | CLI | HTTP | 说明 |
| --- | --- | --- | --- |
| 计数 | `memctl telemetry` | `GET /api/stats` 的 `counters` | 写入候选/闸门拒绝/搜索候选/命中/注入条数与字符/`rpc_errors`/`supersede_low_confidence` |
| 最近检索 | `memctl reads --limit 10` | `GET /api/reads` | 每次查询的"候选 N → 命中 M → 注入 K"、各桶条数、被结构化过滤丢掉的原因 |
| 被丢弃的候选 | `memctl discarded --limit 10` | `GET /api/discarded` | 前 120 字 + 原因；`memctl discarded --purge` 一次清空 |
| 维护扫描 | `memctl scan --limit 500` | —（CLI/RPC） | 重复 / 可能矛盾 / 同槽多位 / 已过期 / 不变式问题五张清单；**只读，不改数据** |
| 记忆在不在模型视野 | 让模型调 `memory_probe` | —（工具） | `inject_visible` / `inject_faded_compaction` / `inject_faded_unexplained` |

管理页顶栏的"诊断"按钮把前两项 + 丢弃候选放在一个浮层里；统计行会显示
"注入 N 次，其中 M 次无解释消失 ⚠"——`M > 0` 通常意味着请求被某个代理层重写过。

---

## 6. 装进桌面 profile（要让主 GUI 里能用）

管理页随插件一起装；桌面 profile 被 Electron 独占，改完**必须重启应用**：

```powershell
$p = "$env:DSH_HOME\profiles\desktop"
cmd /c mklink /J "$p\node_modules\dsh-attune-memory" "<REPO_ROOT>\plugin\dsh-attune-memory"
# 然后手工把 "dsh-attune-memory" 加进 package.json 的 dependencies 与 dsh.profile.bundles，
# 注意用 UTF8 无 BOM 写回：
#   [System.IO.File]::WriteAllText($path, $json, (New-Object System.Text.UTF8Encoding($false)))
```

重启后打开 `http://127.0.0.1:55665/dsh-attune-memory/`。

---

## 7. 已知限制

- **页面没有搜索框**。V1 的检索是"按当前对话"设计的，人肉搜索时直接按状态/类型筛更可靠；
  自由查询留到 V2（等 Bigram/FTS5）。
- **不能编辑 `valid_until`**。过期时间现在由写管线按 state 语义设定，暴露成输入框只会让人手填出
  互相矛盾的区间。要改就 supersede 一条新的。
- **列表一次最多 500 条**。V1 的量级（几十到几百条）够用；再多说明该整理了，而不是该分页了。
- **`tick` 不会告诉你"哪些条目快过期了"**。它只做两条自动归档；主动提醒属于 V2 的 Dream/维护。
