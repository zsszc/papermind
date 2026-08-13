const http = require('http')


function isBackendAlive({
  timeoutMs = 2000,
  url = 'http://127.0.0.1:8000/api/health',
  httpGet = http.get,
} = {}) {
  return new Promise((resolve) => {
    let settled = false
    const finish = (alive) => {
      if (settled) return
      settled = true
      resolve(alive)
    }
    const request = httpGet(url, (response) => finish(response.statusCode === 200))
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


function sanitizeBackendEnv(sourceEnv, dataDir) {
  const env = { ...sourceEnv }
  delete env.PYTHONPATH
  delete env.PYTHONHOME
  env.PAPERMIND_DATA_DIR = dataDir
  return env
}


module.exports = {
  isBackendAlive,
  waitForBackend,
  shouldRestartBackend,
  isProcessRunning,
  sanitizeBackendEnv,
}
