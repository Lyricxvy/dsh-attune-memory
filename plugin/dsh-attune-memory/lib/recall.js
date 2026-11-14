/**
 * T 通道：每轮把"和这个问题相关"的记忆插到最后一条真人消息之前。
 *
 * 为什么必须是 `agent/pre-step`（见 CURRENT_ENVIRONMENT_ANALYSIS.md §3.3）：
 * agent-loop 在 `:670` 先 claim 用户消息、`:679` 先 assemble 系统提示，之后才在 `:862`
 * 把消息写进会话。所以 section/context 的 text() 函数在**用户消息还没入库**时就被调用了，
 * 想按"这一句在问什么"检索，只能在 pre-step 里拿到 decision.messages 之后做。
 *
 * 三条硬约束（都是踩过的坑）：
 *   1. 注入消息必须带 `source.kind`，否则 agent-instructions 读属性时 TypeError；
 *   2. 消息是深冻结的，必须造新对象（不能改 decision 里的原对象）；
 *   3. 同会话同内容不要每轮都注入 —— 注入块会进历史、之后每个请求都重发，不去重历史会滚雪球。
 *
 * 去重不是"永久记住"：任何压缩层都可能把注入块折叠掉（DSH 的 compaction 会发事件，
 * 代理层的 ACP 不会）。所以这里记的是"上次在第几轮注入"，`STALE_TURNS` 轮之后允许重发：
 * 既不会每轮重复，也不会出现"被折叠后再也不出现"（dsh-local-memory 的真实缺陷）。
 */
import { createHash, randomUUID } from 'node:crypto'
import { createMemoryMessage, isHumanMessage, messageText, MEMORY_SOURCE } from './message.js'

/** 太短的输入不检索：'嗯'、'继续'、'好' 这类话没有检索价值，只会污染上下文。 */
const MIN_QUERY_CHARS = 4
/**
 * 同一段上下文隔多少轮允许再注入一次。
 * 取 8 是因为：注入块通常在前 2–3 轮内就落到"最近的窗口"之外；再短会明显重复，
 * 再长则一旦被压缩层折叠，模型可能连着十几轮都看不到这条记忆。
 */
const STALE_TURNS = 8
/**
 * O2：自愈指令（"看不到记忆块就先 memory_search"）的出现频率。
 * 每轮都出现会让模型每轮都搜一次，既费 token 又制造噪声；长期不出现则一旦
 * 稳定块真的被抹掉就再没人提醒。5 轮是"够稀疏、又不至于失联"的折中。
 */
const SELF_HEAL_EVERY = 5
/** 领域推断表：命中最多的一类胜出，一个都不命中就不带 domain（ContextMatch 取 0.9）。 */
const DOMAIN_HINTS = [
  ['study.math', ['数学', '微积分', '极限', '导数', '积分', '线性代数', '矩阵', '行列式', '概率', '期望', '方差', '定理', '证明题']],
  ['study.english', ['英语', '单词', '阅读', '作文', '听力', '翻译', '长难句', '四六级', '雅思', '托福']],
  ['study.heat', ['传热', '导热', '对流', '辐射', '换热', '热阻', '热流', '边界层']],
  ['code', ['代码', '函数', '报错', 'bug', '编译', '部署', '调试', '测试', '接口', '插件', '脚本']],
  ['writing', ['写作', '文案', '润色', '标题', '推文', '稿子']],
  ['career', ['考研', '复试', '简历', '面试', '实习', '导师']],
]

export function guessDomain(text) {
  let best = null
  let bestHits = 0
  for (const [domain, words] of DOMAIN_HINTS) {
    let hits = 0
    for (const word of words) if (text.includes(word)) hits += 1
    if (hits > bestHits) {
      best = domain
      bestHits = hits
    }
  }
  return bestHits > 0 ? best : null
}

function hashText(text) {
  return createHash('sha1').update(text).digest('hex').slice(0, 16)
}

export class Recall {
  #engine
  #log
  #manualBase = ''
  #selfHeal = ''
  #hasMemory = false
  /** 当前轮次与"上一次把自愈指令放出去"的轮次（O2 频率控制）。 */
  #turnClock = 0
  #healAt = -SELF_HEAL_EVERY // 首轮就应该带一次：新会话最需要知道"看不到不代表没有"
  #healVisible = false
  #stableText = ''
  #stableIds = []
  #seen = new Map()
  #pendingReinject = new Set()
  #injectedTurns = new Set()
  #lastError = null
  /** 会话 → 上一次注入的凭据，用于下一轮回头核对"它还在不在上下文里"。 */
  #lastInject = new Map()
  /** 发生过压缩、但还没核对过注入的会话。核对完就消费掉。 */
  #compacted = new Set()

  constructor({ engine, log }) {
    this.#engine = engine
    this.#log = log
  }

