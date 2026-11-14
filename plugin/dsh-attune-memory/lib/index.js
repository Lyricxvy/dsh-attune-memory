/**
 * dsh-attune-memory —— DeepSeek Harness 的长期记忆插件（宿主侧接线）。
 *
 * 三条通道各管一段（ADR-005，ADR-019 改过一次）：
 *   S1 通道 `systemPrompt.section(order=150)`：常量"记忆怎么用"手册。
 *   S2 通道 `systemPrompt.section(order=155)`：always 层的稳定事实。
 *     两者都在 system prompt 里，而不是历史消息里 —— 因为历史会被**任何一层**压缩器折叠：
 *     DSH 自己的 `compaction-basic` 会发 `compaction/end`（我们能听见），而代理层的 ACP
 *     （billion-context）在服务端重写请求体，既不发事件、也不改会话状态，我们什么都收不到。
 *     放 system prompt 是唯一不依赖"谁能通知我"的位置：DSH 每个请求都重新渲染它，
 *     `compaction-basic` 的每个区间也只从"第一个不是 system/message 的节点"开始。
 *   T 通道 `agent/pre-step`：与当前问题相关的记忆，插在最后一条真人消息之前；压缩后由
 *     本插件的 `reinjectPending` 负责整块重建，另有一条"注入过的内容 N 轮后允许重发"的兜底。
 *
 * 写入（ADR-006）：模型工具为主，`turn/end` 的确定性句式抽取为辅。
 * 引擎（ADR-008）：常驻 Python 子进程 + stdio JSON-RPC；任何一步失败都 fail-open。
 */
import { mkdirSync } from 'node:fs'
import { homedir } from 'node:os'
import { dirname, join } from 'node:path'

import { EngineClient } from './engine.js'
import { extractCandidates } from './extract.js'
import { isHumanMessage, messageText } from './message.js'
import { Recall, guessDomain } from './recall.js'
import { registerRoutes } from './routes.js'
import { buildTools } from './tools.js'

export const name = 'dsh-attune-memory'
/** Cordis 只允许访问声明过的服务；漏了不会编译报错，而是运行时报 "cannot get property ... without inject"。 */
export const inject = ['tools', 'systemPrompt']

/** S1 通道位置：排在第一方指引之后、harness/source 之类靠后的段落之前。 */
const MANUAL_ORDER = 150
/** S2 通道位置：紧跟手册之后、仍在 system prompt 内。 */
const STABLE_ORDER = 155

