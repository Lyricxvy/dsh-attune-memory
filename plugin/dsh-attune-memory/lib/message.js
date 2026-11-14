/**
 * 造一条模型历史里能安全通过的消息。
 *
 * 为什么不直接改 decision.messages 里的对象：DSH 的消息是深冻结的（`createMessage` 里
 * `deepFreeze(structuredClone(...))`），改字段会静默失败或抛错。必须造新对象。
 *
 * 为什么必须带 `source.kind`：`@deepseek-ai/dsh-agent-instructions` 会无保护地读
 * `message.source.kind`（见 dsh-agent-instructions/lib/index.js:1273-1280），缺了直接
 * TypeError，整轮在第一个 step 之前就死，界面上只显示"本轮运行失败" —— 极难查。
 */
import { randomUUID } from 'node:crypto'

export const MEMORY_SOURCE = 'dsh-attune-memory'

function deepFreeze(value) {
  if (value === null || typeof value !== 'object' || Object.isFrozen(value)) return value
  Object.freeze(value)
  for (const key of Object.keys(value)) deepFreeze(value[key])
  return value
}

/**
 * @param {string} text 注入正文
 * @param {Record<string, unknown>} source 附加的来源信息（form / memoryIds / scope ...）
 */
export function createMemoryMessage(text, source = {}) {
  return deepFreeze(
    structuredClone({
      id: randomUUID(),
      role: 'user',
      content: [{ type: 'text', text }],
      source: { kind: MEMORY_SOURCE, ...source },
    }),
  )
}

/** 只承认真人输入：runtime-context / skill-catalog / time-context 都是用户角色的合成消息。 */
export function isHumanMessage(message) {
  return message?.role === 'user' && message?.source?.kind === 'user'
}

/** 把 content（字符串或 block 数组）拍平成纯文本，供检索与抽取使用。 */
export function messageText(message) {
  const content = message?.content
  if (typeof content === 'string') return content
  if (Array.isArray(content)) {
    return content
      .map((block) => (block && typeof block === 'object' && block.type === 'text' ? String(block.text ?? '') : ''))
      .filter(Boolean)
      .join('\n')
  }
  return ''
}
