/**
 * T-007：端到端穿透测试（模型工具 → JS 客户端 → Python handle → 引擎）。
 *
 * 三类断言：
 *   1. **契约矩阵**：`describe` 里的每个 op 都跑一次 happy path，再各跑两类非法输入
 *      （缺必填 / 多传未知参数）。整张矩阵是**从 describe 派生**的 —— 新增 op 不用改测试，
 *      但新增 op 如果少了非法参数处理，这里就会红。
 *   2. **反向验证**：把引擎包目录（源码树 `memory/`、发布树 `attune_memory/`）复制到临时目录、
 *      删掉 `OPS["preview"]`，同一条
 *      happy-path 断言必须抛错。它证明"绿灯"不是因为断言没跑。
 *   3. **回归**：`explicit` 两条路径、`forget` 无 `hard`、`/api/reads` 与
 *      `/api/discarded` 走真实引擎（T-005 / T-003 修过的点）。
 *
 * 跑法：node plugin/dsh-attune-memory/test/e2e.test.mjs
 * （不要用 node --test：本沙箱里 test runner 起子进程会 EPERM）
 */
import assert from 'node:assert/strict'
import { cpSync, existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join, resolve } from 'node:path'

import { EngineClient } from '../lib/engine.js'
import { ENGINE_DIR, MODULE, PKG_DIR as ENGINE_PKG_DIR, PKG_NAME as ENGINE_PKG_NAME } from './lib/engine-pkg.mjs'
import { registerRoutes } from '../lib/routes.js'
import { buildTools } from '../lib/tools.js'

// 包目录名/模块名只从 shared 探测点派生（T-016：同一类改名坑踩了三次）
const PKG_NAME = ENGINE_PKG_NAME
const PKG_DIR = ENGINE_PKG_DIR
const PY = process.env.ATTUNE_TEST_PYTHON || 'python'
// 用系统临时目录：engine/_probe/tmp 在 Windows 上偶发 EPERM（目录被前一次子进程占着）。
const TMP = join(tmpdir(), 'attune-e2e')
mkdirSync(TMP, { recursive: true })

const cases = []
function test(name, fn) {
  cases.push({ name, fn })
}

function tmpDb(tag) {
  const dir = mkdtempSync(join(TMP, `e2e-${tag}-`))
  return {
    db: join(dir, 'memory.db'),
    // Windows 上子进程可能还占着 db 文件（引擎刚 close）；删不掉就留给系统回收，
    // 绝不能因为清理失败把已经通过的断言判成失败。
    cleanup: () => {
      try {
        rmSync(dir, { recursive: true, force: true, maxRetries: 3 })
      } catch {
        /* ignore */
      }
    },
  }
}

function newClient(db, overrides = {}) {
  // 模块名跟着实际包目录走（源码树与发布树的包名不同，见 PKG_DIR）。
  return new EngineClient({ python: PY, engineDir: ENGINE_DIR, engineModule: MODULE, dbPath: db, timeoutMs: 8000, ...overrides })
}

/** 一次 happy path 的"调用并断言成功"助手。 */
async function ok(client, op, args) {
  const result = await client.call(op, args)
  return result
}

