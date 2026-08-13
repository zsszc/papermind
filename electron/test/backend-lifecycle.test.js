const assert = require('node:assert/strict')
const { EventEmitter } = require('node:events')
const test = require('node:test')

const {
  isBackendAlive,
  waitForBackend,
  shouldRestartBackend,
  isProcessRunning,
  sanitizeBackendEnv,
} = require('../backend-lifecycle')


function fakeHttpGet({ statusCode, error, timeout = false }) {
  return (_url, onResponse) => {
    const request = new EventEmitter()
    request.abort = () => {}
    request.setTimeout = (_timeoutMs, onTimeout) => {
      if (timeout) queueMicrotask(onTimeout)
    }
    queueMicrotask(() => {
      if (error) request.emit('error', error)
      else if (!timeout) onResponse({ statusCode })
    })
    return request
  }
}


test('health 只有 HTTP 200 视为存活', async () => {
  assert.equal(await isBackendAlive({ httpGet: fakeHttpGet({ statusCode: 200 }) }), true)
  assert.equal(await isBackendAlive({ httpGet: fakeHttpGet({ statusCode: 503 }) }), false)
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


test('进程存活判断与环境净化', () => {
  assert.equal(isProcessRunning(null), false)
  assert.equal(isProcessRunning({ killed: false, exitCode: null, signalCode: null }), true)
  assert.equal(isProcessRunning({ killed: true, exitCode: null, signalCode: null }), false)
  assert.equal(isProcessRunning({ killed: false, exitCode: 0, signalCode: null }), false)

  const env = sanitizeBackendEnv({
    PATH: '/bin',
    PYTHONPATH: '/bad/path',
    PYTHONHOME: '/bad/home',
    PAPERMIND_DATA_DIR: '/old',
  }, '/safe/data')
  assert.deepEqual(env, { PATH: '/bin', PAPERMIND_DATA_DIR: '/safe/data' })
})
