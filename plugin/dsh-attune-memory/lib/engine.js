/**
 * Memory engine 客户端：常驻一个 Python 子进程，用 stdio 上的行分隔 JSON 通信。
 *
 * 为什么是子进程而不是把逻辑写进 JS（见 ADR-008）：
 *   - engine 必须能脱离宿主单独测（P7 的引擎测试就是这么跑的）；
 *   - 记忆读写失败绝不能拖垮整轮对话，子进程崩了重建即可，宿主只 fail-open；
 *   - stdin/stdout 有背压，比 HTTP 少一层端口与鉴权。
 *
 * 这个文件只做三件事：拉进程、发请求、把异常翻译成 EngineError。
 * 它不认识"记忆"是什么 —— 语义都在 engine/attune_memory/ 里。
 */
import { spawn } from 'node:child_process'
import { randomUUID } from 'node:crypto'
import { createInterface } from 'node:readline'

/**
 * 写类操作的**引导名单**。真正的权威是引擎的 OPS 注册表（`describe` 里的 writes 标志）。
 *
 * 这份常量只在两种时候有用：自检还没跑完，以及引擎明确报错退出（那说明子进程有异常）。
 * 自检通过后 `#writeOps` 会被引擎的名单整体替换 —— 以前两边各写一份，于是出现了
 * `tick` 只在 Python 侧算写操作的漂移（审计第 5 条）。
 */
const WRITE_OPS = new Set(['add', 'update', 'supersede', 'merge', 'forget', 'purge', 'restore', 'archive', 'delete', 'tick'])
/** 可以安全重试的错误：请求可能根本没被处理，或者进程已经死了。 */
const RETRYABLE = new Set(['TIMEOUT', 'ENGINE_DEAD'])

export class EngineError extends Error {
  constructor(message, { code = 'ENGINE_ERROR', detail } = {}) {
    super(message)
    this.name = 'EngineError'
    this.code = code
    this.detail = detail
  }
}

/** 一行 JSON 里最多塞这么多字符就认为对面坏了（正常响应远小于此）。 */
const MAX_LINE = 4 * 1024 * 1024

export class EngineClient {
  #python
  #engineDir
  #module
  #dbPath
  #timeoutMs
  #log
  #debug
  #child = null
  #pending = new Map()
  #nextId = 1
  #queue = Promise.resolve()
  #closed = false
  // 契约自检状态：pending 未跑 | ok 一致 | mismatch 不一致 | unavailable 自检跑不起来（都是写操作禁用）
  #contract = 'pending'
  #contractPromise = null
  #contractDetail = ''
  #writeOps = new Set(WRITE_OPS)

  constructor({ python = 'python', engineDir, engineModule = 'attune_memory.rpc', dbPath, timeoutMs = 5000, log, debug = false, writeOps }) {
    // writeOps 只给测试用：故意传一份错的名单，验证自检会红。
    if (writeOps) this.#writeOps = new Set(writeOps)
    if (!engineDir) throw new EngineError('engineDir 未配置：找不到 memory 引擎目录', { code: 'BAD_CONFIG' })
    this.#python = python
    this.#engineDir = engineDir
    // 发布树里包名是 attune_memory；源码树里是 memory。测试要能指过去。
    this.#module = engineModule
    this.#dbPath = dbPath
    this.#timeoutMs = timeoutMs
    this.#log = log
    this.#debug = debug
  }

  get running() {
    return this.#child !== null && this.#child.exitCode === null
  }

