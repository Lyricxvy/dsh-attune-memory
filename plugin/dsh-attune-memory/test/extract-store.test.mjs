/**
 * 兜底抽取通道端到端（插件 → 真引擎子进程）。
 *
 * 要证的三件事：
 *   1. 含确定性句式的消息**真的落库**了（preview→add 链路打通，对照审计第 2 条）；
 *   2. 一轮抽取留下 attempt/written/blocked/failed 四个计数（抽取不再静默）；
 *   3. 抽取出来的条目仍按 inferred 记（confidence ≤ 0.7，不会冒充用户明说）。
 *
 * 跑法：node plugin/dsh-attune-memory/test/extract-store.test.mjs
 * （不要用 node --test：本沙箱里 test runner 起子进程会 EPERM）
 */
import { mkdirSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'

import { apply } from '../lib/index.js'
import { EngineClient } from '../lib/engine.js'
// 包名/模块名只从 shared 探测点来，本文件不再自己选包
import { ENGINE_DIR, MODULE } from './lib/engine-pkg.mjs'
// 临时库放系统临时目录：以前写在 engine/_probe/tmp，会把 .db/-wal/-shm 留在仓库里，
// 发布树里就是"测试弄脏仓库"（check-layout 会抓到）。
const TMP = join(tmpdir(), 'attune-extract-store')
const DB = join(TMP, 'extract-store.db')
const PY = process.env.ATTUNE_TEST_PYTHON || 'python'

mkdirSync(TMP, { recursive: true })
for (const suffix of ['', '-wal', '-shm']) rmSync(`${DB}${suffix}`, { force: true })

const handlers = new Map()
const ctx = {
  logger: { info() {}, warn() {}, debug() {}, error() {} },
  effect(fn) {
    return fn() ?? (() => {})
  },
  systemPrompt: { section() {}, context() {} },
  tools: { register() {} },
  inject() {},
  get() {
    return undefined
  },
  on(name, fn) {
    handlers.set(name, fn)
  },
}

apply(ctx, { engineDir: ENGINE_DIR, dbPath: DB, engineModule: MODULE, python: PY, timeoutMs: 8000 })

const engine = new EngineClient({
  engineDir: ENGINE_DIR,
  engineModule: MODULE,
  dbPath: DB,
  python: PY,
  timeoutMs: 8000,
})

const emit = (type, data, sessionId = 'extract-s1') =>
  handlers.get('session/event')?.({ id: sessionId, header: { origin: 'main' } }, { type, data })
const human = (id, text) => ({ id, role: 'user', content: [{ type: 'text', text }], source: { kind: 'user' } })
const settle = () => new Promise((r) => setTimeout(r, 900))

function assert(ok, message) {
  if (!ok) throw new Error(message)
}

// 插件自己的引擎子进程会一直挂着管道句柄；断言跑完就显式退出，
// 否则 node 的事件循环永远等不到"自然结束"（Windows 上会看起来像卡死）。
const done = (code) => {
  engine.close()
  setTimeout(() => process.exit(code), 200)
}

// ---- 第一轮：说有确定性句式的话，应当落库 -----------------------------------
emit('turn/start', {})
emit('user/message', human('u-1', '以后请先讲思路再给步骤，不要一上来就丢公式。'))
emit('turn/end', {})
await settle()

const afterFirst = await engine.call('stats')
assert(afterFirst.total >= 1, `抽取应当落库至少 1 条，实际 ${afterFirst.total}`)

const metrics1 = await engine.call('metrics', { limit: 5 })
const c1 = metrics1.counters ?? {}
assert(Number(c1.extract_attempt) >= 1, `attempt 应 ≥1，实际 ${c1.extract_attempt}`)
assert(Number(c1.extract_written) >= 1, `written 应 ≥1，实际 ${c1.extract_written}`)

const rows = await engine.call('list', { status: 'active', limit: 20 })
const inferredRow = rows.find((row) => row.subtype === 'interaction') ?? rows[0]
assert(inferredRow, '应能读到落库的条目')
assert(Number(inferredRow.confidence) <= 0.7, `抽取条目必须按推断记，实际 confidence=${inferredRow.confidence}`)

// ---- 第二轮：句式命中但价值闸门拦下（blocked+1，总数不变） -------------------
// 用"记住：…"这种显式句式，保证抽取器真的产出了候选；被拦发生在 preview 的闸门里，
// 而不是"抽取器没认出这句话"（后者压根不算 blocked）。
emit('turn/start', {})
emit('user/message', human('u-2', '记住：今天天气不错，适合晒被子。'))
emit('turn/end', {})
await settle()

const afterSecond = await engine.call('stats')
assert(afterSecond.total === afterFirst.total, `垃圾话不该落库，实际 ${afterFirst.total} → ${afterSecond.total}`)

const metrics2 = await engine.call('metrics', { limit: 5 })
const c2 = metrics2.counters ?? {}
assert(Number(c2.extract_blocked) >= 1, `blocked 应 ≥1，实际 ${c2.extract_blocked}`)
assert(Number(c2.extract_failed) === 0, `不该有失败，实际 ${c2.extract_failed}`)

// ---- 第三轮：模型这轮自己写过（actor=user），抽取应跳过并记账 -----------------
emit('turn/start', {})
emit('user/message', human('u-3', '我叫王五，记住了吗？'))
// 模型自己写记忆发生在**这一轮之内**（turn/start 之后）——v0.3.0 起引擎时间戳
// 精确到毫秒，写在 turn 之前就不会落进本轮的 recent 窗口（这是对的，别把
// 上一轮写过的记忆算成"这轮已经写过"）。
const seeded = await engine.call('add', {
  content: '用户希望回答先给结论再给细节。',
  type: 'profile',
  subtype: 'interaction',
  explicit: true,
})
emit('turn/end', {})
await settle()

const metrics3 = await engine.call('metrics', { limit: 5 })
const c3 = metrics3.counters ?? {}
assert(Number(c3.extract_skipped) >= 1, `模型自己写过时应记 skipped，实际 ${c3.extract_skipped}`)
assert(seeded?.memory?.id, '种子写入应成功')

try {
  rmSync(DB, { force: true })
  rmSync(`${DB}-wal`, { force: true })
  rmSync(`${DB}-shm`, { force: true })
} catch {
  /* Windows 上文件可能还被子进程占着，留给临时目录自己回收 */
}

console.log('extract-store.test.mjs: 抽取链路与四计数全部通过')
done(0)
