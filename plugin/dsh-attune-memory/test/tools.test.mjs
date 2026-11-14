/**
 * 工具面的契约断言：explicit 能被模型显式打开；hard 从模型工具里彻底消失。
 *
 * 跑法：node plugin/dsh-attune-memory/test/tools.test.mjs
 * （不要用 node --test：本沙箱里 test runner 起子进程会 EPERM）
 */
import assert from 'node:assert/strict'

import { buildTools } from '../lib/tools.js'

/** 记账假引擎：只记录被调用的 op 与 args，不写库。 */
function fakeEngine(result = {}) {
  const calls = []
  return {
    calls,
    async call(op, args) {
      calls.push([op, args])
      if (op === 'add') return { action: 'add', memory: { id: 'm-1-1', ...args }, reason: '测试' }
      if (op === 'forget') return { id: args.id, status: 'deleted' }
      return result
    },
  }
}

function finds(tools, name) {
  return tools.find((tool) => tool.name === name)
}

const engine = fakeEngine()
const tools = buildTools(engine, {})

// 1. memory_remember 的 schema 暴露 explicit，且不是必填
const remember = finds(tools, 'memory_remember')
assert.ok(remember, 'memory_remember 存在')
const explicit = remember.parameters.properties.explicit
assert.equal(explicit.type, 'boolean', 'explicit 是布尔字段')
assert.ok(!(remember.parameters.required ?? []).includes('explicit'), 'explicit 不该是必填')
assert.match(explicit.description, /明确陈述/, '描述要说清"用户明确陈述"')

// 2. 不传 explicit 时不要把 explicit 塞进请求（让引擎默认值生效）
await remember.execute({ content: '用户偏好中文回答', type: 'profile', subtype: 'interaction' })
assert.deepEqual(engine.calls.at(-1)[1].explicit, undefined, '省略 explicit 时不该传 explicit 键')

// 3. 传 explicit:true 时原样透传（引擎据此允许高置信覆盖）
await remember.execute({
  content: '用户要求叫他「哥哥」', type: 'profile', subtype: 'interaction', explicit: true,
})
assert.equal(engine.calls.at(-1)[1].explicit, true, 'explicit:true 必须透传到引擎')

// 4. memory_forget 的 schema 里没有 hard，描述写明只软删
const forget = finds(tools, 'memory_forget')
assert.ok(forget, 'memory_forget 存在')
assert.equal(forget.parameters.properties.hard, undefined, 'forget 不得再有 hard 字段')
assert.ok(!(forget.parameters.required ?? []).includes('hard'))
assert.match(forget.description, /软删除/, '描述要说清是软删除')

// 5. 即使调用方硬塞 hard，也不要发给引擎（防旧调用方残留）
await forget.execute({ id: 'm-1-1', hard: true, reason: '测试' })
const [, forgetArgs] = engine.calls.at(-1)
assert.equal(forgetArgs.hard, undefined, 'forget 不该把 hard 传给引擎')
assert.equal(engine.calls.at(-1)[0], 'forget')

// 6. 物理删除在管理页/CLI 那条路上是 forget → purge 两步，模型工具里没有第三步
assert.equal(finds(tools, 'memory_purge'), undefined, '模型侧没有 purge 工具')

// 7. memory_probe 会把契约状态带出来（写不进去时先看这里）
const probeEngine = {
  async call(op) {
    if (op === 'metrics') return { memories: { total: 0, by_status: {} }, counters: {}, observations: [] }
    return {}
  },
}
const probeTools = buildTools(probeEngine, {
  probe: () => ({ manualChars: 10, stableChars: 0, stableIds: [], sessions: [], contract: { state: 'unavailable', detail: 'ENGINE_REJECTED: 未知操作：describe' } }),
})
const output = await probeTools.find((tool) => tool.name === 'memory_probe').execute({})
assert.match(String(output), /引擎契约：unavailable/, 'probe 应报告契约状态')
assert.match(String(output), /写操作已禁用/, '契约异常时要提示写操作已禁用')

console.log('tools.test.mjs: 7 组断言全部通过')