  /**
   * S1 通道的正文：引擎自带的用法手册。
   * 引擎没起来时返回空串，宁可少说也不胡说；自愈段按 SELF_HEAL_EVERY 轮一次拼进来。
   */
  get manual() {
    if (!this.#manualBase) return ''
    if (!this.#healVisible || !this.#selfHeal) return this.#manualBase
    return `${this.#manualBase}\n\n${this.#selfHeal}`
  }

  /**
   * 轮次推进时决定这一轮要不要带自愈指令。
   * 放在这里而不是 getter 里，是因为 getter 一个 step 内可能被调多次——
   * 中途改文本会让系统提示在同一个 step 内变化，反而破坏前缀缓存。
   */
  noteTurn(turn) {
    const t = Number(turn)
    if (!Number.isFinite(t) || t <= this.#turnClock) return
    this.#turnClock = t
    const due = this.#hasMemory && Boolean(this.#selfHeal) && t - this.#healAt >= SELF_HEAL_EVERY
    this.#healVisible = due
    if (due) this.#healAt = t
  }

  /** S2 通道的正文：稳定事实块。必须是同步可读的，所以只返回缓存。 */
  get stableText() {
    return this.#stableText
  }

  get lastError() {
    return this.#lastError
  }

  /** 刷新 S/C 两个通道的常量与稳定块。启动时调一次，之后每次写操作与回合结束时调。 */
  async refresh() {
    try {
      const res = await this.#engine.call('read_for_context', {})
      if (typeof res?.manual_base === 'string') this.#manualBase = res.manual_base
      else if (typeof res?.manual === 'string') this.#manualBase = res.manual
      if (typeof res?.self_heal === 'string') this.#selfHeal = res.self_heal
      if (typeof res?.has_memory === 'boolean') this.#hasMemory = res.has_memory
      const next = res?.stable?.text ?? ''
      if (next !== this.#stableText) {
        this.#stableText = next
        this.#stableIds = Array.isArray(res?.stable?.ids) ? res.stable.ids : []
      }
      return true
    } catch (error) {
      this.#lastError = error
      this.#log?.warn?.(`dsh-attune-memory: 读取常驻记忆失败（保留上一版）：${error?.message ?? error}`)
      return false
    }
  }

  /** 压缩结束：本会话的注入块已被遮蔽，去重集合作废，下一轮整块重建。 */
  noteCompaction(sessionId) {
    this.#pendingReinject.add(sessionId)
    this.#seen.delete(sessionId)
    this.#compacted.add(sessionId)
  }
  forgetSession(sessionId) {
    this.#pendingReinject.delete(sessionId)
    this.#seen.delete(sessionId)
    this.#lastInject.delete(sessionId)
    this.#compacted.delete(sessionId)
  }

  /** 给 `memory_probe` 用的自检快照（只读，不改任何状态）。 */
  probe() {
    const sessions = [...this.#lastInject.entries()].map(([id, v]) => ({ sessionId: id, ...v }))
    return {
      manualChars: this.manual.length,
      selfHealVisible: this.#healVisible,
      stableChars: this.#stableText.length,
      stableIds: this.#stableIds,
      sessions,
      pendingReinject: [...this.#pendingReinject],
      // 契约状态（T-013）：写不进去时先看这里 —— ok/mismatch/unavailable。
      contract: this.#engine?.contract ?? null,
      lastError: this.#lastError ? String(this.#lastError.message ?? this.#lastError) : null,
    }
  }

  /** 遥测记账：只观测，失败就算了，绝不影响注入。 */
  async #record(payload) {
    try {
      return await this.#engine.call('record', payload)
    } catch (error) {
      this.#log?.warn?.(`dsh-attune-memory: 遥测写入失败（忽略）：${error?.message ?? error}`)
      return null
    }
  }

  /**
   * 核对上一次注入是否还在模型眼前。
   *
   * 这是对 ACP 盲区的落地方案：代理层把消息折叠掉时不发任何会话事件，
   * 我们只能在一轮之后回头问自己"那条消息还在 decision.messages 里吗"。
   * 结论只记为观测（kind=inject-visible / inject-faded），不改变注入行为 ——
   * 症状级证据，不是自动断言。
   */
  #reconcile(decision, turn, sessionId, session) {
    const last = this.#lastInject.get(sessionId)
    if (!last || turn <= last.turn) return
    // 注入块上一轮是被 agent-loop 写进**会话历史**的，不是留在 pre-step 的 decision.messages 里。
    // 早期版本只查 decision.messages，于是每一轮都判成"消失"（实测 8 次全误报）。
    // 这里两处都查：历史是主证据，decision 只用于同一轮内重复装配的情况。
    let history = []
    try {
      history = typeof session?.deriveMessages === 'function' ? session.deriveMessages() : []
    } catch {
      history = []
    }
    const seen = (message) =>
      message?.source?.kind === MEMORY_SOURCE && message?.source?.injectId === last.injectId
    const stillThere = history.some(seen) || decision.messages.some(seen)
    const afterCompaction = this.#compacted.delete(sessionId)
    if (stillThere) {
      void this.#record({ counters: { inject_visible: 1 }, observation: { kind: 'inject-visible', subject: last.injectId } })
      return
    }
    void this.#record({
      counters: afterCompaction ? { inject_faded_compaction: 1 } : { inject_faded_unexplained: 1 },
      observation: {
        kind: 'inject-faded',
        subject: last.injectId,
        detail: afterCompaction ? 'after-compaction' : 'no-compaction-observed',
      },
    })
  }

  /**
   * 注入决策。返回新的 decision（或在不需要注入时原样返回）。
   * @param {{agent?: any, turn?: number, step?: number}} payload
   * @param {{kind: string, messages?: any[]}} decision
   */
  async inject(payload, decision) {
    if (decision?.kind !== 'enter' || !Array.isArray(decision.messages)) return decision
    const agent = payload?.agent
    const session = agent?.session
    if (!session) return decision
    if (session.header?.origin === 'subagent') return decision // 子 agent 继承主会话的记忆，不重复注入

    const humans = decision.messages.filter(isHumanMessage)
    if (humans.length === 0) return decision
    const lastUser = humans[humans.length - 1]
    const query = messageText(lastUser).trim()
    if (query.length < MIN_QUERY_CHARS) return decision
    if (query.startsWith('/')) return decision // 斜杠命令不是问题，别浪费一次检索

    const sessionId = String(session.id ?? 'unknown')
    const turn = Number(payload?.turn ?? 0)
    this.#reconcile(decision, turn, sessionId, session)
    const turnKey = `${sessionId}:${turn}`
    if (this.#injectedTurns.has(turnKey)) return decision
    if (this.#injectedTurns.size > 512) this.#injectedTurns.clear() // 只是防内存增长，不是语义状态

    const force = this.#pendingReinject.has(sessionId)
    // 会话 → (问题哈希 → 上次注入的轮次)。记轮次而不是布尔，才能在 STALE_TURNS 之后允许重发。
    const seen = this.#seen.get(sessionId) ?? new Map()
    const queryHash = hashText(query)
    if (!force) {
      const lastTurn = seen.get(queryHash)
      if (lastTurn !== undefined && turn - lastTurn < STALE_TURNS) return decision
    }

    const domain = guessDomain(query)
    let res
    try {
      res = await this.#engine.call('read_for_context', domain ? { query, domain } : { query })
    } catch (error) {
      this.#lastError = error
      this.#log?.warn?.(`dsh-attune-memory: 检索失败（fail-open，本轮不注入）：${error?.message ?? error}`)
      return decision
    }

    const text = res?.context?.text ?? ''
    // 稳定块可能刚变（别的地方写过库），顺手同步给 S2 通道的缓存。
    if (typeof res?.stable?.text === 'string' && res.stable.text !== this.#stableText) {
      this.#stableText = res.stable.text
      this.#stableIds = Array.isArray(res.stable.ids) ? res.stable.ids : []
    }

    // 没有相关命中就不注入（"宁可少说"）。唯一的例外是压缩之后：
    // 那时本会话的注入块已被遮蔽，即使这轮问题没命中任何条目，也要把稳定块重建出来
    // （稳定块本来就在 system prompt 里，这里的合并只是引擎/S 通道临时不可用时的保底）。
    if (!text && !(force && this.#stableText)) {
      this.#injectedTurns.add(turnKey)
      if (force) this.#pendingReinject.delete(sessionId)
      return decision
    }

    // 压缩后强制重注入时，把"稳定块 + 相关块"合成一条，模型才看得到完整背景。
    const body = force && this.#stableText ? (text ? `${this.#stableText}\n\n${text}` : this.#stableText) : text
    const ids = [...new Set([...(force ? this.#stableIds : []), ...(res?.context?.ids ?? [])])]

    const injectId = randomUUID()
    const note = createMemoryMessage(body, {
      form: force ? 'reinject' : 'recall',
      injectId,
      memoryIds: ids,
      ...(domain ? { domain } : {}),
      ...(force ? { afterCompaction: true } : {}),
    })

    const messages = [...decision.messages]
    // 插在最后一条**真人**消息之前（不是数组最前）：背景要贴着问题，且排在 runtime-context 快照之前。
    const index = messages.indexOf(lastUser)
    messages.splice(index < 0 ? messages.length : index, 0, note)

    seen.set(queryHash, turn)
    this.#seen.set(sessionId, seen)
    this.#lastInject.set(sessionId, { turn, injectId, ids })
    // 只有真正 splice 进决策的这一批 id 才记 inject_count（模块 4）。
    void this.#record({
      counters: { inject_turns: 1, inject_expected: ids.length || 1 },
      ...(ids.length > 0 ? { injected_ids: ids } : {}),
    })
    this.#injectedTurns.add(turnKey)
    if (force) this.#pendingReinject.delete(sessionId)

    this.#log?.info?.(
      `dsh-attune-memory: 注入 ${ids.length} 条记忆（domain=${domain ?? '-'}${force ? '，压缩后重建' : ''}）`,
    )
    return { ...decision, messages }
  }
}
