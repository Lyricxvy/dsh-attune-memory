#!/usr/bin/env node
/**
 * 宿主契约测试（模块 1）。
 *
 * 它不测"插件能不能启动"，只测"我们赖以工作的宿主行为是否仍然成立"：
 *   H1  本轮用户消息在 agent/pre-step 的 decision.messages 里可见
 *   H2  systemPrompt.section 的 text() 每步重算（两轮之间改库，第二轮 system 会变）
 *   H3  pre-step 注入的消息真的进了会话（也就进了最终请求）
 *   H4  压缩之后仍会重注入（压舱石）
 *   H5  注入消息的结构仍被宿主接受（不 TypeError、role/content/source 齐全）
 *
 * 为什么必须跑真实宿主：这五条都不是我们的代码，是 DSH 的行为。单元测试再怎么绿，
 * 宿主升级改掉 pre-step 的时机或 section 的重算规则，效果会静默归零。
 *
 * 用法：
 *   node test/contract/run-contract.mjs              # 全量（真模型，约 40–60s）
 *   node test/contract/run-contract.mjs --offline    # 只跑结构断言（不连模型，2s）
 *   node test/contract/run-contract.mjs --quick      # 跳过压缩那一轮
 *   node test/contract/run-contract.mjs --keep       # 保留临时目录
 *
 * 依赖环境：DSH_INSTALL、DSH_HOME（两者都必须由环境变量给出）、
 *          CONTRACT_PROFILE（默认 contract）。contract profile 必须已装好 dsh-attune-probe 与 dsh-attune-memory。
 */