// --------------------------------------------------------------------- 1. 矩阵
test('契约矩阵：每个 op 的 happy path + 两类非法输入', async () => {
  const { db, cleanup } = tmpDb('matrix')
  const client = newClient(db)
  try {
    const ops = await client.call('describe', {})
    const byName = new Map(ops.map((row) => [row.op, row]))
    assert.equal(ops.length, 27, `describe 应返回 27 个 op，实际 ${ops.length}`)
    assert.ok(byName.has('preview'), 'describe 必须包含 preview（T-004 的回归点）')

    // ---- 种子数据 ----
    const seeded = await ok(client, 'add', {
      content: '用户正在备考 2027 年考研数学一。', type: 'state', subtype: 'goal',
      confidence: 0.9, explicit: true,
    })
    const id1 = seeded.memory.id
    const superseded = await ok(client, 'supersede', {
      id: (await ok(client, 'add', {
        content: '用户的英语一目标是 70 分。', type: 'state', subtype: 'goal',
        confidence: 0.9, explicit: true,
      })).memory.id,
      content: '用户的英语一目标是 80 分。', reason: 'T-007',
    })
    assert.equal(Number(superseded.memory.confidence), 0.9,
      '显式取代出来的新条不该被推断降级压到 0.7')

    const seeded2 = await ok(client, 'add', {
      content: '用户习惯先看思路再看步骤。', type: 'profile', subtype: 'interaction',
      confidence: 0.9, explicit: true,
    })
    const id2 = seeded2.memory.id

    const happy = {
      describe: () => ok(client, 'describe', {}),
      add: () => ok(client, 'add', { content: '用户叫李四。', type: 'profile', subtype: 'identity' }),
      preview: () => ok(client, 'preview', { content: '用户叫李四。', type: 'profile', subtype: 'identity' }),
      update: () => ok(client, 'update', { id: id1, fields: { content: '用户正在备考 2027 年考研数学一（数一）。' }, reason: 'T-007' }),
      supersede: () => ok(client, 'supersede', { id: id1, content: '用户已放弃考研，准备直接工作。', reason: 'T-007' }),
      merge: () => ok(client, 'merge', { ids: [id2, id2], content: '用户习惯先看思路再看步骤。', reason: 'T-007' }),
      archive: () => ok(client, 'archive', { id: id2, reason: 'T-007' }),
      delete: () => ok(client, 'delete', { id: id2, reason: 'T-007' }),
      forget: () => ok(client, 'forget', { id: id2, reason: 'T-007' }),
      restore: () => ok(client, 'restore', { id: id2, reason: 'T-007' }),
      purge: async () => {
        const row = await ok(client, 'add', { content: '待物理删除的测试条目。', type: 'episodic', subtype: 'event' })
        await ok(client, 'forget', { id: row.memory.id, reason: 'T-007' })
        return ok(client, 'purge', { id: row.memory.id, reason: 'T-007' })
      },
      get: () => ok(client, 'get', { id: id1 }),
      explain: () => ok(client, 'explain', { id: id1 }),
      list: () => ok(client, 'list', { status: 'active', limit: 20 }),
      search: () => ok(client, 'search', { query: '考研', limit: 5 }),
      find_similar: () => ok(client, 'find_similar', { content: '用户正在备考考研数学一。' }),
      read_for_context: () => ok(client, 'read_for_context', { query: '数学一怎么复习', domain: 'study.math' }),
      recent: () => ok(client, 'recent', { since: '2000-01-01T00:00:00Z', limit: 10 }),
      stats: () => ok(client, 'stats', {}),
      tick: () => ok(client, 'tick', {}),
      scan: () => ok(client, 'scan', { limit: 50 }),
      record: () => ok(client, 'record', { counters: { e2e_hits: 1 }, observation: { kind: 'read', subject: 'e2e', detail: 'T-007' } }),
      metrics: () => ok(client, 'metrics', { limit: 5 }),
      telemetry: () => ok(client, 'telemetry', { limit: 5 }),
      reads: () => ok(client, 'reads', { limit: 5 }),
      discarded: () => ok(client, 'discarded', { limit: 5 }),
      purge_observations: () => ok(client, 'purge_observations', { kind: 'read' }),
    }
    assert.deepEqual([...Object.keys(happy)].sort(), [...byName.keys()].sort(),
      'happy-path 表与 describe 的 op 集合必须一一对应（新增 op 时忘了补测试会在这里红）')

    for (const op of byName.keys()) {
      // happy path
      await happy[op]()
      // 非法输入一：多传未知参数
      await assert.rejects(
        () => client.call(op, { __nonsense: 1 }),
        (error) => /不接受这些参数/.test(error.message),
        `${op} 应拒绝未知参数，实际没拒`,
      )
      // 非法输入二：缺必填（只有真的声明了 required 的 op 才有这一条）
      const required = byName.get(op).required
      if (required.length > 0) {
        await assert.rejects(
          () => client.call(op, {}),
          (error) => /缺少必填参数/.test(error.message),
          `${op} 应报缺必填参数（required=${JSON.stringify(required)}）`,
        )
      }
    }
  } finally {
    client.close()
    cleanup()
  }
})

