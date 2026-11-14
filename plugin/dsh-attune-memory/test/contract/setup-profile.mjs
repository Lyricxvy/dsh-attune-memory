#!/usr/bin/env node
/**
 * 准备契约测试用的 profile（幂等，可反复跑）。
 *
 * 为什么要单独一个 profile：
 *   - desktop profile 被 Electron 独占，CLI 不能启动它；
 *   - 契约测试要能改压缩阈值（H4），不能动真实使用的 profile；
 *   - 测试的是**发布树**里的插件，所以依赖指向 release/dsh-attune-memory。
 *
 * 做的事：复制 headless 模板 → 写 package.json（两个依赖 + 两个 bundle）→ 建两个目录联接。
 */
import { spawnSync } from 'node:child_process'
import { existsSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs'
import { join, resolve } from 'node:path'

const HERE = resolve(import.meta.dirname, '..', '..')
const WORKSPACE = resolve(HERE, '..', '..')
const DSH_INSTALL = process.env.DSH_INSTALL ?? '<DSH_INSTALL>'
const DSH_HOME = process.env.DSH_HOME ?? '<DSH_HOME>'
const PROFILE = process.env.CONTRACT_PROFILE ?? 'contract'
const HOME = join(DSH_HOME, 'profiles', PROFILE)
const BIN = join(DSH_INSTALL, 'resources', 'app.asar', 'dsh', 'node_modules', '@deepseek-ai', 'dsh', 'lib', 'bin.js')
const EXE = join(DSH_INSTALL, 'DeepSeek Harness.exe')

const PLUGIN = join(WORKSPACE, 'release', 'dsh-attune-memory', 'plugin', 'dsh-attune-memory')
const PROBE = join(HERE, 'test', 'contract', 'probe')

if (!existsSync(PLUGIN)) {
  console.error(`发布树不存在：${PLUGIN}\n先跑 .publish/build-release.ps1（契约测试测的是发布树，不是源码）`)
  process.exit(2)
}

if (!existsSync(join(HOME, 'package.json'))) {
  console.log(`创建 profile：${PROFILE}`)
  const result = spawnSync(EXE, [BIN, PROFILE, '--from-default-profile', 'headless', '--dump-config'], {
    env: { ...process.env, DSH_HOME, ELECTRON_RUN_AS_NODE: '1' },
    encoding: 'utf8',
    windowsHide: true,
  })
  if (!existsSync(join(HOME, 'package.json'))) {
    console.error(`创建失败：${result.stderr || result.stdout}`)
    process.exit(2)
  }
}

const manifestPath = join(HOME, 'package.json')
const manifest = JSON.parse(readFileSync(manifestPath, 'utf8'))
manifest.dependencies = {
  'dsh-attune-probe': `file:${PROBE.replaceAll('\\', '/')}`,
  'dsh-attune-memory': `file:${PLUGIN.replaceAll('\\', '/')}`,
}
manifest.dsh ??= { profile: {} }
manifest.dsh.profile.bundles = ['@deepseek-ai/dsh-base', '@deepseek-ai/dsh-headless', 'dsh-attune-probe', 'dsh-attune-memory']
writeFileSync(manifestPath, `${JSON.stringify(manifest, null, 2)}\n`, 'utf8')

const nodeModules = join(HOME, 'node_modules')
mkdirSync(nodeModules, { recursive: true })
for (const [name, target] of [
  ['dsh-attune-memory', PLUGIN],
  ['dsh-attune-probe', PROBE],
]) {
  const link = join(nodeModules, name)
  if (existsSync(link)) continue
  const ps = spawnSync(
    'powershell.exe',
    ['-NoProfile', '-Command', `New-Item -ItemType Junction -Path '${link}' -Target '${target}' | Out-Null`],
    { encoding: 'utf8', windowsHide: true },
  )
  if (ps.status !== 0) {
    console.error(`建联接失败：${link}\n${ps.stderr}`)
    process.exit(2)
  }
}

console.log(`profile 就绪：${HOME}`)
console.log(`  dependencies: dsh-attune-memory=${PLUGIN}`)
console.log(`                dsh-attune-probe =${PROBE}`)
console.log(`  bundles     : base, headless, probe, attune-memory`)
