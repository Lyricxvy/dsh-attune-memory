// T 通道去重的回归测试：证明"不重复注入"与"被压缩层吞掉后还能回来"同时成立。
//
// 跑法（Node 22+，无第三方依赖）：
//   node plugin/dsh-attune-memory/test/recall.test.mjs
import assert from 'node:assert/strict'

import { Recall } from '../lib/recall.js'

/** 假引擎：稳定块固定，相关块固定；调用次数可数。 */
function fakeEngine(contextText = '[长期记忆 · 本次相关]\n- 技能：极限题卡在变形方向') {
  return {
    calls: 0,
    records: [],
    async call(op, args) {
      if (op === 'record') {
        this.records.push(args ?? {})
        return { counters: {} }
      }
      assert.equal(op, 'read_for_context')
      this.calls += 1
      return {
        manual_base: '## 长期记忆怎么用\n只写长期有效的事实。',
        self_heal: '看不到记忆块时先 memory_search 再下结论。',
        has_memory: true,
        stable: { text: '[长期记忆 · 稳定事实]\n- 身份：用户叫李四。', ids: ['m-stable'] },
        context: contextText ? { text: contextText, ids: ['m-ctx'] } : { text: '' },
      }
    },
  }
}

/** 等一拍，让 fire-and-forget 的遥测调用落地。 */
const tick = () => new Promise((done) => setTimeout(done, 0))

function humanMessage(text) {
  return { id: `u-${text}`, role: 'user', content: [{ type: 'text', text }], source: { kind: 'user' } }
}

function payload(turn, sessionId = 's1') {
  return { turn, step: 0, agent: { session: { id: sessionId, header: { origin: 'main' } } } }
}

function enter(text) {
  return { kind: 'enter', messages: [humanMessage(text)] }
}

const injected = (decision) => decision.messages.some((m) => m.source?.kind === 'dsh-attune-memory')

