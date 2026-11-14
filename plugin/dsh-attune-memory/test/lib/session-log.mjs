/**
 * 会话日志读取（契约测试用）。
 *
 * DSH 把会话日志按 flush 分帧写成 zstd：一个文件里是**多帧拼接**的 zstd 流。
 * Node 的 `zlib.zstdDecompressSync` 只解第一帧，所以必须按帧魔数逐个解再拼。
 * 帧魔数：标准帧 `0x28B52FFD`，skippable 帧 `0x184D2A5x`。
 *
 * 这个文件是 `.verify/dump-session.js` 的正式化版本（原来的只用于一次性排查）。
 */
import { readdirSync, readFileSync, existsSync } from 'node:fs'
import { join } from 'node:path'
import { zstdDecompressSync } from 'node:zlib'

const MAGIC_STD = [0x28, 0xb5, 0x2f, 0xfd]
const MAGIC_SKIP = [0x50, 0x2a, 0x4d, 0x18]

function isFrameAt(buf, i) {
  if (i + 4 > buf.length) return 0
  const std = MAGIC_STD.every((byte, k) => buf[i + k] === byte)
  if (std) return 1
  const skip = MAGIC_SKIP.every((byte, k) => buf[i + k] === byte)
  return skip ? 2 : 0
}

/** 把多帧 zstd 解成整份文本。解不出来的字节跳过，不抛错。 */
export function decompressSession(buf) {
  const parts = []
  let i = 0
  let frames = 0
  let skipped = 0
  while (i + 4 <= buf.length) {
    if (!isFrameAt(buf, i)) {
      i += 1
      skipped += 1
      continue
    }
    try {
      const out = zstdDecompressSync(buf.subarray(i))
      if (out.length > 0) {
        parts.push(out.toString('utf8'))
        frames += 1
      }
      // 下一帧起点：从当前魔数之后找下一个魔数
      let j = i + 4
      while (j + 4 <= buf.length && !isFrameAt(buf, j)) j += 1
      i = j
    } catch {
      i += 1
    }
  }
  return { text: parts.join(''), frames, skipped }
}

/** 在 $DSH_HOME/sessions/*\/session-<id>/ 里找到会话文件（会话 id 全局唯一）。 */
export function findSessionFile(dshHome, sessionId) {
  const root = join(dshHome, 'sessions')
  if (!existsSync(root)) return null
  for (const bucket of readdirSync(root)) {
    const dir = join(root, bucket, sessionId)
    if (!existsSync(dir)) continue
    for (const name of readdirSync(dir)) {
      if (name.endsWith('.zstd')) return join(dir, name)
    }
  }
  return null
}

/** 读会话事件：[{type, data}]。坏行跳过。 */
export function readSessionEvents(file) {
  const { text, frames, skipped } = decompressSession(readFileSync(file))
  const events = []
  for (const line of text.split('\n')) {
    if (!line.trim()) continue
    try {
      const raw = JSON.parse(line)
      events.push({ type: raw.type ?? '?', data: raw.data ?? {}, seq: raw.seq })
    } catch {
      /* 半行（理论上不会出现）跳过 */
    }
  }
  return { events, frames, bytes: readFileSync(file).length, chars: text.length, skipped }
}

/** 从一条事件里取消息正文（字符串或 block 数组都支持）。 */
export function messageText(message) {
  const content = message?.content
  if (typeof content === 'string') return content
  if (Array.isArray(content)) {
    return content
      .map((part) => (part && typeof part === 'object' && part.type === 'text' ? String(part.text ?? '') : ''))
      .join('')
  }
  return ''
}

/**
 * 把事件列表压成契约测试关心的几张表。
 * - systemMessages: [{turn, step, text}]
 * - injected: [{seq, turn, form, ids, afterCompaction, text}]
 * - compaction: [{phase, turn}]
 * - humanUserSeqs: 真人 user/message 的序号
 */
export function summarize(events) {
  const systemMessages = []
  const injected = []
  const compaction = []
  const humanUserSeqs = []
  let seq = 0
  for (const event of events) {
    seq += 1
    const data = event.data ?? {}
    // 两种事件的 data 形状不同：system/message 是 {turn, step, message}，
    // 而 user/message 的 data 本身就是消息（id/role/content/source）。
    const message = data.message ?? data
    const at = typeof event.seq === 'number' ? event.seq : seq
    if (event.type === 'system/message') {
      systemMessages.push({ turn: data.turn, step: data.step, text: messageText(message) })
      continue
    }
    if (event.type === 'user/message') {
      const source = message.source ?? {}
      if (source.kind === 'user') humanUserSeqs.push(at)
      if (typeof source.kind === 'string' && source.kind.startsWith('dsh-attune-memory')) {
        injected.push({
          seq: at,
          turn: data.turn ?? message.turn ?? null,
          form: source.form ?? null,
          ids: source.memoryIds ?? [],
          afterCompaction: Boolean(source.afterCompaction),
          text: messageText(message),
        })
      }
      continue
    }
    if (event.type === 'compaction/start' || event.type === 'compaction/end') {
      // compaction/end 带 error 表示宿主放弃了这次压缩（例如摘要不比原文小）。
      const error = typeof data.error === 'string' && data.error.length > 0 ? data.error : null
      compaction.push({ phase: event.type, turn: data.turn, error, seq: at })
    }
  }
  return { systemMessages, injected, compaction, humanUserSeqs }
}