import { spawn } from 'node:child_process'
import { createRequire } from 'node:module'
import { existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs'
import { join, resolve } from 'node:path'
import { findSessionFile, messageText, readSessionEvents, summarize } from '../lib/session-log.mjs'

const require = createRequire(import.meta.url)

const HERE = resolve(import.meta.dirname, '..', '..') // plugin/dsh-attune-memory
const WORKSPACE = resolve(HERE, '..', '..')

const DSH_INSTALL = process.env.DSH_INSTALL ?? '<DSH_INSTALL>'
const DSH_HOME = process.env.DSH_HOME ?? (() => { throw new Error('请先设置 DSH_HOME 环境变量') })()
const PROFILE = process.env.CONTRACT_PROFILE ?? 'contract'
const PYTHON = process.env.CONTRACT_PYTHON ?? 'python'

/** 契约测试跑在**发布树**上：这是用户实际安装的东西，不是工作区源码。 */
const RELEASE = join(WORKSPACE, 'release', 'dsh-attune-memory')
const ENGINE_DIR = join(RELEASE, 'engine')
const BIN = join(DSH_INSTALL, 'resources', 'app.asar', 'dsh', 'node_modules', '@deepseek-ai', 'dsh', 'lib', 'bin.js')
const EXE = join(DSH_INSTALL, 'DeepSeek Harness.exe')

const OFFLINE = process.argv.includes('--offline')
const QUICK = process.argv.includes('--quick')
const KEEP = process.argv.includes('--keep')

const results = []
const pass = (name, detail) => results.push({ name, status: '通过', detail })
const fail = (name, detail) => results.push({ name, status: '失败', detail })
const skip = (name, detail) => results.push({ name, status: '跳过', detail })

const BASE_PATCH = `# 契约测试叠加层（生成物，勿手改）：打开桌面端的 deepseek-account 路由。
- id: credentials
  disabled: false
- id: authorization
  disabled: false
- id: deepseek-account
  disabled: false
- id: agent-default-model
  config:
    provider: deepseek-account
    model: deepseek-flash
`

const COMPACTION_PATCH = `
# 强制压缩：阈值压到 0.5% 窗口，让第二轮必然触发压缩（真实部署不会这么配）。
# thresholdTokens = floor(min(window * thresholdRatio, window - reservedCompletion - headroom))，
# 因此阈值远小于 system prompt 本身的 token 数，第二轮开工即超压。
- id: compaction-basic
  name: '@deepseek-ai/dsh-compaction-basic'
  config:
    thresholdRatio: 0.005
    headroomTokens: 4096
    retainTokens: 200
    maxTokens: 8192
`

function patchFor({ dbPath, compaction }) {
  return `${BASE_PATCH}
- id: dsh-attune-memory
  config:
    engineDir: ${ENGINE_DIR.replaceAll('\\', '/')}
    dbPath: ${dbPath.replaceAll('\\', '/')}
    debug: true
${compaction ? COMPACTION_PATCH : ''}`
}

/** 跑一次 headless，返回 {code, events, stderr, sessionId, out}。 */
function runHeadless({ task, patchFile, logDir, sessionId }) {
  const args = ['--profile', PROFILE, '--patch', patchFile, '--json']
  if (sessionId) args.push('--session-id', sessionId)
  args.push(task)

  return new Promise((resolvePromise) => {
    const child = spawn(EXE, [BIN, ...args], {
      cwd: WORKSPACE,
      env: {
        ...process.env,
        DSH_HOME,
        ELECTRON_RUN_AS_NODE: '1',
        ATTUNE_CONTRACT_LOG: join(logDir, 'probe.jsonl'),
        PYTHONIOENCODING: 'utf-8',
        PYTHONUTF8: '1',
      },
      windowsHide: true,
    })
    let out = ''
    let err = ''
    const timer = setTimeout(() => {
      try {
        child.kill()
      } catch {
        /* ignore */
      }
    }, 240_000)
    child.stdout.on('data', (chunk) => {
      out += String(chunk)
    })
    child.stderr.on('data', (chunk) => {
      err += String(chunk)
    })
    child.on('close', (code) => {
      clearTimeout(timer)
      const events = []
      for (const line of out.split('\n')) {
        if (!line.trim()) continue
        try {
          events.push(JSON.parse(line))
        } catch {
          /* 非 JSON 行（诊断）忽略 */
        }
      }
      const session = events.find((event) => event.type === 'session')
      writeFileSync(join(logDir, 'stdout.jsonl'), out, 'utf8')
      writeFileSync(join(logDir, 'stderr.txt'), err, 'utf8')
      resolvePromise({ code, events, out, err, sessionId: session?.sessionId ?? null })
    })
  })
}

/** 直接对引擎发几条 RPC（用于播种，绕过插件）。 */
async function rpcSeed(dbPath, ops) {
  const child = spawn(PYTHON, ['-m', 'attune_memory.rpc', dbPath], {
    cwd: ENGINE_DIR,
    env: { ...process.env, PYTHONIOENCODING: 'utf-8', PYTHONUTF8: '1' },
    windowsHide: true,
    stdio: ['pipe', 'pipe', 'pipe'],
  })
  let out = ''
  child.stdout.on('data', (chunk) => {
    out += String(chunk)
  })
  ops.forEach((op, index) => {
    child.stdin.write(`${JSON.stringify({ id: index + 1, op: op.op, args: op.args })}\n`)
  })
  await new Promise((done) => {
    const timer = setTimeout(() => {
      child.kill()
      done()
    }, 20_000)
    child.on('close', () => {
      clearTimeout(timer)
      done()
    })
    child.stdin.end()
  })
  return out
    .split('\n')
    .filter((line) => line.trim())
    .map((line) => JSON.parse(line))
}

function readProbe(file) {
  if (!existsSync(file)) return []
  return readFileSync(file, 'utf8')
    .split('\n')
    .filter((line) => line.trim())
    .map((line) => {
      try {
        return JSON.parse(line)
      } catch {
        return null
      }
    })
    .filter(Boolean)
}

function short(text, n = 42) {
  const flat = String(text ?? '').replace(/\s+/g, ' ')
  return flat.length > n ? `${flat.slice(0, n)}…` : flat
}

async function main() {
  // 离线：只做结构断言。CI/构建后跑这一档，2 秒完成，不需要 DSH 在场。
  if (OFFLINE) {
    const { createMemoryMessage, isHumanMessage } = await import('../../lib/message.js')
    const note = createMemoryMessage('[长期记忆 · 本次相关]\n- 技能：示例', { form: 'recall', memoryIds: ['m-x'] })
    const okShape =
      note && note.role === 'user' && Array.isArray(note.content) && note.content[0]?.type === 'text' && note.source?.kind === 'dsh-attune-memory' && note.source?.form === 'recall'
    if (okShape) pass('H5 注入消息结构', 'role/content/source 齐全（离线结构断言）')
    else fail('H5 注入消息结构', `结构不符：${JSON.stringify(note)?.slice(0, 180)}`)
    if (isHumanMessage({ role: 'user', source: { kind: 'user' } }) && !isHumanMessage({ role: 'user', source: { kind: 'runtime-context' } })) {
      pass('H1 真人消息识别', 'source.kind 判定未被误改（离线）')
    } else {
      fail('H1 真人消息识别', 'isHumanMessage 判定与宿主 source.kind 约定不一致')
    }
    return report()
  }

  // bin.js 在 app.asar 里：普通 Node 的 fs 看不见 asar 内部，只有 Electron 能读，
  // 所以这里只校验 Electron 可执行文件存在，不去 stat asar 里的路径。
  if (!existsSync(EXE)) {
    console.error(`找不到 DSH：${EXE}`)
    process.exit(2)
  }

  if (!existsSync(join(DSH_HOME, 'profiles', PROFILE, 'package.json'))) {
    console.error(`contract profile 不存在：${join(DSH_HOME, 'profiles', PROFILE)}\n先跑 test/contract/setup-profile.mjs`)
    process.exit(2)
  }

  const root = join(WORKSPACE, '.contract')
  mkdirSync(root, { recursive: true })
  const runDir = mkdtempSync(join(root, 'run-'))
  const dbPath = join(runDir, 'memory.db')
  const patchA = join(runDir, 'patch-a.yml')
  const patchB = join(runDir, 'patch-b.yml')
  writeFileSync(patchA, patchFor({ dbPath, compaction: false }), 'utf8')
  writeFileSync(patchB, patchFor({ dbPath, compaction: true }), 'utf8')

  const logA = join(runDir, 'run1')
  const logFill = join(runDir, 'run-fill')
  const logB = join(runDir, 'run2')
  mkdirSync(logA, { recursive: true })
  mkdirSync(logFill, { recursive: true })
  mkdirSync(logB, { recursive: true })

  // 播种一条 contextual 记忆，让第一轮一定有东西可注入（否则 H3 无从判定）。
  const seeded = await rpcSeed(dbPath, [
    {
      op: 'add',
      args: {
        content: '用户做数学一极限题时总是想不到变形方向。',
        type: 'profile',
        subtype: 'skill',
        domain_key: 'study.math',
        injection_policy: 'contextual',
        importance: 'important',
        confidence: 0.9,
        keywords: '数学一,极限,变形',
      },
    },
  ])
  if (!seeded.every((entry) => entry.ok)) {
    console.error(`播种失败：${JSON.stringify(seeded).slice(0, 400)}`)
    process.exit(2)
  }

  const task1 = '数学一的极限题我总是想不到变形方向，帮我理一下思路，两句话就够。'
  const run1 = await runHeadless({ task: task1, patchFile: patchA, logDir: logA })
  if (run1.code !== 0 || !run1.sessionId) {
    fail('H0 首轮能跑通', `exit=${run1.code} session=${run1.sessionId ?? 'null'}；stderr：${short(run1.err, 300)}`)
    return report(runDir)
  }
  const probe1 = readProbe(join(logA, 'probe.jsonl'))
  const steps1 = probe1.filter((entry) => entry.kind === 'pre-step')
  const file1 = findSessionFile(DSH_HOME, run1.sessionId)
  const log1 = file1 ? summarize(readSessionEvents(file1).events) : null

  // H1：pre-step 里能看到本轮真人消息
  const humanSeen = steps1.find((entry) => entry.lastHuman && entry.lastHuman.head?.length > 0)
  if (humanSeen && task1.includes(humanSeen.lastHuman.head.slice(0, 12))) {
    pass('H1 本轮用户消息可见', `turn=${humanSeen.turn} step=${humanSeen.step} head="${short(humanSeen.lastHuman.head)}"`)
  } else {
    fail('H1 本轮用户消息可见', `probe 里没有真人消息；steps=${steps1.length}`)
  }

  // H3：注入真的进了会话，且排在真人消息之前
  const injected1 = log1?.injected ?? []
  const firstInjected = injected1[0]
  const humanSeq = log1?.humanUserSeqs?.[0]
  if (firstInjected && humanSeq && firstInjected.seq < humanSeq) {
    pass('H3 注入进入最终请求', `seq=${firstInjected.seq} < 真人 seq=${humanSeq}，ids=${firstInjected.ids.length}`)
  } else if (firstInjected) {
    fail('H3 注入进入最终请求', `注入 seq=${firstInjected.seq} 未排在真人（seq=${humanSeq}）之前`)
  } else {
    fail('H3 注入进入最终请求', `会话里没有 dsh-attune-memory 消息；steps=${steps1.length}`)
  }

  // H5：结构被接受（没有 pre-step 异常，注入消息字段齐全）
  const errors = probe1.filter((entry) => entry.kind === 'pre-step-error')
  const shapeOk = injected1.every((entry) => entry.text.length > 0)
  if (errors.length === 0 && shapeOk) pass('H5 消息结构兼容', '无 pre-step 异常；注入正文可读')
  else fail('H5 消息结构兼容', `异常 ${errors.length} 条：${short(errors[0]?.error, 200)}`)

  // 第二轮：先往库里加一条 always 记忆（H2 的变量），再用低阈值补丁恢复会话（H4）。
  const seeded2 = await rpcSeed(dbPath, [
    {
      op: 'add',
      args: {
        content: '契约测试稳定事实：用户名叫赵六。',
        type: 'profile',
        subtype: 'identity',
        injection_policy: 'always',
        importance: 'key',
        confidence: 0.95,
      },
    },
  ])
  if (!seeded2.every((entry) => entry.ok)) {
    console.error(`第二轮播种失败：${JSON.stringify(seeded2).slice(0, 400)}`)
    return report(runDir)
  }

  if (QUICK) {
    skip('H2 section 每步重算', '--quick 跳过第二轮')
    skip('H4 压缩后重注入', '--quick 跳过第二轮')
    return report(runDir)
  }

  // 中间垫一轮长回答：压缩要求"摘要必须比被遮蔽内容小"，
  // 历史太薄时宿主会拒绝压缩（compaction/end 带 error），H4 就永远测不到。
  const fillerTask =
    '请用大约 900 字，把考研数学一极限题的七种未定式分别该用什么方法讲清楚：每种给出判断依据、典型变形方向和一个短例子。不要省略，不要只列标题。'
  const runFill = await runHeadless({ task: fillerTask, patchFile: patchA, logDir: logFill, sessionId: run1.sessionId })
  if (runFill.code !== 0 || !runFill.sessionId) {
    fail('H4 压缩后重注入', `垫场轮没跑通：exit=${runFill.code}`)
    return report(runDir)
  }

  const task2 = '你还记得我叫什么吗？一句话回答。'
  const run2 = await runHeadless({ task: task2, patchFile: patchB, logDir: logB, sessionId: run1.sessionId })
  const file2 = findSessionFile(DSH_HOME, run1.sessionId)
  const log2 = file2 ? summarize(readSessionEvents(file2).events) : null
  if (run2.code !== 0 || !log2) {
    fail('H2 section 每步重算', `第二轮没跑通：exit=${run2.code}`)
    fail('H4 压缩后重注入', `第二轮没跑通：exit=${run2.code}`)
    return report(runDir)
  }

  // H2：恢复会话后的 system prompt 应包含"上一轮之后才写进去"的那条事实
  const lastTurn = Math.max(0, ...log2.systemMessages.map((entry) => entry.turn ?? 0))
  const turn2System = log2.systemMessages.filter((entry) => entry.turn === lastTurn)
  const hasStable = turn2System.some((entry) => entry.text.includes('赵六'))
  if (turn2System.length > 0 && hasStable) {
    pass('H2 section 每步重算', `turn${lastTurn} system ${turn2System[0].text.length} 字符，含新增事实「赵六」`)
  } else if (turn2System.length === 0) {
    fail('H2 section 每步重算', '恢复的会话里没有 system/message 事件')
  } else {
    fail('H2 section 每步重算', `turn${lastTurn} system 不含新增事实（${turn2System[0].text.length} 字符）—— section 可能只在启动时算了一次`)
  }

  // H4：压缩发生 + 压缩后仍有记忆注入
  const compactionStart = log2.compaction.filter((entry) => entry.phase === 'compaction/start')
  const compactionEnd = log2.compaction.filter((entry) => entry.phase === 'compaction/end')
  const succeeded = compactionEnd.filter((entry) => entry.error === null)
  const failedCompaction = compactionEnd.filter((entry) => entry.error !== null)
  // 「压缩后注入」用序号判定：user/message 事件里不一定带 turn，
  // 而注入必须发生在 compaction/end 之后才有意义。
  const lastCompactionSeq = succeeded.at(-1)?.seq ?? -1
  const reinjected = log2.injected.filter((entry) => entry.seq > lastCompactionSeq)
  if (compactionStart.length === 0) {
    skip('H4 压缩后重注入', '本轮没有触发压缩（阈值没被打到），无法判定')
  } else if (succeeded.length === 0) {
    skip('H4 压缩后重注入', `宿主放弃压缩，无法判定：${short(failedCompaction[0]?.error, 120)}`)
  } else if (reinjected.length > 0) {
    const forced = reinjected.filter((entry) => entry.form === 'reinject' || entry.afterCompaction)
    pass(
      'H4 压缩后重注入',
      `压缩成功 ${succeeded.length} 次；其后注入 ${reinjected.length} 条，其中 ${forced.length} 条 form=reinject、ids=${reinjected[0].ids.length}`,
    )
  } else {
    fail('H4 压缩后重注入', `压缩成功（${succeeded.length} 次）但这一轮没有任何记忆注入`)
  }

  if (!run2.err.includes('Error')) {
    // 只有诊断，不算失败；这里只把 stderr 首行放进报告，便于排查。
  }

  return report(runDir)
}

function report(runDir) {
  const width = Math.max(...results.map((entry) => entry.name.length), 8)
  console.log('')
  for (const entry of results) {
    console.log(`${entry.name.padEnd(width)} ... ${entry.status}  ${entry.detail ?? ''}`)
  }
  const failed = results.filter((entry) => entry.status === '失败').length
  const skipped = results.filter((entry) => entry.status === '跳过').length
  console.log(`\n${results.length - failed - skipped}/${results.length - skipped} 通过${skipped ? `，${skipped} 跳过` : ''}`)
  if (!KEEP && runDir) {
    try {
      rmSync(runDir, { recursive: true, force: true })
    } catch {
      /* 清理失败不影响结论 */
    }
  } else if (runDir) {
    console.log(`临时目录保留：${runDir}`)
  }
  process.exit(failed > 0 ? 1 : 0)
}

main().catch((error) => {
  console.error(`契约测试自身失败：${error?.stack ?? error}`)
  process.exit(2)
})
