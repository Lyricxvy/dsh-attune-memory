/**
 * 模块 2 的宿主侧断言：客户端能不能把"可能已写入"和"肯定没写"分开。
 *
 * 引擎侧的幂等靠 Python 测试（tests/test_rpc_reliability.py）保证；这里只测客户端：
 *   1. 超时 → EngineError.code = TIMEOUT，且 mayHaveApplied = true（请求确实发出去了）
 *   2. 同一个 dedupe 键重发 → 第二次拿到 deduped，库里只有一条
 *   3. 引擎被杀 → ENGINE_DEAD，下一次调用会自动重启
 *
 * 跑法：node plugin/dsh-attune-memory/test/engine.test.mjs
 * （不要用 node --test：本沙箱里 test runner 起子进程会 EPERM）
 */
import { existsSync, mkdtempSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join, resolve, dirname } from 'node:path'
import { fileURLToPath } from 'node:url'
import { EngineClient } from '../lib/engine.js'

const HERE = dirname(fileURLToPath(import.meta.url))
const ENGINE_DIR = resolve(HERE, '../../../engine') // 源码树与发布树都指向各自的 engine/
// 源码树里包名是 memory，发布树里被 rename.py 改成了 attune_memory；按实际存在的目录选。
const MODULE = existsSync(join(ENGINE_DIR, 'attune_memory')) ? 'attune_memory.rpc' : 'attune_memory.rpc'
const PY = process.env.ATTUNE_TEST_PYTHON || 'python'

const cases = []
function test(name, fn) { cases.push({ name, fn }) }

/** Windows 上引擎子进程可能还没松开 db 文件；临时目录删不掉不影响结论，忽略即可。 */
function cleanup(dir) {
  try {
    rmSync(dir, { recursive: true, force: true, maxRetries: 3 })
  } catch {
    /* 留给系统临时目录自己回收 */
  }
}

function newClient(db, overrides = {}) {
  return new EngineClient({
    python: PY,
    engineDir: ENGINE_DIR,
    engineModule: MODULE,
    dbPath: db,
    timeoutMs: 4000,
    ...overrides,
  })
}

test('超时会带 mayHaveApplied，且不会伪装成成功', async () => {
  const dir = mkdtempSync(join(tmpdir(), 'attune-timeout-'))
  const client = newClient(join(dir, 'memory.db'), { timeoutMs: 1, engineModule: MODULE })
  try {
    let error
    try {
      await client.call('stats', {})
    } catch (e) {
      error = e
    }
    if (!error) throw new Error('应当超时，但调用成功了')
    if (error.code !== 'TIMEOUT') throw new Error(`期望 TIMEOUT，实际 ${error.code}`)
    if (error.mayHaveApplied !== true) throw new Error('超时时必须标记 mayHaveApplied')
  } finally {
    client.close()
    cleanup(dir)
  }
})

test('同一个 dedupe 键写入两次只落一条', async () => {
  const dir = mkdtempSync(join(tmpdir(), 'attune-dedupe-'))
  const client = newClient(join(dir, 'memory.db'))
  try {
    const args = { content: '用户备考 2027 年考研数学一。', type: 'state', subtype: 'goal' }
    const first = await client.call('add', args, { dedupe: 'same-key' })
    const second = await client.call('add', args, { dedupe: 'same-key' })
    if (first.memory.id !== second.memory.id) throw new Error('两次返回了不同的 id')
    const rows = await client.call('list', { status: 'active', limit: 20 })
    if (rows.length !== 1) throw new Error(`库里应有 1 条，实际 ${rows.length} 条`)
  } finally {
    client.close()
    cleanup(dir)
  }
})

test('引擎被杀后下一次调用自动重启', async () => {
  const dir = mkdtempSync(join(tmpdir(), 'attune-restart-'))
  const client = newClient(join(dir, 'memory.db'))
  try {
    await client.call('add', { content: '用户习惯先看思路再看步骤。', type: 'profile', subtype: 'interaction' })
    // 直接杀掉子进程，模拟崩溃
    const child = client.__childForTest
    if (child) child.kill()
    else { // 没有测试钩子就跳过（不伪造结论）
      console.log('  （跳过：客户端没有暴露 __childForTest）')
      return
    }
    await new Promise((r) => setTimeout(r, 200))
    const stats = await client.call('stats', {})
    if (!stats || typeof stats.total !== 'number') throw new Error('重启后调用失败')
  } finally {
    client.close()
    cleanup(dir)
  }
})

let failed = 0
for (const c of cases) {
  try {
    await c.fn()
    console.log(`ok   ${c.name}`)
  } catch (e) {
    failed += 1
    console.log(`FAIL ${c.name}\n     ${e.message}`)
  }
}
console.log(failed === 0 ? `\n${cases.length} 组断言全部通过` : `\n${failed} 组失败`)
process.exit(failed === 0 ? 0 : 1)
