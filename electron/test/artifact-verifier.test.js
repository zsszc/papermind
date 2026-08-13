const assert = require('node:assert/strict')
const fs = require('node:fs')
const os = require('node:os')
const path = require('node:path')
const test = require('node:test')

const { scanArtifact } = require('../scripts/verify-artifact')


function createFixture() {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'papermind-artifact-'))
  for (const relativePath of [
    'frontend/dist/index.html',
    'backend/app/main.py',
  ]) {
    const target = path.join(root, relativePath)
    fs.mkdirSync(path.dirname(target), { recursive: true })
    fs.writeFileSync(target, '')
  }
  const python = path.join(root, 'backend/venv/bin/python')
  fs.mkdirSync(path.dirname(python), { recursive: true })
  fs.writeFileSync(python, '#!/bin/sh\n')
  fs.writeFileSync(path.join(root, 'backend/venv/.papermind-runtime.json'), JSON.stringify({
    portable: true,
    platform: 'darwin',
    arch: 'arm64',
    pythonVersion: '3.12.13',
    sourceSha256: 'a'.repeat(64),
    fingerprint: 'b'.repeat(64),
  }))
  fs.writeFileSync(path.join(root, 'app.asar'), '')
  fs.writeFileSync(path.join(root, 'config.yaml.example'), 'llm:\n  api_key: ""\n')
  return root
}


const validAsarEntries = [
  '/main.js',
  '/preload.js',
  '/backend-lifecycle.js',
  '/security-policy.js',
]


test('合法制品包含前后端入口、Python 与四个桌面模块', () => {
  const root = createFixture()
  try {
    assert.deepEqual(scanArtifact(root, { asarEntries: validAsarEntries }), [])
  } finally {
    fs.rmSync(root, { recursive: true, force: true })
  }
})


test('制品缺少运行入口时给出全部错误', () => {
  const root = createFixture()
  try {
    fs.rmSync(path.join(root, 'frontend/dist/index.html'))
    const errors = scanArtifact(root, { asarEntries: ['/main.js'] })
    assert.ok(errors.some((error) => error.includes('frontend/dist/index.html')))
    assert.ok(errors.some((error) => error.includes('backend-lifecycle.js')))
    assert.ok(errors.some((error) => error.includes('security-policy.js')))
  } finally {
    fs.rmSync(root, { recursive: true, force: true })
  }
})


test('制品拒绝真实配置、用户数据、测试与评测目录', () => {
  const root = createFixture()
  try {
    for (const relativePath of [
      'config.yaml',
      'data/papers.db',
      'papers/private.pdf',
      'backend/tests/test_security.py',
      'backend/eval/reports/latest.json',
      'backend/.pytest_cache/CACHEDIR.TAG',
    ]) {
      const target = path.join(root, relativePath)
      fs.mkdirSync(path.dirname(target), { recursive: true })
      fs.writeFileSync(target, '')
    }
    const errors = scanArtifact(root, { asarEntries: validAsarEntries })
    for (const forbidden of ['config.yaml', 'data/papers.db', 'papers/private.pdf', 'backend/tests']) {
      assert.ok(errors.some((error) => error.includes(forbidden)), `应拒绝 ${forbidden}`)
    }
  } finally {
    fs.rmSync(root, { recursive: true, force: true })
  }
})


test('公开配置模板中的疑似真实 API Key 会阻断发布', () => {
  const root = createFixture()
  try {
    fs.writeFileSync(
      path.join(root, 'config.yaml.example'),
      `llm:\n  api_key: "sk-${'A'.repeat(32)}"\n`,
    )
    const errors = scanArtifact(root, { asarEntries: validAsarEntries })
    assert.ok(errors.some((error) => error.includes('疑似密钥')))
  } finally {
    fs.rmSync(root, { recursive: true, force: true })
  }
})


test('Python 解释器软链不得逃出发布资源目录', () => {
  const root = createFixture()
  try {
    const python = path.join(root, 'backend/venv/bin/python')
    fs.rmSync(python)
    const python3 = path.join(root, 'backend/venv/bin/python3')
    fs.symlinkSync('/opt/miniconda3/bin/python3', python3)
    fs.symlinkSync('python3', python)
    const errors = scanArtifact(root, { asarEntries: validAsarEntries })
    assert.ok(errors.some((error) => error.includes('Python 软链逃出制品')))
  } finally {
    fs.rmSync(root, { recursive: true, force: true })
  }
})


test('Python 运行时必须携带可移植架构清单', () => {
  const root = createFixture()
  try {
    fs.writeFileSync(
      path.join(root, 'backend/venv/.papermind-runtime.json'),
      JSON.stringify({ portable: false, arch: 'x64' }),
    )
    const errors = scanArtifact(root, { asarEntries: validAsarEntries })
    assert.ok(errors.some((error) => error.includes('运行时清单非法')))
  } finally {
    fs.rmSync(root, { recursive: true, force: true })
  }
})