// ----------------------------------------------------------- 2. 反向验证（删 preview）
test('反向验证：删掉 OPS["preview"] 后同一条断言必须抛错', async () => {
  const dir = mkdtempSync(join(TMP, 'e2e-sabotage-'))
  // 临时副本的目录名与模块名都跟随 PKG_DIR.name：发布树里改名流水线会把模块引用
  // Python 包名改成 attune_memory，若这里硬写目录名或模块名，两侧就会对不上
  // （T-010 真红过：cwd 下只有 memory/ 却 spawn `-m attune_memory.<模块>`）。
  const pkgCopy = join(dir, PKG_NAME)
  cpSync(PKG_DIR, pkgCopy, { recursive: true })
  const rpcPath = join(pkgCopy, 'rpc.py')
  const source = readFileSync(rpcPath, 'utf8')
  const start = source.indexOf('    "preview": OpSpec(')
  const end = source.indexOf('    "update": OpSpec(')
  assert.ok(start > 0 && end > start, '在 rpc.py 里定位 OPS["preview"] 失败，反向测试失效')
  writeFileSync(rpcPath, source.slice(0, start) + source.slice(end), 'utf8')

  const db = join(dir, 'sabotage.db')
  const client = new EngineClient({
    python: PY,
    engineDir: dir,
    engineModule: MODULE,
    dbPath: db,
    timeoutMs: 8000,
  })
  let sawUnknown = false
  try {
    const ops = await client.call('describe', {})
    assert.ok(!ops.some((row) => row.op === 'preview'), '破坏后的引擎不该还有 preview')
    assert.equal(ops.length, 26, `破坏后应为 26 个 op，实际 ${ops.length}`)

    // 这就是"绿灯是真的"的证明：同一条 happy-path 断言在这里必然失败。
    await assert.rejects(
      () => client.call('preview', { content: '用户叫李四。', type: 'profile', subtype: 'identity' }),
      (error) => {
        sawUnknown = /未知操作/.test(error.message)
        return sawUnknown
      },
      '删掉 preview 之后，preview 调用必须报未知操作',
    )
  } finally {
    client.close()
    try {
      rmSync(dir, { recursive: true, force: true, maxRetries: 3 })
    } catch {
      /* ignore */
    }
  }
  assert.ok(sawUnknown, '反向验证没有观察到「未知操作」——测试可能没打到点上')
})

// ---------------------------------------------------- 3. 回归 T-005：explicit 两条路径
test('回归：explicit 省略=推断（0.7/contextual），显式=true 保留高置信', async () => {
  const { db, cleanup } = tmpDb('explicit')
  const client = newClient(db)
  try {
    const inferred = await client.call('add', {
      content: '用户好像更喜欢简洁的回答。', type: 'profile', subtype: 'preference',
      confidence: 0.95, injection_policy: 'always',
    })
    assert.equal(Number(inferred.memory.confidence), 0.7, '省略 explicit 时置信度应压到 0.7')
    assert.equal(inferred.memory.injection_policy, 'contextual', '省略 explicit 时 always 应降级')

    const explicit = await client.call('add', {
      content: '用户要求叫他「哥哥」。', type: 'profile', subtype: 'interaction',
      confidence: 0.95, injection_policy: 'always', explicit: true,
    })
    assert.equal(Number(explicit.memory.confidence), 0.95, '显式 explicit 时应保留 0.95')
    assert.equal(explicit.memory.injection_policy, 'always', '显式 explicit 时 always 应保留')
  } finally {
    client.close()
    cleanup()
  }
})

