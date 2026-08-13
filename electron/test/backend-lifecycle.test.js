const assert = require('node:assert/strict')
const { EventEmitter } = require('node:events')
const test = require('node:test')

const {
  buildBackendUrl,
  isBackendAlive,
  waitForBackend,
  shouldRestartBackend,
  isProcessRunning,
  sanitizeBackendEnv,
  createProcessTracker,
} = require('../backend-lifecycle')


function fakeHttpGet({ statusCode, body = '', error, timeout = false, capture }) {
  return (options, onResponse) => {
    if (capture) capture(options)
    const request = new EventEmitter()
    request.abort = () => {}
    request.setTimeout = (_timeoutMs, onTimeout) => {
      if (timeout) queueMicrotask(onTimeout)
    }
    queueMicrotask(() => {
      if (error) request.emit('error', error)
      else if (!timeout) {
        const response = new EventEmitter()
        response.statusCode = statusCode
        onResponse(response)
        if (body) response.emit('data', Buffer.from(body))
        response.emit('end')
      }
    })
    return request
  }
}


test('health 必须同时匹配状态、实例 ID 与能力头', async () => {
  let requestOptions
  const identity = { port: 49152, token: 'token-1', instanceId: 'instance-1' }
  assert.equal(await isBackendAlive({
    ...identity,
    httpGet: fakeHttpGet({
      statusCode: 200,
      body: JSON.stringify({ status: 'ok', instance_id: 'instance-1' }),
      capture: (value) => { requestOptions = value },
    }),
  }), true)
  assert.equal(requestOptions.hostname, '127.0.0.1')
  assert.equal(requestOptions.port, 49152)
  assert.equal(requestOptions.path, '/api/health')
  assert.equal(requestOptions.headers['X-PaperMind-Token'], 'token-1')

  assert.equal(await isBackendAlive({
    ...identity,
    httpGet: fakeHttpGet({ statusCode: 200, body: '{"status":"ok","instance_id":"fake"}' }),
  }), false)
  assert.equal(await isBackendAlive({
    ...identity,
    httpGet: fakeHttpGet({ statusCode: 200, body: 'not-json' }),
  }), false)
  assert.equal(await isBackendAlive({
    ...identity,
    httpGet: fakeHttpGet({ statusCode: 503 }),
  }), false)
})


test('health 网络错误与超时安全返回 false', async () => {
  assert.equal(await isBackendAlive({ httpGet: fakeHttpGet({ error: new Error('ECONNREFUSED') }) }), false)
  assert.equal(await isBackendAlive({ httpGet: fakeHttpGet({ timeout: true }) }), false)
})


test('等待后端支持立即成功与假时钟重试成功', async () => {
  assert.equal(await waitForBackend({ probe: async () => true, timeoutMs: 1000 }), true)

  let now = 0
  let calls = 0
  const result = await waitForBackend({
    probe: async () => ++calls >= 3,
    timeoutMs: 1000,
    intervalMs: 100,
    now: () => now,
    schedule: (fn, delay) => {
      now += delay
      fn()
    },
  })
  assert.equal(result, true)
  assert.equal(calls, 3)
})


test('等待后端在截止时间后返回 false', async () => {
  let now = 0
  const result = await waitForBackend({
    probe: async () => false,
    timeoutMs: 250,
    intervalMs: 100,
    now: () => now,
    schedule: (fn, delay) => {
      now += delay
      fn()
    },
  })
  assert.equal(result, false)
})


test('自动重启决策拒绝主动退出、窗口销毁与 30 秒内重复崩溃', () => {
  const base = { now: 50_000, lastRestartAt: 0 }
  assert.equal(shouldRestartBackend({ ...base, intentionalKill: true, windowAlive: true }), false)
  assert.equal(shouldRestartBackend({ ...base, intentionalKill: false, windowAlive: false }), false)
  assert.equal(shouldRestartBackend({
    intentionalKill: false,
    windowAlive: true,
    now: 50_000,
    lastRestartAt: 30_001,
  }), false)
  assert.equal(shouldRestartBackend({ ...base, intentionalKill: false, windowAlive: true }), true)
})


test('后端 URL、进程存活判断与环境净化', () => {
  assert.equal(buildBackendUrl(49152), 'http://127.0.0.1:49152')
  assert.equal(isProcessRunning(null), false)
  assert.equal(isProcessRunning({ killed: false, exitCode: null, signalCode: null }), true)
  assert.equal(isProcessRunning({ killed: true, exitCode: null, signalCode: null }), false)
  assert.equal(isProcessRunning({ killed: false, exitCode: 0, signalCode: null }), false)

  const env = sanitizeBackendEnv({
    PATH: '/bin',
    PYTHONPATH: '/bad/path',
    PYTHONHOME: '/bad/home',
    PAPERMIND_DATA_DIR: '/old',
  }, '/safe/data', {
    token: 'token-1',
    instanceId: 'instance-1',
  })
  assert.deepEqual(env, {
    PATH: '/bin',
    PAPERMIND_DATA_DIR: '/safe/data',
    PAPERMIND_API_TOKEN: 'token-1',
    PAPERMIND_INSTANCE_ID: 'instance-1',
  })
})


test('旧进程迟到退出不会清除新进程，主动停止按进程隔离', () => {
  const tracker = createProcessTracker()
  const oldProcess = {}
  const newProcess = {}
  tracker.setCurrent(oldProcess)
  tracker.markStopped(oldProcess)
  tracker.setCurrent(newProcess)

  assert.equal(tracker.clearIfCurrent(oldProcess), false)
  assert.equal(tracker.getCurrent(), newProcess)
  assert.equal(tracker.wasStopped(oldProcess), true)
  assert.equal(tracker.wasStopped(newProcess), false)
  assert.equal(tracker.clearIfCurrent(newProcess), true)
  assert.equal(tracker.getCurrent(), null)
})


test('主进程启动接线不引用废弃进程变量且 readiness 失败会停止进程', () => {
  const fs = require('node:fs')
  const path = require('node:path')
  const source = fs.readFileSync(path.join(__dirname, '../main.js'), 'utf8')

  assert.doesNotMatch(source, /backendProcess/)
  assert.doesNotMatch(source, /intentionalKill\s*=/)
  assert.match(source, /if \(!alive\) stopBackendProcess\(proc\)/)
})