export function apply(ctx, config = {}) {
  const settings = resolveConfig(config)
  const log = ctx.logger ?? console

  const engine = new EngineClient({
    python: settings.python,
    engineDir: settings.engineDir,
    dbPath: settings.dbPath,
    // 发布树里包名是 attune_memory（rename.py 改名），源码树里是 memory。
    // 默认按发布树走；源码树里跑测试时用 engineModule 配置覆盖。
    engineModule: settings.engineModule,
    timeoutMs: settings.timeoutMs,
    log,
    debug: settings.debug,
  })
  const recall = new Recall({ engine, log })
  let disposed = false

  /** 会话 → 本回合待抽取的真人消息（turn/end 时消费）。 */
  const pendingTexts = new Map()
  /** M2：会话 → 最近 5 句真人原话（只在内存里，用来把碎片拼成一句可读的自述）。 */
  const recentTexts = new Map()
  /** 会话 → 本回合开始时刻（ISO）。抽取只在该时刻之后没有"模型自己写的"变更时进行。 */
  const turnStart = new Map()

  ctx.effect(() => () => {
    disposed = true
    recall.forgetSession('*')
    engine.close()
  })

  // ---------------------------------------------------------------- S 通道
  ctx.systemPrompt.section({
    name: 'memory:manual',
    order: MANUAL_ORDER,
    // 常量手册：文本一旦稳定，system 节点就不动，KV 前缀不会被我们自己打断。
    text: () => recall.manual,
  })

  // ---------------------------------------------------------------- S2 通道
  // 稳定事实放 section，不放 context：section 由 DSH 每个请求重新渲染，天然在"前缀"里；
  // context 会变成历史里的一条快照消息，被任何压缩层（尤其代理层 ACP）折叠后无声消失。
  ctx.systemPrompt.section({
    name: 'memory:stable',
    order: STABLE_ORDER,
    // 绝不能带时间词：文本每轮都变，system 节点就每轮变，KV 前缀每轮断一次。
    text: () => recall.stableText,
  })

  // ---------------------------------------------------------------- 写点
  for (const tool of buildTools(engine, {
    onWrite: () => {
      void recall.refresh()
    },
    domainFor: (value) => guessDomain(String(value ?? '')),
    probe: () => recall.probe(),
  })) {
    try {
      ctx.tools.register(tool)
    } catch (error) {
      // 单个工具注册失败不该拖垮整个插件（重名、schema 不支持都可能）。
      log.warn?.(`dsh-attune-memory: 工具 ${tool.name} 注册失败：${error?.message ?? error}`)
    }
  }

  // ---------------------------------------------------------------- 管理页（P9）
  // 用 ctx.inject 而不是把 webServer 写进顶层 inject：记忆本体在无 Web 的组合
  // （headless）里也必须能用，管理页只是"在场就挂"的附加能力。
  ctx.inject?.(['connection', 'webServer'], (webCtx) => {
    try {
      const disposeRoutes = registerRoutes(webCtx, { engine, log })
      webCtx.effect(() => () => {
        try {
          disposeRoutes?.()
        } catch {
          /* 卸载时的清理失败不值得炸掉整个宿主 */
        }
      })
      log.info?.('dsh-attune-memory: 管理页已挂载（/dsh-attune-memory/）')
    } catch (error) {
      log.warn?.(`dsh-attune-memory: 管理页挂载失败：${error?.message ?? error}`)
    }
  })

  // ---------------------------------------------------------------- 会话事件
  ctx.on('session/event', (session, event) => {
    try {
      if (disposed || !event || typeof event.type !== 'string') return
      const sessionId = String(session?.id ?? 'unknown')

      if (event.type === 'compaction/end') {
        // data.error 为真表示压缩失败，此时历史没被替换，不需要重注入。
        if (event.data?.error) return
        recall.noteCompaction(sessionId)
        log.info?.('dsh-attune-memory: 检测到压缩完成，下一轮重建记忆上下文')
        return
      }

      if (event.type === 'user/message') {
        const message = event.data
        if (!isHumanMessage(message)) return
        const body = messageText(message).trim()
        if (!body) return
        // M2：跨轮缓冲。单句太短（"大三了""本校"）时，只有把前后几句拼起来才读得懂；
        // 这里只留最近 5 句，且不落盘，进程退出就没了。
        const history = recentTexts.get(sessionId) ?? []
        history.push(body)
        while (history.length > 5) history.shift()
        recentTexts.set(sessionId, history)
        if (body.length < 6) return
        const list = pendingTexts.get(sessionId) ?? []
        list.push(body)
        if (list.length > 20) list.shift() // 只关心最近几句，别把整个会话攒在内存里
        pendingTexts.set(sessionId, list)
        return
      }

      if (event.type === 'turn/start') {
        turnStart.set(sessionId, new Date().toISOString().replace(/\.\d{3}Z$/, 'Z'))
        return
      }

      if (event.type === 'turn/end') {
        const texts = pendingTexts.get(sessionId)
        pendingTexts.delete(sessionId)
        const since = turnStart.get(sessionId)
        turnStart.delete(sessionId)
        if (texts && texts.length > 0 && since) {
          void extractAndStore(texts, sessionId, since, recentTexts.get(sessionId) ?? [])
        }
        // 回合结束顺手刷新稳定块：别的地方（工具/另一个会话）可能刚写过库。
        void recall.refresh()
        return
      }

      if (event.type === 'session/disposed') {
        pendingTexts.delete(sessionId)
        recentTexts.delete(sessionId)
        turnStart.delete(sessionId)
        recall.forgetSession(sessionId)
      }
    } catch (error) {
      log.warn?.(`dsh-attune-memory: 会话事件处理失败（忽略）：${error?.message ?? error}`)
    }
  })

  /**
   * 兜底抽取：只认明确的自述句式，一条消息最多 3 条，全部按 inferred 记。
   *
   * 但先问一句"这轮模型自己写过吗"：主通道已经记过的东西，兜底再记一遍就是重复。
   * P8 真跑时这里翻过车 —— 同一句自我介绍被写了两遍，一条 0.95 一条 0.65，
   * 槽位还因为 domain_key 不同而互不覆盖。判据是变更流水的 actor：工具写的记 'user'，
   * 抽取写的记 'inferred'，只认前者。
   */
  async function extractAndStore(texts, sessionId, since, history = []) {
    // 四计数（T-006）：抽取通道以前是"静默失败"的代名词 —— preview 挂了几个月，
    // 落库 0 条，界面上看不出任何异常。现在每次 turn/end 都把"试了几次/写成几条/
    // 被闸门拦下几条/失败几次"记进 memory_counter，诊断面板与 memory_probe 都能看见。
    const tally = { attempt: 0, written: 0, blocked: 0, failed: 0 }
    try {
      const changes = await engine.call('recent', { since, limit: 50 })
      if (Array.isArray(changes) && changes.some((change) => change?.actor === 'user')) {
        log.info?.('dsh-attune-memory: 本轮模型已自行写入记忆，跳过兜底抽取')
        await recordExtract(tally, { skipped: 1 })
        return
      }
    } catch (error) {
      log.debug?.(`dsh-attune-memory: 查询变更流水失败（继续抽取）：${error?.message ?? error}`)
      tally.failed += 1
    }

    const tryOne = async (body) => {
      const candidates = extractCandidates(body, { sourceConversationId: sessionId })
      for (const candidate of candidates) {
        tally.attempt += 1
        try {
          // W2：先走候选预览，拿到"会怎么落库"再决定写不写。
          // "该不该记"的判定只留在引擎里，插件不自己臆断；被拦下的那条
          // 由引擎记进丢弃日志（人能在诊断面板里看见为什么不记）。
          const plan = await engine.call('preview', candidate)
          if (!plan || plan.would === 'ignore') {
            tally.blocked += 1
            log.debug?.(`dsh-attune-memory: 候选被闸门拦下（${plan?.reason ?? '未给出理由'}）`)
            continue
          }
          await engine.call('add', candidate)
          tally.written += 1
        } catch (error) {
          tally.failed += 1
          log.debug?.(`dsh-attune-memory: 抽取写入失败（忽略）：${error?.message ?? error}`)
        }
      }
    }

    try {
      for (const body of texts) await tryOne(body)
      // M2：逐句抽不到、但上下文里有碎片时，把最近三句拼起来再试一次
      // （"我叫张三" + "大三" + "郑州的" 单独看都记不下完整画像）。
      if (tally.written === 0 && history.length >= 2) await tryOne(history.slice(-3).join('，'))
    } finally {
      await recordExtract(tally)
    }
  }

  /**
   * 把一轮抽取的四个数字记进引擎。
   *
   * 为什么不在这里直接 log：日志只在开启 debug 时可见，而"抽取默默死了"恰恰是
   * 没人开 debug 时才发生的。计数落库后，`memory_probe` / 诊断面板能直接读出来。
   */
  async function recordExtract(tally, extra = {}) {
    const counters = {
      extract_attempt: tally.attempt,
      extract_written: tally.written,
      extract_blocked: tally.blocked,
      extract_failed: tally.failed,
      ...(extra.skipped ? { extract_skipped: extra.skipped } : {}),
    }
    // 全零且没跳过就不必每次 turn/end 都写库（空轮次太多了）。
    const total = Object.values(counters).reduce((sum, n) => sum + n, 0)
    if (!total) return
    try {
      await engine.call('record', { counters })
    } catch (error) {
      log.debug?.(`dsh-attune-memory: 记抽取计数失败（忽略）：${error?.message ?? error}`)
    }
  }

  // ---------------------------------------------------------------- T 通道
  ctx.on('agent/pre-step', async (payload, next) => {
    const decision = await next()
    if (disposed) return decision
    try {
      if (!recall.manual) await recall.refresh() // 首次装配前把手册补上
      recall.noteTurn(payload?.turn) // O2：自愈指令的频控按轮推进
      return await recall.inject(payload, decision)
    } catch (error) {
      // 注入失败必须放行原始消息：宁可这一轮没有记忆，也不能没有用户的话。
      log.warn?.(`dsh-attune-memory: 注入失败（fail-open）：${error?.message ?? error}`)
      return decision
    }
  })

  // 启动时就拉一次，让第一轮的 S1/S2 通道就有内容（失败也无所谓，pre-step 会再试）。
  void recall.refresh()

  log.info?.(`dsh-attune-memory: 已挂载（engine=${settings.engineDir}，db=${settings.dbPath}）`)
}

function resolveConfig(config) {
  const engineDir = config.engineDir ?? process.env.DSH_ATTUNE_MEMORY_ENGINE_DIR
  if (!engineDir) throw new Error('dsh-attune-memory: 缺少 engineDir 配置（或环境变量 DSH_ATTUNE_MEMORY_ENGINE_DIR）')
  const home = process.env.DSH_HOME ?? join(homedir(), '.dsh')
  const dbPath = config.dbPath ?? process.env.DSH_ATTUNE_MEMORY_DB ?? join(home, 'attune-memory', 'memory.db')
  // SQLite 不会替我们建目录；第一次跑就崩在这里会让人以为是引擎坏了。
  try {
    mkdirSync(dirname(dbPath), { recursive: true })
  } catch {
    /* 权限问题留给引擎报，这里不该拦启动 */
  }
  return {
    engineDir,
    dbPath,
    engineModule: config.engineModule ?? process.env.DSH_ATTUNE_MEMORY_ENGINE_MODULE ?? 'attune_memory.rpc',
    python: config.python ?? process.env.DSH_ATTUNE_MEMORY_PYTHON ?? 'python',
    timeoutMs: Number(config.timeoutMs ?? 5000),
    debug: config.debug === true,
  }
}

export default { name, inject, apply }
