/**
 * 引擎包位置的**唯一**探测点（T-016）。
 *
 * 为什么要有这个文件：同一类坑踩了三次（T-009、T-011、T-013）——
 * 测试里直接写 `join(ENGINE_DIR, 'memory')` 或 `engineModule: 'attune_memory.rpc'`。
 * 源码树里包目录叫 `memory`，发布树经 `.publish/rename.py` 改名后叫 `attune_memory`；
 * 写死任何一边，另一边就是 ENOENT / ModuleNotFoundError。
 *
 * 规则（`FILE_LAYOUT.md` I6）：**任何会与改名流水线输出同名的字符串都不可写死**。
 * 包目录名、Python 模块名、测试内临时副本目录名，三者必须从这里的同一个探测结果派生。
 *
 * 所以：测试代码不要再自己探测包名，import 本模块即可。
 */
import { cpSync, existsSync } from 'node:fs'
import { join, resolve } from 'node:path'

const HERE = import.meta.dirname

/** 工作区里的源码树 `engine/`（发布树里同名同级）。 */
export const ENGINE_DIR = resolve(HERE, '../../../..', 'engine')

/** 候选包名：探测目录 + rpc.py 是否存在。注意这里列的是探测输入，不是写死的使用点。 */
const CANDIDATES = ['attune_memory', 'memory']

const found = CANDIDATES.find((name) => existsSync(join(ENGINE_DIR, name, 'rpc.py')))
if (!found) {
  throw new Error(`在 ${ENGINE_DIR} 下找不到引擎包目录（候选：${CANDIDATES.join(' / ')}），请确认源码树完整`)
}

/** 实际存在的包目录名（源码树 `memory` / 发布树 `attune_memory`）。 */
export const PKG_NAME = found

/** 交给 EngineClient 的 `engineModule`，如 `attune_memory.rpc` / `attune_memory.rpc`。 */
export const MODULE = `${PKG_NAME}.rpc`

/** 引擎包目录的绝对路径。 */
export const PKG_DIR = join(ENGINE_DIR, PKG_NAME)

/**
 * 把引擎包复制到 `destRoot` 下，用于"改坏一份副本 / 反向验证"这类测试。
 *
 * 副本目录名默认跟随 `PKG_NAME`：改名后模块引用变成 `attune_memory.rpc`，
 * 若副本目录还叫 `memory`，spawn 出去的 `-m attune_memory.rpc` 就会 ModuleNotFoundError
 * （T-011 真红过这一处）。
 *
 * @param {string} destRoot 目标父目录（通常是 mkdtemp 出来的临时目录）
 * @param {{ destName?: string }} [options]
 * @returns {{ name: string, dir: string }}
 */
export function copyEnginePkg(destRoot, options = {}) {
  const name = options.destName ?? PKG_NAME
  const dir = join(destRoot, name)
  cpSync(PKG_DIR, dir, { recursive: true })
  return { name, dir }
}

/** 复制出来的副本该用哪个 `engineModule`（与目录名保持一致）。 */
export function moduleFor(name = PKG_NAME) {
  return `${name}.rpc`
}
