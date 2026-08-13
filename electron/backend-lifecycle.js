const http = require('http')


function buildBackendUrl(port) {
  if (!Number.isInteger(port) || port < 1 || port > 65535) throw new Error('后端端口非法')
  return `http://127.0.0.1:${port}`
}


function isBackendAlive({
  timeoutMs = 2000,
  port = 8000,
  token = '',
  instanceId,
  httpGet = http.get,
} = {}) {
  return new Promise((resolve) => {
    let settled = false
    const deadline = setTimeout(() => {
      request?.abort?.()
      finish(false)
    }, timeoutMs)
    const finish = (alive) => {
      if (settled) return
      settled = true
      clearTimeout(deadline)
      resolve(alive)
    }
    let request
    try {
      request = httpGet({
        hostname: '127.0.0.1',
        port,
        path: '/api/health',
        headers: token ? { 'X-PaperMind-Token': token } : {},
      }, (response) => {
        let body = ''
        response.on('error', () => finish(false))
        response.on('data', (chunk) => {
          if (Buffer.byteLength(body) + chunk.length > 16 * 1024) {
            response.destroy?.()
            finish(false)
            return
          }
          body += chunk.toString()
        })
        response.on('end', () => {
          if (response.statusCode !== 200 || !instanceId) return finish(false)
          try {
            const data = JSON.parse(body)
            finish(data.status === 'ok' && data.instance_id === instanceId)
          } catch {
            finish(false)
          }
        })
      })
    } catch {
      finish(false)
      return
    }
    request.on('error', () => finish(false))
    request.setTimeout(timeoutMs, () => {
      request.abort()
      finish(false)
    })
  })
}


function waitForBackend({
  timeoutMs,
  intervalMs = 300,
  probe = () => isBackendAlive({ timeoutMs: 1000 }),
  now = Date.now,
  schedule = setTimeout,
}) {
  const startedAt = now()
  return new Promise((resolve) => {
    const check = async () => {
      if (now() - startedAt > timeoutMs) {
        resolve(false)
        return
      }
      if (await probe()) {
        resolve(true)
        return
      }
      schedule(check, intervalMs)
    }
    check()
  })
}


function shouldRestartBackend({
  intentionalKill,
  windowAlive,
  now,
  lastRestartAt,
  restartWindowMs = 30000,
}) {
  return !intentionalKill && windowAlive && now - lastRestartAt >= restartWindowMs
}


function isProcessRunning(processHandle) {
  return Boolean(
    processHandle
    && !processHandle.killed
    && processHandle.exitCode === null
    && processHandle.signalCode === null
  )
}


function createProcessTracker() {
  let current = null
  const intentionallyStopped = new WeakSet()
  return {
    getCurrent: () => current,
    setCurrent: (processHandle) => { current = processHandle },
    clearIfCurrent: (processHandle) => {
      if (current !== processHandle) return false
      current = null
      return true
    },
    markStopped: (processHandle) => {
      if (processHandle && typeof processHandle === 'object') intentionallyStopped.add(processHandle)
    },
    wasStopped: (processHandle) => intentionallyStopped.has(processHandle),
  }
}


function sanitizeBackendEnv(sourceEnv, dataDir, { token = '', instanceId = '' } = {}) {
  const env = { ...sourceEnv }
  delete env.PYTHONPATH
  delete env.PYTHONHOME
  delete env.PAPERMIND_API_TOKEN
  delete env.PAPERMIND_INSTANCE_ID
  env.PAPERMIND_DATA_DIR = dataDir
  if (token) env.PAPERMIND_API_TOKEN = token
  else delete env.PAPERMIND_API_TOKEN
  if (instanceId) env.PAPERMIND_INSTANCE_ID = instanceId
  else delete env.PAPERMIND_INSTANCE_ID
  return env
}


module.exports = {
  buildBackendUrl,
  createProcessTracker,
  isBackendAlive,
  waitForBackend,
  shouldRestartBackend,
  isProcessRunning,
  sanitizeBackendEnv,
}