// ------------------------------------------- 4. 回归 T-005：forget 的 hard 已经不存在
test('回归：forget 不接受 hard（模型侧物理删除通道已关闭）', async () => {
  const { db, cleanup } = tmpDb('forget')
  const client = newClient(db)
  try {
    const row = await client.call('add', { content: '用户叫王五。', type: 'profile', subtype: 'identity' })
    await assert.rejects(
      () => client.call('forget', { id: row.memory.id, hard: true, reason: 'T-007' }),
      (error) => /不接受这些参数/.test(error.message),
      'forget 的 hard 必须已被删除',
    )
    // 软删仍然可用，且软删后能 restore
    await client.call('forget', { id: row.memory.id, reason: 'T-007' })
    const restored = await client.call('restore', { id: row.memory.id, reason: 'T-007' })
    assert.equal(restored.status, 'active')

    // 物理删除只能走 purge，且必须先 deleted
    const row2 = await client.call('add', { content: '用户叫赵六。', type: 'profile', subtype: 'identity' })
    await assert.rejects(
      () => client.call('purge', { id: row2.memory.id, reason: 'T-007' }),
      (error) => /必须先 forget/.test(error.message),
      'purge 必须先软删',
    )
  } finally {
    client.close()
    cleanup()
  }
})

// ------------------------------------- 5. 回归 T-003：两条诊断 API 走真实引擎返回 200
test('回归：/api/reads 与 /api/discarded 返回 200 且有数据', async () => {
  const { db, cleanup } = tmpDb('routes')
  const client = newClient(db)
  await client.call('read_for_context', { query: '数学一极限题怎么复习', domain: 'study.math' })
  // 造一条被丢弃的候选，让 /api/discarded 有内容
  await client.call('add', { content: '今天天气不错，适合晒被子。', type: 'episodic', subtype: 'event' })

  let handler
  const ctx = {
    webServer: { register(options) { handler = options.handler; return () => {} } },
    connection: { requestRejection: () => undefined },
    get: () => undefined,
  }
  registerRoutes(ctx, { engine: client, log: { warn() {} } })

  const call = async (sub) => {
    const chunks = []
    const res = {
      statusCode: 200,
      headers: {},
      setHeader(k, v) { this.headers[k.toLowerCase()] = v },
      end(body) { if (body !== undefined) chunks.push(Buffer.from(String(body), 'utf8')) },
    }
    await handler({ method: 'GET', url: `/dsh-attune-memory${sub}`, async *[Symbol.asyncIterator]() {} }, res)
    return { status: res.statusCode, json: JSON.parse(Buffer.concat(chunks).toString('utf8') || '{}') }
  }

  try {
    const reads = await call('/api/reads?limit=5')
    assert.equal(reads.status, 200, `reads 状态码应为 200，实际 ${reads.status}`)
    assert.ok(Array.isArray(reads.json.rows), 'reads 应返回 rows 数组')

    const discarded = await call('/api/discarded?limit=5')
    assert.equal(discarded.status, 200, `discarded 状态码应为 200，实际 ${discarded.status}`)
    assert.ok(Array.isArray(discarded.json.rows), 'discarded 应返回 rows 数组')
  } finally {
    client.close()
    cleanup()
  }
})

// ------------------------------------- 6. 工具 schema：preview.keywords 是数组（STATUS §5.4②）
test('工具 schema：memory_preview 的 keywords 是数组', () => {
  const tools = buildTools({ async call() {} }, {})
  const preview = tools.find((tool) => tool.name === 'memory_preview')
  assert.ok(preview, 'memory_preview 存在')
  assert.equal(preview.parameters.properties.keywords.type, 'array', 'keywords 应为数组而非字符串')
})

// ---------------------------------------------------------------------- 运行器
let failed = 0
for (const c of cases) {
  try {
    await c.fn()
    console.log(`ok   ${c.name}`)
  } catch (error) {
    failed += 1
    console.log(`FAIL ${c.name}\n     ${error?.message ?? error}`)
  }
}
console.log(failed === 0 ? `\ne2e.test.mjs: ${cases.length} 组穿透测试全部通过` : `\n${failed} 组失败`)
// 插件侧的引擎子进程会挂住事件循环；断言跑完显式退出。
process.exit(failed === 0 ? 0 : 1)
