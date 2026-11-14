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
 * 写类操作。它们会在库里留下痕迹，所以：
 *   - 每次调用都带一个 `dedupe` 键（同一次逻辑写入的多次重试共用一个键）；
 *   - 只有带同一个键的重复请求才会在引擎侧被识别成"重复"并原样返回上次的响应。
 */
const WRITE_OPS = new Set(['add', 'update', 'supersede', 'merge', 'forget', 'purge', 'restore', 'archive', 'delete'])
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

  constructor({ python = 'python', engineDir, engineModule = 'attune_memory.rpc', dbPath, timeoutMs = 5000, log, debug = false }) {
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
    const write = WRITE_OPS.has(op)
    // 写操作默认带幂等键：调用方没给就造一个，重试时复用同一个。
    const dedupe = write ? (options.dedupe ?? randomUUID()) : undefined
    const body = dedupe ? { ...args, __dedupe: dedupe } : args
    const attempts = options.retries ?? (write ? 1 : 1)
    const run = () => this.#withRetry(op, body, attempts, write)
    const result = this.#queue.then(run, run)
    // 队列本身不能因为某一次失败而断掉，所以把 rejection 就地吞掉再抛给调用方。
    this.#queue = result.then(
      () => undefined,
      () => undefined,
    )
    return result
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
