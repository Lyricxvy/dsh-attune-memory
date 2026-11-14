/**
 * 契约测试探针（只在 test/contract 里用，不进发布包）。
 *
 * 它做的唯一一件事：在 `agent/pre-step` 的外层记下"这一步模型到底会看到哪些消息"，
 * 追加写入 ATTUNE_CONTRACT_LOG 指定的 JSONL 文件。契约测试跑完读这个文件，
 * 就能判断"本轮用户消息可见吗""注入了没有""注入是不是进了最终请求"。
 *
 * 为什么用外层（注册在 dsh-attune-memory 之前）：
 * pre-step 是 waterfall，先注册的在外层，`await next()` 拿到的是下游全部处理完之后
 * 的 decision —— 那才是真正要进模型的批次。注册在内层只能看到注入前的样子。
 *
 * 任何异常都吞掉：探针坏了不该影响被测系统。
 */
import { appendFileSync } from 'node:fs'

export const name = 'dsh-attune-probe'

export function apply(ctx) {
  const file = process.env.ATTUNE_CONTRACT_LOG
  if (!file) return

  const record = (payload) => {
    try {
      appendFileSync(file, `${JSON.stringify(payload)}\n`, 'utf8')
    } catch {
      /* 探针写不进去就算了，不能影响主流程 */
    }
  }

  const textOf = (message) => {
    const content = message?.content
    if (typeof content === 'string') return content
    if (Array.isArray(content)) {
      return content
        .map((part) => (part && typeof part === 'object' && part.type === 'text' ? String(part.text ?? '') : ''))
        .join('')
    }
    return ''
  }

  ctx.on('agent/pre-step', async (payload, next) => {
    let decision
    try {
      decision = await next()
    } catch (error) {
      record({ at: new Date().toISOString(), kind: 'pre-step-error', turn: payload?.turn, step: payload?.step, error: String(error?.message ?? error) })
      throw error
    }
    try {
      const messages = Array.isArray(decision?.messages) ? decision.messages : []
      const shaped = messages.map((message, index) => ({
        index,
        role: message?.role ?? null,
        kind: message?.source?.kind ?? null,
        form: message?.source?.form ?? null,
        ids: Array.isArray(message?.source?.memoryIds) ? message.source.memoryIds : undefined,
        chars: textOf(message).length,
        head: textOf(message).slice(0, 60),
      }))
      const humans = shaped.filter((entry) => entry.role === 'user' && entry.kind === 'user')
      const memory = shaped.filter((entry) => typeof entry.kind === 'string' && entry.kind.startsWith('dsh-attune-memory'))
      record({
        at: new Date().toISOString(),
        kind: 'pre-step',
        turn: payload?.turn ?? null,
        step: payload?.step ?? null,
        decisionKind: decision?.kind ?? null,
        count: shaped.length,
        humanCount: humans.length,
        lastHuman: humans.length > 0 ? shaped[humans[humans.length - 1].index] : null,
        memory,
        memoryIndexes: memory.map((entry) => entry.index),
        lastHumanIndex: humans.length > 0 ? humans[humans.length - 1].index : null,
        shaped,
      })
    } catch (error) {
      record({ at: new Date().toISOString(), kind: 'pre-step-record-error', error: String(error?.message ?? error) })
    }
    return decision
  })
}
