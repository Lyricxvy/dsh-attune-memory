/**
 * 防复发闸：测试里不许再裸写引擎包目录名。
 *
 * 背景：`engine/memory` → 发布树 `engine/attune_memory`（`.publish/rename.py`）。
 * 三次都是同一种写法——测试自己写 `join(ENGINE_DIR, 'memory')`
 * 或 `engineModule: 'attune_memory.rpc'`，源码树绿、发布树 ENOENT / ModuleNotFoundError。
 *
 * 现在包名/模块名只有一个来源：`test/lib/engine-pkg.mjs`。本文件扫描 test/ 下其余
 * .mjs，任何"在 join/cpSync/existsSync/resolve 里直接写死引擎包名"的写法都判红。
 *
 * 跑法：node plugin/dsh-attune-memory/test/layout-guard.test.mjs
 */
import { readdirSync, readFileSync, statSync } from 'node:fs'
import { join, relative } from 'node:path'

import { ENGINE_DIR, MODULE, PKG_DIR, PKG_NAME, copyEnginePkg } from './lib/engine-pkg.mjs'

const HERE = import.meta.dirname
const TEST_ROOT = join(HERE, '..')
/** 探测点自己必然提到包名；本文件是规则的定义者。 */
const ALLOWLIST = new Set(['lib/engine-pkg.mjs', 'layout-guard.test.mjs'].map((p) => join(TEST_ROOT, p)))

/**
 * 一行里是不是"把引擎包名当目录名写死"。
 * 只在 join/cpSync/existsSync/resolve 调用里出现才算——注释里提包名是正常的。
 */
const SUSPECT_CALL = /(?:join|cpSync|existsSync|resolve|mkdirSync)\([^)]*ENGINE_DIR[^)]*['"`](?:attune_memory|memory)['"`]/
/** engineModule 直接写死也是同一类错误。 */
const SUSPECT_MODULE = /engineModule\s*:\s*['"`](?:attune_memory|memory)\.rpc['"`]/

function walk(dir, out = []) {
  for (const entry of readdirSync(dir)) {
    const full = join(dir, entry)
    if (statSync(full).isDirectory()) {
      if (entry === 'node_modules' || entry === '__pycache__') continue
      walk(full, out)
    } else if (entry.endsWith('.mjs')) {
      out.push(full)
    }
  }
  return out
}

const violations = []
for (const file of walk(TEST_ROOT)) {
  if (ALLOWLIST.has(file)) continue
  const lines = readFileSync(file, 'utf8').split('\n')
  lines.forEach((line, i) => {
    const trimmed = line.trim()
    if (trimmed.startsWith('//') || trimmed.startsWith('*') || trimmed.startsWith('/*')) return
    if (SUSPECT_CALL.test(line) || SUSPECT_MODULE.test(line)) {
      violations.push(`${relative(TEST_ROOT, file)}:${i + 1}  ${trimmed}`)
    }
  })
}

const cases = []
function test(name, fn) {
  cases.push({ name, fn })
}

test('引擎包探测结果是自洽的（目录存在、模块名由目录名派生）', () => {
  if (!['attune_memory', 'memory'].includes(PKG_NAME)) throw new Error(`意外的包名：${PKG_NAME}`)
  if (MODULE !== `${PKG_NAME}.rpc`) throw new Error(`MODULE 与 PKG_NAME 不一致：${MODULE} / ${PKG_NAME}`)
  if (!PKG_DIR.endsWith(PKG_NAME)) throw new Error(`PKG_DIR 不以包名结尾：${PKG_DIR}`)
  if (!ENGINE_DIR.endsWith('engine')) throw new Error(`ENGINE_DIR 不像 engine 目录：${ENGINE_DIR}`)
})

test('test/ 下没有裸写引擎包目录名的地方', () => {
  if (violations.length > 0) {
    throw new Error(
      `发现 ${violations.length} 处裸写引擎包名，改用 test/lib/engine-pkg.mjs 的 PKG_DIR / MODULE / copyEnginePkg()：\n  ` +
        violations.join('\n  '),
    )
  }
})

test('copyEnginePkg 的副本目录名与模块名一致（改名后不会 spawn 错模块）', async () => {
  const { mkdtempSync, rmSync } = await import('node:fs')
  const { tmpdir } = await import('node:os')
  const { existsSync } = await import('node:fs')
  const dir = mkdtempSync(join(tmpdir(), 'attune-guard-'))
  try {
    const copy = copyEnginePkg(dir)
    if (copy.name !== PKG_NAME) throw new Error(`副本目录名应跟随实际包名，实际 ${copy.name}`)
    if (!existsSync(join(copy.dir, 'rpc.py'))) throw new Error('副本里没有 rpc.py，复制没生效')
  } finally {
    try {
      rmSync(dir, { recursive: true, force: true, maxRetries: 3 })
    } catch {
      /* Windows 上子进程可能还占着文件，留给临时目录自回收 */
    }
  }
})

let failed = 0
for (const { name, fn } of cases) {
  try {
    await fn()
    console.log(`ok   ${name}`)
  } catch (error) {
    failed += 1
    console.log(`FAIL ${name}\n     ${error.message}`)
  }
}
console.log(`\n${cases.length - failed}/${cases.length} 通过`)
if (failed > 0) process.exit(1)
