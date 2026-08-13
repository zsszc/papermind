#!/usr/bin/env node

const assert = require('node:assert/strict')
const { spawn } = require('node:child_process')
const fs = require('node:fs')
const http = require('node:http')
const net = require('node:net')
const os = require('node:os')
const path = require('node:path')
const { once } = require('node:events')
const { isBackendAlive, sanitizeBackendEnv, waitForBackend } = require('../backend-lifecycle')
const { createRuntimeIdentity } = require('../runtime-identity')


function request({ port, token = '' }) {
  return new Promise((resolve, reject) => {
    const req = http.get({
      hostname: '127.0.0.1',
      port,
      path: '/api/health',
      headers: token ? { 'X-PaperMind-Token': token } : {},
    }, (response) => {
      let body = ''
      response.setEncoding('utf8')
      response.on('data', (chunk) => { body += chunk })
      response.on('end', () => resolve({ status: response.statusCode, body }))
    })
    req.setTimeout(2000, () => req.destroy(new Error('health 请求超时')))
    req.on('error', reject)
  })
}


function assertPortReleased(port) {
  return new Promise((resolve, reject) => {
    const server = net.createServer()
    server.once('error', reject)
    server.listen({ host: '127.0.0.1', port, exclusive: true }, () => {
      server.close((error) => error ? reject(error) : resolve())
    })
  })
}


async function main() {
  const electronRoot = path.resolve(__dirname, '..')
  const projectRoot = path.resolve(electronRoot, '..')
  const backendRoot = path.join(projectRoot, 'backend')
  const bundledPython = path.join(backendRoot, 'venv', 'bin', 'python')
  const python = process.env.PAPERMIND_PYTHON
    || (fs.existsSync(bundledPython) ? bundledPython : 'python3')
  const dataDir = fs.mkdtempSync(path.join(os.tmpdir(), 'papermind-identity-smoke-'))
  const identity = await createRuntimeIdentity()
  let child
  let stderr = ''

  try {
    child = spawn(python, [
      '-m', 'uvicorn', 'app.main:app', '--lifespan', 'off', '--host', '127.0.0.1',
      '--port', String(identity.port), '--workers', '1',
    ], {
      cwd: backendRoot,
      env: sanitizeBackendEnv(process.env, dataDir, identity),
      stdio: ['ignore', 'ignore', 'pipe'],
    })
    child.stderr.on('data', (chunk) => { stderr = `${stderr}${chunk}`.slice(-8192) })

    const ready = await waitForBackend({
      timeoutMs: 15000,
      probe: () => isBackendAlive({ ...identity, timeoutMs: 1000 }),
    })
    assert.equal(ready, true, `真实后端未就绪：${stderr}`)

    assert.equal((await request({ port: identity.port })).status, 401)
    assert.equal((await request({ port: identity.port, token: 'wrong-token' })).status, 401)
    const accepted = await request({ port: identity.port, token: identity.token })
    assert.equal(accepted.status, 200)
    assert.equal(JSON.parse(accepted.body).instance_id, identity.instanceId)
    assert.equal(await isBackendAlive({
      ...identity,
      instanceId: '00000000-0000-4000-8000-000000000000',
      timeoutMs: 1000,
    }), false)
  } finally {
    if (child && child.exitCode === null && child.signalCode === null) {
      child.kill('SIGTERM')
      await Promise.race([
        once(child, 'exit'),
        new Promise((resolve) => setTimeout(() => {
          if (child.exitCode === null && child.signalCode === null) child.kill('SIGKILL')
          resolve()
        }, 3000)),
      ])
    }
    await assertPortReleased(identity.port)
    fs.rmSync(dataDir, { recursive: true, force: true })
  }

  console.log('PASS: 随机回环端口、能力令牌、实例拒绝与端口释放 smoke')
}


main().catch((error) => {
  console.error(`FAIL: ${error.stack || error}`)
  process.exitCode = 1
})
