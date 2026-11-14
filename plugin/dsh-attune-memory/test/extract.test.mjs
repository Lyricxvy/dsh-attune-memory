/**
 * 确定性抽取的边界（W3/W5 的插件侧）：只认明确句式、默认按推断处理、
 * interaction 默认收窄到 on_demand（除非有 domain 上下文）。
 *
 * Run: node test/extract.test.mjs
 */
import assert from 'node:assert/strict'
import { extractCandidates } from '../lib/extract.js'

const one = (text, ctx) => {
  const out = extractCandidates(text, ctx)
  assert.equal(out.length, 1, `expected exactly one candidate for ${text}, got ${JSON.stringify(out)}`)
  return out[0]
}

// 显式偏好句式
const pref = one('我很喜欢用 Rust 写小工具')
assert.equal(pref.type, 'profile')
assert.equal(pref.subtype, 'preference')
assert.equal(pref.explicit, false, '抽取的一律是推断')
assert.equal(pref.confidence, 0.65)

// interaction 默认 on_demand
const inter = one('以后请先给结论再解释')
assert.equal(inter.subtype, 'interaction')
assert.equal(inter.injection_policy, 'on_demand', 'W5: 抽取的 interaction 不主动注入')

// 有 domain 上下文时，interaction 跟随 domain 作用域
const scoped = one('以后请先给结论再解释', { domain: 'study.math' })
assert.equal(scoped.scope, 'domain')
assert.equal(scoped.domain_key, 'study.math')

// 没有明确句式 → 不抽
assert.deepEqual(extractCandidates('今天天气不错，适合晒被子。'), [])
assert.deepEqual(extractCandidates('嗯'), [], '过短不抽')
assert.deepEqual(extractCandidates('x'.repeat(2001)), [], '超长（粘贴资料）不抽')

// 一条消息最多 3 条
const many = extractCandidates('我叫张三。我很喜欢喝茶。我习惯早睡。以后请简短回答。我讨厌味精。')
assert.ok(many.length <= 3, `cap at 3, got ${many.length}`)

console.log(`ok — extract: preference/interaction/domain/cap all behave (${many.length} candidates from 5 statements)`)