async function main() {
  // ---- 1. 第一次注入，且只注入一条
  const engine = fakeEngine()
  const recall = new Recall({ engine, log: {} })
  await recall.refresh()

  const d1 = await recall.inject(payload(1), enter('数学一的极限题怎么突破变形方向'))
  assert.equal(injected(d1), true, '第 1 轮应当注入')
  assert.equal(d1.messages.length, 2, '注入后应当多一条消息')
  // 插在真人消息**之前**：背景贴着问题，且不改写用户原话。
  assert.equal(d1.messages[0].source.kind, 'dsh-attune-memory', '注入消息必须带 source.kind')
  assert.equal(d1.messages[1].source.kind, 'user', '真人消息仍在最后')

  // ---- 2. 同一轮第二个 step 不再注入（一轮只注一次）
  const d1b = await recall.inject(payload(1), enter('数学一的极限题怎么突破变形方向'))
  assert.equal(d1b.messages.length, 1, '同一 turn 的第二个 step 不该重复注入')

  // ---- 3. 下一轮同样的问句仍然不注入（STALE_TURNS 未到）
  const d2 = await recall.inject(payload(2), enter('数学一的极限题怎么突破变形方向'))
  assert.equal(d2.messages.length, 1, 'STALE_TURNS 之内不该重复注入')

  // ---- 4. 第 1 + 8 = 9 轮允许重发（应对"被听不见的压缩层折掉"）
  const d9 = await recall.inject(payload(9), enter('数学一的极限题怎么突破变形方向'))
  assert.equal(injected(d9), true, '隔 STALE_TURNS 轮之后应当允许重发')

  // ---- 5. 压缩通知进来后，即使本轮检索为空也要把稳定块重建出来
  const emptyEngine = fakeEngine('')
  const recall2 = new Recall({ engine: emptyEngine, log: {} })
  await recall2.refresh()
  const first = await recall2.inject(payload(1, 's2'), enter('帮我列三条明天复习的建议'))
  assert.equal(first.messages.length, 1, '没有命中时不该注入')

  recall2.noteCompaction('s2')
  const forced = await recall2.inject(payload(2, 's2'), enter('帮我列三条明天复习的建议'))
  assert.equal(injected(forced), true, '压缩之后应当强制重建')
  const body = forced.messages[0].content.map((p) => p.text).join('')
  assert.ok(body.includes('稳定事实'), '强制重建必须包含稳定块')
  assert.equal(forced.messages[0].source.form, 'reinject')

  // ---- 6. 压缩之后同一个问题立刻再问，不再重复（pending 已消费）
  const again = await recall2.inject(payload(3, 's2'), enter('帮我列三条明天复习的建议'))
  assert.equal(again.messages.length, 1, 'pending 只能消费一次')

  // ---- 7. 子 agent 不注入
  const sub = await recall.inject(
    { turn: 4, agent: { session: { id: 's3', header: { origin: 'subagent' } } } },
    enter('数学一的极限题怎么突破变形方向'),
  )
  assert.equal(sub.messages.length, 1, '子 agent 不重复注入')

  // ---- 8. 太短的消息不检索
  const short = await recall.inject(payload(5), enter('嗯'))
  assert.equal(short.messages.length, 1, '过短输入不注入')

  // ---- 9. 注入记账：注入时记 expected，下一轮核对可见性
  const tEngine = fakeEngine()
  const tRecall = new Recall({ engine: tEngine, log: {} })
  await tRecall.refresh()
  const t1 = await tRecall.inject(payload(1, 's4'), enter('数学一的极限题怎么突破变形方向'))
  assert.equal(injected(t1), true)
  await tick()
  assert.ok(
    tEngine.records.some((r) => r.counters?.inject_expected > 0),
    '注入后应当记一条 inject_expected',
  )

  // 下一轮上下文里注入块"还在"（模拟正常情况）
  const carried = t1.messages.map((m) => m)
  const ok = await tRecall.inject(payload(2, 's4'), { kind: 'enter', messages: [...carried, humanMessage('随便聊两句别的')] })
  assert.equal(injected(ok), true, '上下文里注入块在时，新一轮照常注入')
  await tick()
  assert.ok(tEngine.records.some((r) => r.counters?.inject_visible === 1), '应当记一条 inject_visible')
  assert.equal(tEngine.records.some((r) => r.counters?.inject_faded_unexplained), false)

  // ---- 9.1 可见性以会话历史为准（decision.messages 里没有也算在）
  const hisEngine = fakeEngine()
  const hisRecall = new Recall({ engine: hisEngine, log: {} })
  await hisRecall.refresh()
  const h1 = await hisRecall.inject(payload(1, 's91'), enter('数学一的极限题怎么突破变形方向'))
  await tick()
  const persisted = h1.messages.find((m) => m.source?.kind === 'dsh-attune-memory')
  // 第二轮：decision.messages 只有新消息，但历史里还留着上一轮的注入块
  const historySession = {
    id: 's91',
    header: { origin: 'main' },
    deriveMessages: () => [humanMessage('上一轮的问题'), persisted, humanMessage('这轮的新问题')],
  }
  await hisRecall.inject(
    { turn: 2, step: 0, agent: { session: historySession } },
    enter('换个话题：帮我列三条复习建议'),
  )
  await tick()
  assert.ok(
    hisEngine.records.some((r) => r.counters?.inject_visible === 1),
    '历史里还在 → 应记 inject_visible（旧实现查 decision 会误报消失）',
  )

  // ---- 10. 注入块在没有压缩事件的情况下消失 → 原因不明（ACP 盲区）
  const aEngine = fakeEngine()
  const aRecall = new Recall({ engine: aEngine, log: {} })
  await aRecall.refresh()
  await aRecall.inject(payload(1, 's5'), enter('数学一的极限题怎么突破变形方向'))
  await tick()
  await aRecall.inject(payload(2, 's5'), enter('换个话题：帮我列三条复习建议'))
  await tick()
  const faded = aEngine.records.find((r) => r.counters?.inject_faded_unexplained === 1)
  assert.ok(faded, '没有压缩事件时消失，应当记为 inject_faded_unexplained')
  assert.equal(faded.observation?.detail, 'no-compaction-observed')

  // ---- 11. 有压缩事件时消失 → 归因给压缩，不算"原因不明"
  const cEngine = fakeEngine()
  const cRecall = new Recall({ engine: cEngine, log: {} })
  await cRecall.refresh()
  await cRecall.inject(payload(1, 's6'), enter('数学一的极限题怎么突破变形方向'))
  await tick()
  cRecall.noteCompaction('s6')
  await cRecall.inject(payload(2, 's6'), enter('换个话题：帮我列三条复习建议'))
  await tick()
  const compactedFade = cEngine.records.find((r) => r.counters?.inject_faded_compaction === 1)
  assert.ok(compactedFade, '压缩导致的消失应记为 inject_faded_compaction')
  assert.equal(compactedFade.observation?.detail, 'after-compaction')

  // ---- 12. probe() 只读自检，不触发任何引擎调用
  const before = cEngine.records.length
  const snapshot = cRecall.probe()
  assert.equal(cEngine.records.length, before, 'probe 不该写库')
  assert.ok(snapshot.stableChars > 0 && Array.isArray(snapshot.sessions))

  // ---- O2：自愈指令按轮频控（不是每轮都出现）
  const hEngine = fakeEngine()
  const hRecall = new Recall({ engine: hEngine, log: {} })
  await hRecall.refresh()
  hRecall.noteTurn(1)
  assert.match(hRecall.manual, /memory_search/, '第 1 轮应带自愈指令')
  hRecall.noteTurn(2)
  assert.doesNotMatch(hRecall.manual, /memory_search/, '第 2 轮不应重复')
  hRecall.noteTurn(5)
  assert.doesNotMatch(hRecall.manual, /memory_search/, '第 5 轮仍不应重复')
  hRecall.noteTurn(6)
  assert.match(hRecall.manual, /memory_search/, '第 6 轮（间隔 5 轮）应再次出现')

  // 空库时不该提醒自愈：让模型白搜一次没有意义
  const emptyHeal = new Recall({
    engine: { async call() { return { manual_base: '## 手册', self_heal: '看不到就 memory_search', has_memory: false } } },
    log: {},
  })
  await emptyHeal.refresh()
  emptyHeal.noteTurn(1)
  assert.doesNotMatch(emptyHeal.manual, /memory_search/, '空库不提醒')

  console.log('recall.test.mjs: 12 组断言全部通过')
}

main().catch((error) => {
  console.error('recall.test.mjs 失败：', error)
  process.exitCode = 1
})
