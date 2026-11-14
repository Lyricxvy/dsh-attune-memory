# 安装 dsh-attune-memory

两种用法：**只跑引擎**（独立进程，不碰 harness），或**接进 harness**（记忆才真正生效）。

---

## 0. 先决条件

| 需要 | 版本 | 说明 |
| --- | --- | --- |
| Python | ≥ 3.10（开发用 3.11） | 引擎只用标准库：`sqlite3` / `json` / `hashlib` / `subprocess` |
| Node.js | ≥ 22.19 | 插件是 ESM，且用了 `node:test`；DSH 内置的 Electron 运行时也满足 |
| DeepSeek Harness | 0.2.x（取证基准 0.2.2-rc.1） | 插件依赖的公开 API 见 `docs/DESIGN.md` 第 8 节 |

---

## 1. 先验证引擎能跑

```bash
cd engine
python -m unittest discover -s tests -v     # 期望全部 OK（条数以实际输出为准）
python -m eval.run_eval                     # 期望 场景 A/B/C/D/E：24/24 通过
```

手动试一条（会建一个临时库，不碰真实数据）：

```bash
python -m attune_memory.memctl --db :memory: stats
```

引擎也能脱离 harness 当库用：

```python
from attune_memory import Memory

m = Memory("./memory.db")
m.add("用户叫张三，正在备考考研数学一。", type="profile", subtype="identity",
      importance="key", injection_policy="always", confidence=0.95)
m.add("张三做数学一极限题时总是想不到变形方向。", type="profile", subtype="skill",
      domain_key="study.math")

print(m.read_for_context(query="数学一的极限题怎么突破？", domain="study.math"))
print(m.explain(m.list()[0]["id"]))
```

---

## 2. 接进 harness

> 桌面版 profile 由 Electron 独占，命令行 `dsh --profile desktop` 会被拒绝；所以改文件 + 重启应用。

### 2.1 放到插件目录并硬链接进 profile

把仓库放到一个长期不动的路径（下面用 `D:/tools/dsh-attune-memory` 举例）。

```powershell
$repo = "D:/tools/dsh-attune-memory"
$p    = "$env:DSH_HOME\profiles\desktop"       # 你实际在用的 profile
cmd /c mklink /J "$p\node_modules\dsh-attune-memory" "$repo\plugin\dsh-attune-memory"
```

`mklink /J` 是目录联接，改仓库代码立即生效，不必复制。没有管理员权限也可以用 `xcopy` 复制整个 `plugin\dsh-attune-memory`。

### 2.2 改 profile 的 package.json

在 `$p\package.json` 里加两处：

```json
{
  "dependencies": {
    "dsh-attune-memory": "file:D:/tools/dsh-attune-memory/plugin/dsh-attune-memory"
  },
  "dsh": {
    "profile": {
      "bundles": ["...原有的...", "dsh-attune-memory"]
    }
  }
}
```

**必须用 UTF-8 无 BOM 保存**，否则启动报 `SyntaxError: Unexpected token '\uFEFF'`。PowerShell 安全写法：

```powershell
$path = "$env:DSH_HOME\profiles\desktop\package.json"
$json = Get-Content $path -Raw -Encoding UTF8 | ConvertFrom-Json
# …在这里改 $json…
$text = $json | ConvertTo-Json -Depth 20
[System.IO.File]::WriteAllText($path, $text, (New-Object System.Text.UTF8Encoding($false)))
```

插件自己的 `cordis.patch.yml` 会 insert 一行 `dsh-attune-memory`，所以 **profile 的 `cordis.patch.yml` 不用改**；只有想改配置时才在里面覆盖：

```yaml
- id: dsh-attune-memory
  config:
    engineDir: "D:/tools/dsh-attune-memory/engine"
    dbPath: "D:/tools/dsh-attune-memory/data/memory.db"
```

### 2.3 重启应用

关掉 DeepSeek Harness 再打开。启动日志里应出现插件挂载，界面上：

- 模型侧工具表里出现 `memory_remember`、`memory_search` 等 8 个工具；
- 打开 `http://127.0.0.1:<GUI端口>/dsh-attune-memory/` 能进管理页（端口看 GUI 地址栏）。

### 2.4 配置项

| 键 | 必填 | 默认 | 说明 |
| --- | --- | --- | --- |
| `engineDir` | 是 | — | 引擎目录（含 `attune_memory/` 包）；也读环境变量 `DSH_ATTUNE_MEMORY_ENGINE_DIR` |
| `dbPath` | 否 | `$DSH_HOME/attune-memory/memory.db` | SQLite 文件，目录不存在会自动建 |
| `python` | 否 | `python` | 解释器路径；也读 `DSH_ATTUNE_MEMORY_PYTHON` |
| `timeoutMs` | 否 | `5000` | 单次 RPC 超时 |
| `debug` | 否 | `false` | 打印每次 RPC 往来 |

---

## 3. 装完自检

1. 对模型说一句关于自己的话（"我叫张三，正在备考数学一"）。
2. 打开管理页，应看到 `profile/identity` 一条，状态 `active`。
3. 开一个新会话，问"你记得我叫什么吗" —— 答对说明 **S2 通道**（system prompt 稳定块）在跨会话生效。
4. 故意聊到很长让上下文压缩，再问一次同样的问题 —— 仍然答对说明压缩后重注入生效。

管理页的 `/api/*` 对未授权请求返回 401 是**正常**的（信任栅栏）；从 GUI 同一浏览器打开才有 Cookie。

---

## 4. 卸载

```powershell
$p = "$env:DSH_HOME\profiles\desktop"
cmd /c rmdir "$p\node_modules\dsh-attune-memory"        # 只删联接，不删仓库
# 再从 package.json 的 dependencies / dsh.profile.bundles 里删掉 dsh-attune-memory，重启应用
```

记忆库在 `$DSH_HOME/attune-memory/memory.db`，要彻底清除就删这个文件（先备份）。

---

## 5. 排错

| 现象 | 原因 / 处理 |
| --- | --- |
| 启动报 `Unexpected token '\uFEFF'` | package.json 带了 BOM，用 `UTF8Encoding($false)` 重写 |
| 工具全没了但日志说插件加载成功 | Cordis 只允许访问**声明过**的服务；检查 `lib/index.js` 的 `export const inject` |
| `MISSING_CREDENTIAL` | 这是 headless 验证 profile 的现象（默认关掉桌面凭据），正常桌面 profile 不会 |
| 中文乱码 / `UnicodeEncodeError: '\udca6'` | 引擎已强制 UTF-8；若自定义启动，确保 `PYTHONIOENCODING=utf-8` |
| 管理页 401 | 正常：用 GUI 同一浏览器打开，或从界面里的链接进入 |
| 记忆不增长 | 模型没主动调工具。让它在回答里带一句"记一下"，或检查 `turn/end` 兜底抽取是否命中句式 |

## 隐私

- 记忆全部存在本机 SQLite（默认 `$DSH_HOME/attune-memory/memory.db`），不上传。
- 两份"诊断"数据会包含用户原话片段：`memory_observation(kind='read')` 保存**查询文本的前 80 字**（最近 200 条），
  `kind='discarded'` 保存**被拒候选的前 120 字**（最长 14 天）。用 `memctl discarded --purge` 可一次清空。
- 不保存完整聊天记录：只保存被判定为长期有效的事实，以及上面两份有上限的诊断摘要。
