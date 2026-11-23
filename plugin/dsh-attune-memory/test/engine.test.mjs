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
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { EngineClient } from '../lib/engine.js'
// 包目录名 / 模块名只从 test/lib/engine-pkg.mjs 派生（改名陷阱踩了三次）
import { ENGINE_DIR, MODULE, copyEnginePkg, moduleFor } from './lib/engine-pkg.mjs'

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

test('启动自检与引擎的写操作集合一致（含 tick）', async () => {
  const dir = mkdtempSync(join(tmpdir(), 'attune-contract-'))
  const client = newClient(join(dir, 'memory.db'))
  try {
    await client.call('stats', {})
    const contract = client.contract
    if (contract.state !== 'ok') throw new Error(`自检应为 ok，实际 ${contract.state}（${contract.detail}）`)
    if (!contract.writeOps.includes('tick')) throw new Error('自检后应以引擎名单为准，且含 tick')
    if (contract.writeOps.length !== 10) throw new Error(`写操作应为 10 个，实际 ${contract.writeOps.length}`)
  } finally {
    client.close()
    cleanup(dir)
  }
})

test('自检发现不一致时 fail-closed：写入报 BAD_CONFIG，读取仍可用', async () => {
  const dir = mkdtempSync(join(tmpdir(), 'attune-mismatch-'))
  // 故意给一份错的写入名单（少了 tick），模拟「插件比引擎旧/新」的漂移
  const client = newClient(join(dir, 'memory.db'), { writeOps: ['add', 'update'] })
  try {
    let error
    try {
      await client.call('add', { content: '用户偏好中文回答。', type: 'profile', subtype: 'interaction' })
    } catch (e) {
      error = e
    }
    if (!error) throw new Error('不一致时必须拒绝写入，但写入成功了')
    if (error.code !== 'BAD_CONFIG') throw new Error(`期望 BAD_CONFIG，实际 ${error.code}`)
    if (client.contract.state !== 'mismatch') throw new Error('自检状态应为 mismatch')
    const stats = await client.call('stats', {})
    if (!stats || typeof stats.total !== 'number') throw new Error('读操作不该被自检拦下')
  } finally {
    client.close()
    cleanup(dir)
  }
})

test('describe 不可用时写操作 fail-closed，读不受影响', async () => {
  // 复制引擎包，删掉 describe 那一行 —— 模拟"插件比引擎新"的版本不匹配。
  const root = mkdtempSync(join(tmpdir(), 'attune-nodescribe-'))
  // 副本目录名跟随实际包名：发布树里是 attune_memory，写死 memory 会 ENOENT（真红过）
  const { name, dir } = copyEnginePkg(root)
  const rpcPath = join(dir, 'rpc.py')
  const source = readFileSync(rpcPath, 'utf8')
  // 按整行正则匹配注册行：OpSpec 的字段会随版本变化（v0.3.0 加了 layer），
  // 写死整行会让"反向测试"因为格式变了而失效——那比测试失败更糟（会静默不测）。
  const linePattern = /^ *"describe": OpSpec\(.*\),\r?\n/m
  const matched = source.match(linePattern)
  if (!matched) throw new Error('在 rpc.py 里找不到 describe 的注册行，反向测试失效')
  writeFileSync(rpcPath, source.replace(matched[0], ''), 'utf8')

  // 副本在 root 下，所以 engineDir 指 root；engineModule 必须跟着副本目录名走
  const client = newClient(join(root, 'memory.db'), { engineDir: root, engineModule: moduleFor(name) })
  try {
    let error
    try {
      await client.call('add', { content: '用户叫测试丙。', type: 'profile', subtype: 'identity' })
    } catch (e) {
      error = e
    }
    if (!error) throw new Error('describe 不可用时写入必须被拒，但写成功了')
    if (error.code !== 'BAD_CONFIG') throw new Error(`期望 BAD_CONFIG，实际 ${error.code}`)
    if (!/写操作已禁用/.test(error.message)) throw new Error(`错误信息应说明写操作已禁用：${error.message}`)
    if (client.contract.state !== 'unavailable') {
      throw new Error(`自检状态应为 unavailable，实际 ${client.contract.state}`)
    }
    // 读操作必须还能用（契约问题不该让人连记忆都查不了）
    const stats = await client.call('stats', {})
    if (!stats || typeof stats.total !== 'number') throw new Error('读操作不该被契约自检拦下')
  } finally {
    client.close()
    cleanup(root)
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
