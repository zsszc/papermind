const assert = require('node:assert/strict')
const path = require('node:path')
const test = require('node:test')

const manifest = require('../runtime-manifest.json')
const {
  computeBuildFingerprint,
  selectRuntime,
  validateRuntimeManifest,
} = require('../scripts/prepare-runtime')


test('运行时清单固定 macOS arm64/x64 的版本、URL 与 SHA-256', () => {
  assert.deepEqual(validateRuntimeManifest(manifest), [])
  for (const arch of ['arm64', 'x64']) {
    const runtime = selectRuntime(manifest, 'darwin', arch)
    assert.match(runtime.url, /^https:\/\/github\.com\/astral-sh\/python-build-standalone\//)
    assert.match(runtime.sha256, /^[a-f0-9]{64}$/)
    assert.match(runtime.filename, new RegExp(`${arch === 'x64' ? 'x86_64' : 'aarch64'}-apple-darwin`))
  }
})


test('不支持的平台或架构明确失败', () => {
  assert.throws(() => selectRuntime(manifest, 'win32', 'arm64'), /不支持的桌面运行时/)
})


test('构建指纹同时绑定运行时摘要与 requirements 内容', () => {
  const runtime = selectRuntime(manifest, 'darwin', 'arm64')
  const first = computeBuildFingerprint(runtime, Buffer.from('fastapi==1'))
  const second = computeBuildFingerprint(runtime, Buffer.from('fastapi==2'))
  assert.match(first, /^[a-f0-9]{64}$/)
  assert.notEqual(first, second)
})
