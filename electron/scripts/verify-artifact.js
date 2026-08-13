#!/usr/bin/env node

const fs = require('node:fs')
const path = require('node:path')

const REQUIRED_PATHS = [
  'app.asar',
  'frontend/dist/index.html',
  'backend/app/main.py',
  'config.yaml.example',
]

const REQUIRED_ASAR_ENTRIES = [
  '/main.js',
  '/preload.js',
  '/backend-lifecycle.js',
  '/security-policy.js',
]

const FORBIDDEN_PATHS = [
  /^config\.yaml$/,
  /^(?:data|papers|notes|summaries|my-thesis|vector_db|logs|backups)(?:\/|$)/,
  /^backend\/(?:tests|eval|\.pytest_cache)(?:\/|$)/,
  /^backend\/(?:data|papers|notes|summaries|my-thesis|vector_db|logs|backups)(?:\/|$)/,
  /(?:^|\/)\.env(?:\.|$)/,
  /\.(?:db|sqlite|sqlite3)$/i,
]

const SECRET_PATTERNS = [
  /\bsk-[A-Za-z0-9_-]{20,}\b/,
  /\bAKIA[0-9A-Z]{16}\b/,
  /-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----/,
]


function walkFiles(root, relative = '') {
  const current = path.join(root, relative)
  if (!fs.existsSync(current)) return []
  return fs.readdirSync(current, { withFileTypes: true }).flatMap((entry) => {
    const child = path.posix.join(relative.replaceAll(path.sep, '/'), entry.name)
    return entry.isDirectory() ? walkFiles(root, child) : [child]
  })
}


function hasPythonRuntime(resourcesRoot) {
  return [
    'backend/venv/bin/python',
    'backend/venv/bin/python3',
    'backend/venv/Scripts/python.exe',
  ].some((relativePath) => fs.existsSync(path.join(resourcesRoot, relativePath)))
}


function readAsarEntries(asarPath) {
  // electron-builder 自带 @electron/asar；只在实际制品扫描时加载，单元测试可直接注入条目。
  return require('@electron/asar').listPackage(asarPath)
}


function scanArtifact(resourcesRoot, { asarEntries } = {}) {
  const errors = []
  const files = walkFiles(resourcesRoot)
  const fileSet = new Set(files)

  for (const requiredPath of REQUIRED_PATHS) {
    if (!fileSet.has(requiredPath)) errors.push(`缺少运行文件: ${requiredPath}`)
  }
  if (!hasPythonRuntime(resourcesRoot)) errors.push('缺少 Python 运行时: backend/venv/{bin,Scripts}/python')

  for (const relativePath of files) {
    if (FORBIDDEN_PATHS.some((pattern) => pattern.test(relativePath))) {
      errors.push(`包含禁止发布的路径: ${relativePath}`)
    }
  }

  const packageEntries = asarEntries || (fileSet.has('app.asar')
    ? readAsarEntries(path.join(resourcesRoot, 'app.asar'))
    : [])
  for (const requiredEntry of REQUIRED_ASAR_ENTRIES) {
    if (!packageEntries.includes(requiredEntry)) errors.push(`app.asar 缺少模块: ${requiredEntry}`)
  }

  for (const relativePath of files.filter((item) => /(?:^|\/)(?:config[^/]*\.(?:ya?ml|json)(?:\.example)?|\.env[^/]*)$/i.test(item))) {
    const absolutePath = path.join(resourcesRoot, relativePath)
    if (fs.statSync(absolutePath).size > 1024 * 1024) continue
    const content = fs.readFileSync(absolutePath, 'utf8')
    if (SECRET_PATTERNS.some((pattern) => pattern.test(content))) {
      errors.push(`配置中包含疑似密钥: ${relativePath}`)
    }
  }
  return errors
}


function findResourcesRoots(outputRoot) {
  if (!fs.existsSync(outputRoot)) return []
  const candidates = []
  for (const entry of fs.readdirSync(outputRoot, { withFileTypes: true })) {
    if (!entry.isDirectory()) continue
    const base = path.join(outputRoot, entry.name)
    const direct = path.join(base, 'resources')
    if (fs.existsSync(direct)) candidates.push(direct)
    for (const child of fs.readdirSync(base, { withFileTypes: true })) {
      if (!child.isDirectory() || !child.name.endsWith('.app')) continue
      const macResources = path.join(base, child.name, 'Contents', 'Resources')
      if (fs.existsSync(macResources)) candidates.push(macResources)
    }
  }
  return candidates
}


function main() {
  const explicitRoot = process.argv[2]
  const roots = explicitRoot
    ? [path.resolve(explicitRoot)]
    : findResourcesRoots(path.resolve(__dirname, '../../frontend/out'))
  if (roots.length === 0) {
    console.error('未找到 unpacked 应用资源目录；请先执行 electron-builder --dir')
    process.exitCode = 1
    return
  }
  let failed = false
  for (const root of roots) {
    const errors = scanArtifact(root)
    if (errors.length > 0) {
      failed = true
      console.error(`[artifact] ${root}`)
      for (const error of errors) console.error(`  - ${error}`)
    } else {
      console.log(`[artifact] 通过: ${root}`)
    }
  }
  if (failed) process.exitCode = 1
}


if (require.main === module) main()

module.exports = { findResourcesRoots, scanArtifact }
