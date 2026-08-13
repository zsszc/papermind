#!/usr/bin/env node

const crypto = require('node:crypto')
const fs = require('node:fs')
const path = require('node:path')
const { spawnSync } = require('node:child_process')

const manifest = require('../runtime-manifest.json')

const electronRoot = path.resolve(__dirname, '..')
const projectRoot = path.resolve(electronRoot, '..')
const runtimeRoot = path.join(electronRoot, 'runtime')
const backendSource = path.join(projectRoot, 'backend')
const backendTarget = path.join(runtimeRoot, 'backend')
const pythonTarget = path.join(backendTarget, 'venv')
const cacheRoot = path.join(electronRoot, '.runtime-cache')


function selectRuntime(source, platform, arch) {
  const normalizedArch = arch === 'x86_64' ? 'x64' : arch
  const runtime = source.runtimes[`${platform}-${normalizedArch}`]
  if (!runtime) throw new Error(`不支持的桌面运行时: ${platform}-${normalizedArch}`)
  return runtime
}


function validateRuntimeManifest(source) {
  const errors = []
  if (!/^3\.12\.\d+$/.test(source.pythonVersion || '')) errors.push('pythonVersion 必须锁定到 Python 3.12 补丁版本')
  for (const key of ['darwin-arm64', 'darwin-x64']) {
    const runtime = source.runtimes?.[key]
    if (!runtime) {
      errors.push(`缺少运行时: ${key}`)
      continue
    }
    if (!runtime.url?.startsWith('https://github.com/astral-sh/python-build-standalone/')) {
      errors.push(`运行时 URL 非官方源: ${key}`)
    }
    if (!/^[a-f0-9]{64}$/.test(runtime.sha256 || '')) errors.push(`SHA-256 非法: ${key}`)
    if (!runtime.filename?.endsWith('-install_only_stripped.tar.gz')) errors.push(`制品类型非法: ${key}`)
  }
  return errors
}


function computeBuildFingerprint(runtime, requirements) {
  return crypto.createHash('sha256')
    .update(runtime.sha256)
    .update('\0')
    .update(requirements)
    .digest('hex')
}


function run(command, args, options = {}) {
  const result = spawnSync(command, args, { stdio: 'inherit', ...options })
  if (result.error) throw result.error
  if (result.status !== 0) throw new Error(`${command} 退出码 ${result.status}`)
}


function sha256File(filePath) {
  return crypto.createHash('sha256').update(fs.readFileSync(filePath)).digest('hex')
}


function ensureArchive(runtime) {
  fs.mkdirSync(cacheRoot, { recursive: true })
  const archive = path.join(cacheRoot, runtime.filename)
  if (fs.existsSync(archive) && sha256File(archive) === runtime.sha256) return archive
  run('curl', [
    '-L', '--fail', '--show-error', '--retry', '8', '--retry-all-errors',
    '--continue-at', '-', '--output', archive, runtime.url,
  ])
  const actual = sha256File(archive)
  if (actual !== runtime.sha256) {
    fs.rmSync(archive, { force: true })
    throw new Error(`Python 运行时 SHA-256 不匹配: ${actual}`)
  }
  return archive
}


function refreshBackendCode() {
  fs.mkdirSync(backendTarget, { recursive: true })
  fs.rmSync(path.join(backendTarget, 'app'), { recursive: true, force: true })
  fs.cpSync(path.join(backendSource, 'app'), path.join(backendTarget, 'app'), {
    recursive: true,
    filter: (source) => !source.split(path.sep).includes('__pycache__') && !source.endsWith('.pyc'),
  })
  for (const filename of ['requirements.txt', 'pyproject.toml']) {
    fs.copyFileSync(path.join(backendSource, filename), path.join(backendTarget, filename))
  }
}


function prepareRuntime({ platform = process.platform, arch = process.env.PAPERMIND_TARGET_ARCH || process.arch } = {}) {
  const errors = validateRuntimeManifest(manifest)
  if (errors.length > 0) throw new Error(errors.join('\n'))

  const runtime = selectRuntime(manifest, platform, arch)
  const requirementsPath = path.join(backendSource, 'requirements.txt')
  const requirements = fs.readFileSync(requirementsPath)
  const fingerprint = computeBuildFingerprint(runtime, requirements)
  const marker = path.join(pythonTarget, '.papermind-runtime.json')
  let currentFingerprint = null
  try {
    currentFingerprint = JSON.parse(fs.readFileSync(marker, 'utf8')).fingerprint
  } catch {
    // 缺失或旧格式 marker 时重建运行时。
  }

  if (currentFingerprint !== fingerprint) {
    const archive = ensureArchive(runtime)
    fs.rmSync(pythonTarget, { recursive: true, force: true })
    fs.mkdirSync(pythonTarget, { recursive: true })
    run('tar', ['-xzf', archive, '--strip-components=1', '-C', pythonTarget])
    const python = path.join(pythonTarget, 'bin', 'python3')
    run(python, [
      '-m', 'pip', 'install', '--disable-pip-version-check', '--no-cache-dir',
      '--no-compile', '-r', requirementsPath,
    ])
    run(python, ['-m', 'pip', 'check'])
    fs.writeFileSync(marker, `${JSON.stringify({
      portable: true,
      platform,
      arch: arch === 'x86_64' ? 'x64' : arch,
      pythonVersion: manifest.pythonVersion,
      sourceSha256: runtime.sha256,
      fingerprint,
    }, null, 2)}\n`)
  }

  refreshBackendCode()
  console.log(`[runtime] 已准备 ${platform}-${arch}: ${backendTarget}`)
}


if (require.main === module) {
  try {
    prepareRuntime()
  } catch (error) {
    console.error(`[runtime] ${error.message}`)
    process.exitCode = 1
  }
}

module.exports = {
  computeBuildFingerprint,
  prepareRuntime,
  selectRuntime,
  validateRuntimeManifest,
}