  /**
   * 调一个引擎操作。串行排队：记忆库的写本来就要串行，并发只会让 SQLite 抢锁。
   * @param {string} op
   * @param {object} args
   */
  call(op, args = {}, options = {}) {
    const write = this.#writeOps.has(op)
    // 写操作默认带幂等键：调用方没给就造一个，重试时复用同一个。
    const dedupe = write ? (options.dedupe ?? randomUUID()) : undefined
    const body = dedupe ? { ...args, __dedupe: dedupe } : args
    const attempts = options.retries ?? (write ? 1 : 1)
    const run = async () => {
      await this.#ensureContract()
      // 契约没对齐（或压根拿不到）时只禁写：读失败顶多少记一条，写错会污染记忆库。
      if (write && this.#contract !== 'ok') {
        const why = this.#contract === 'mismatch'
          ? `引擎契约与插件白名单不一致：${this.#contractDetail}`
          : `引擎不支持 describe，无法校验写操作契约（${this.#contractDetail || '自检未完成'}）`
        throw new EngineError(`${why}；写操作已禁用（读操作不受影响）`, { code: 'BAD_CONFIG' })
      }
      return this.#withRetry(op, body, attempts, write)
    }
    const result = this.#queue.then(run, run)
    // 队列本身不能因为某一次失败而断掉，所以把 rejection 就地吞掉再抛给调用方。
    this.#queue = result.then(
      () => undefined,
      () => undefined,
    )
    return result
  }

  /**
   * 启动自检：问引擎要 describe，比对写操作集合。
   *
   * 为什么是"写操作"而不是"全部 op"：读操作多一个少一个最多是功能缺失，
   * 写操作判错却会让同一份数据被写两遍或写不进去 —— 这是必须 fail-closed 的那一半。
   */
  #ensureContract() {
    if (this.#contract !== 'pending') return Promise.resolve()
    this.#contractPromise ??= this.#runContractCheck()
    return this.#contractPromise
  }

  async #runContractCheck() {
    try {
      const rows = await this.#request('describe', {})
      const engineOps = new Set(rows.filter((row) => row?.writes).map((row) => row.op))
      const onlyLocal = [...this.#writeOps].filter((op) => !engineOps.has(op)).sort()
      const onlyEngine = [...engineOps].filter((op) => !this.#writeOps.has(op)).sort()
      if (onlyLocal.length || onlyEngine.length) {
        this.#contract = 'mismatch'
        this.#contractDetail = `插件多 ${JSON.stringify(onlyLocal)}、引擎多 ${JSON.stringify(onlyEngine)}`
        this.#log?.error?.(`dsh-attune-memory 契约自检失败：${this.#contractDetail}；写操作已禁用，读操作不受影响`)
        return
      }
      // 以引擎为准：从此不再看本地常量。
      this.#writeOps = engineOps
      this.#contract = 'ok'
    } catch (error) {
      // describe 不可用 = 引擎与插件版本不匹配（v0.2.0 起引擎必然实现 describe）。
      // 这里**不能**退回本地名单：那等于"校验失败就用未经校验的假设继续写"，
      // 正是评审指出的绕过路径。改为契约不可用 → 写操作 fail-closed，读照常。
      this.#contract = 'unavailable'
      this.#contractDetail = `${error?.code ?? 'UNKNOWN'}: ${message(error)}`
      this.#log?.error?.(
        `dsh-attune-memory 契约自检失败（${this.#contractDetail}）；`
        + '写操作已禁用，读操作不受影响。请确认插件与引擎是同一版本（引擎需实现 describe）。',
      )
    }
  }

  /** 自检状态，测试与 memory_probe 用。 */
  get contract() {
    return { state: this.#contract, detail: this.#contractDetail, writeOps: [...this.#writeOps].sort() }
  }

  async #withRetry(op, args, attempts, write) {
    let lastError
    for (let i = 0; i <= attempts; i += 1) {
      try {
        return await this.#request(op, args)
      } catch (error) {
        lastError = error
        // 只重试"可能没被处理"的错误；ENGINE_REJECTED 是引擎明确拒绝，重试没意义。
        if (!RETRYABLE.has(error?.code) || i === attempts) break
        this.#log?.warn?.(`dsh-attune-memory ${op} 失败（${error.code}），重试一次`)
      }
    }
    throw lastError
  }

  #request(op, args) {
    if (this.#closed) return Promise.reject(new EngineError('engine 已关闭', { code: 'CLOSED' }))
    return new Promise((resolve, reject) => {
      const child = this.#ensureChild()
      const id = this.#nextId++
      // 写进管道就算"可能已经生效"：进程可能收到并执行了，只是没来得及回。
      // 这个标记最终会进 EngineError.detail，让上层把"可能已写入"和"肯定没写"分开。
      let sent = false
      const fail = (error) => {
        if (sent && error && !error.mayHaveApplied) error.mayHaveApplied = true
        reject(error)
      }
      const timer = setTimeout(() => {
        this.#pending.delete(id)
        fail(new EngineError(`engine ${op} 超时（${this.#timeoutMs}ms）`, { code: 'TIMEOUT' }))
      }, this.#timeoutMs)
      if (typeof timer.unref === 'function') timer.unref()
      this.#pending.set(id, { resolve, reject: fail, timer, op })
      try {
        child.stdin.write(`${JSON.stringify({ id, op, args })}\n`)
        sent = true
      } catch (error) {
        clearTimeout(timer)
        this.#pending.delete(id)
        reject(new EngineError(`engine 写入失败：${message(error)}`, { code: 'WRITE_FAILED' }))
      }
    })
  }

  #ensureChild() {
    if (this.running) return this.#child
    const env = {
      ...process.env,
      PYTHONIOENCODING: 'utf-8',
      PYTHONUTF8: '1',
      PYTHONDONTWRITEBYTECODE: '1',
    }
    const child = spawn(this.#python, ['-m', this.#module, this.#dbPath], {
      cwd: this.#engineDir,
      env,
      stdio: ['pipe', 'pipe', 'pipe'],
      windowsHide: true,
    })
    this.#child = child
    const rl = createInterface({ input: child.stdout, crlfDelay: Infinity })
    rl.on('line', (line) => this.#onLine(line))
    // stderr 只当诊断信息，不当数据；引擎把 traceback 也塞在响应里了。
    child.stderr.on('data', (chunk) => {
      const text = String(chunk).trim()
      if (text) this.#log?.warn?.(`dsh-attune-memory engine stderr: ${text.slice(0, 800)}`)
    })
    child.on('error', (error) => {
      this.#failAll(`engine 启动失败：${message(error)}`)
      this.#child = null
    })
    child.on('exit', (code, signal) => {
      const detail = signal ? `signal ${signal}` : `code ${code}`
      this.#failAll(`engine 退出（${detail}）`)
      this.#child = null
      if (this.#debug && !this.#closed) this.#log?.info?.(`dsh-attune-memory engine exited: ${detail}`)
    })
    return child
  }

  #onLine(line) {
    if (!line) return
    if (line.length > MAX_LINE) {
      this.#failAll('engine 输出过长，判定为协议错乱')
      return
    }
    let payload
    try {
      payload = JSON.parse(line)
    } catch {
      this.#log?.warn?.(`dsh-attune-memory engine 非 JSON 输出：${line.slice(0, 200)}`)
      return
    }
    const entry = this.#pending.get(payload.id)
    if (!entry) return
    this.#pending.delete(payload.id)
    clearTimeout(entry.timer)
    if (payload.ok) entry.resolve(payload.result)
    else {
      const error = new EngineError(String(payload.error ?? `engine ${entry.op} 失败`), {
        code: 'ENGINE_REJECTED',
        detail: payload.traceback,
      })
      if (this.#debug && payload.traceback) this.#log?.warn?.(String(payload.traceback))
      entry.reject(error)
    }
  }

  #failAll(reason) {
    for (const [, entry] of this.#pending) {
      clearTimeout(entry.timer)
      entry.reject(new EngineError(reason, { code: 'ENGINE_DEAD' }))
    }
    this.#pending.clear()
  }

  /** 仅测试用：拿到子进程句柄，好模拟"引擎被杀"。 */
  get __childForTest() {
    return this.#child
  }

  close() {
    this.#closed = true
    this.#failAll('engine 已关闭')
    const child = this.#child
    this.#child = null
    if (!child) return
    try {
      child.stdin.end()
    } catch {
      /* 已经关了就算了 */
    }
    // 给引擎 300ms 优雅退出，超时就强杀：不能让宿主退出卡在子进程上。
    const timer = setTimeout(() => {
      try {
        child.kill()
      } catch {
        /* ignore */
      }
    }, 300)
    if (typeof timer.unref === 'function') timer.unref()
    child.once('exit', () => clearTimeout(timer))
  }
}

function message(error) {
  return error instanceof Error ? error.message : String(error)
}
