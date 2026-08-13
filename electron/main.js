const { app, BrowserWindow, ipcMain } = require('electron')
const path = require('path')
const { pathToFileURL } = require('url')
const { spawn } = require('child_process')
const fs = require('fs')
const {
  isBackendAlive,
  waitForBackend,
  shouldRestartBackend,
  isProcessRunning,
  sanitizeBackendEnv,
  buildBackendUrl,
  createProcessTracker,
} = require('./backend-lifecycle')
const {
  createSecureWebPreferences,
  focusExistingWindow,
  installPermissionGuards,
  installWindowGuards,
} = require('./security-policy')
const {
  createRuntimeIdentity,
  isAllowedRuntimeConfigRequest,
} = require('./runtime-identity')

// 将主进程日志写入应用数据目录，便于排查后端启动问题
function getLogPath() {
  const userData = app?.getPath ? app.getPath('userData') : require('os').homedir()
  const logDir = path.join(userData, 'PaperMindData', 'logs')
  fs.mkdirSync(logDir, { recursive: true })
  return path.join(logDir, 'electron-main.log')
}
const logStream = fs.createWriteStream(getLogPath(), { flags: 'a' })
function logToFile(...args) {
  const line = `[${new Date().toISOString()}] ${args.map(a => typeof a === 'object' ? JSON.stringify(a) : String(a)).join(' ')}\n`
  logStream.write(line)
  console.log(...args)
}
function errToFile(...args) {
  const line = `[${new Date().toISOString()}] [ERROR] ${args.map(a => typeof a === 'object' ? JSON.stringify(a) : String(a)).join(' ')}\n`
  logStream.write(line)
  console.error(...args)
}

const isDev = process.env.NODE_ENV === 'development'

let mainWindow
let backendStartPromise = null
let runtimeIdentity = null
const processTracker = createProcessTracker()
// 上一次自动重启的时间戳，用于限制 30 秒内最多重启 1 次
let lastRestartAt = 0

function getProjectPaths() {
  if (app.isPackaged) {
    // 生产包：backend 和 frontend/dist 都在 extraResources 中
    return {
      projectRoot: process.resourcesPath,
      distPath: path.join(process.resourcesPath, 'frontend/dist/index.html'),
      dataDir: path.join(app.getPath('userData'), 'PaperMindData'),
    }
  }
  // 开发/未打包：使用项目根目录的相对路径
  return {
    projectRoot: path.join(__dirname, '..'),
    distPath: path.join(__dirname, '../frontend/dist/index.html'),
    dataDir: path.join(__dirname, '..'),
  }
}

function createWindow() {
  const { distPath } = getProjectPaths()
  mainWindow = new BrowserWindow({
    width: 1400,
    height: 900,
    minWidth: 1000,
    minHeight: 700,
    title: 'PaperMind',
    webPreferences: createSecureWebPreferences(path.join(__dirname, 'preload.js')),
  })
  installPermissionGuards(mainWindow.webContents.session)
  installWindowGuards(mainWindow.webContents, {
    isDev,
    productionEntryUrl: pathToFileURL(distPath).href,
  })

  if (isDev) {
    mainWindow.loadURL('http://localhost:5173')
    mainWindow.webContents.openDevTools()
  } else {
    // 生产模式：先检查后端是否已运行，未运行则自动启动，然后加载 dist
    // 注意：生产模式不打开 DevTools，避免打包版自带开发者工具
    void loadProductionApp().catch((error) => {
      errToFile('[electron] 加载生产应用失败:', error)
    })
  }
}

async function loadProductionApp() {
  const { distPath } = getProjectPaths()
  const started = await startBackend()
  if (!started) {
    errToFile('[electron] 未能启动并验证本应用后端，拒绝加载生产前端')
    return
  }
  mainWindow.loadFile(distPath)
}

async function startBackend() {
  if (backendStartPromise) return backendStartPromise
  backendStartPromise = startBackendOnce()
  try {
    return await backendStartPromise
  } finally {
    backendStartPromise = null
  }
}

async function startBackendOnce() {
  const { projectRoot, dataDir } = getProjectPaths()
  const venvPython = path.join(projectRoot, 'backend/venv/bin/python')
  const backendCwd = path.join(projectRoot, 'backend')

  logToFile(`[electron] 准备启动后端，projectRoot=${projectRoot}`)
  logToFile(`[electron] venvPython=${venvPython}, exists=${fs.existsSync(venvPython)}`)
  logToFile(`[electron] backendCwd=${backendCwd}, exists=${fs.existsSync(backendCwd)}`)
  logToFile(`[electron] dataDir=${dataDir}`)

  if (!fs.existsSync(venvPython)) {
    errToFile(`[electron] Python 解释器不存在: ${venvPython}`)
    return false
  }

  const existing = processTracker.getCurrent()
  if (isProcessRunning(existing)) {
    if (processTracker.wasStopped(existing)) return false
    return isBackendAlive({ ...runtimeIdentity, timeoutMs: 1000 })
  }

  // 确保数据目录存在，生产环境下数据写在应用数据目录而非 resources（macOS 只读）
  fs.mkdirSync(path.join(dataDir, 'data'), { recursive: true })
  fs.mkdirSync(path.join(dataDir, 'papers'), { recursive: true })
  fs.mkdirSync(path.join(dataDir, 'notes'), { recursive: true })
  fs.mkdirSync(path.join(dataDir, 'summaries'), { recursive: true })
  fs.mkdirSync(path.join(dataDir, 'my-thesis'), { recursive: true })
  fs.mkdirSync(path.join(dataDir, 'vector_db'), { recursive: true })
  fs.mkdirSync(path.join(dataDir, 'backups'), { recursive: true })
  fs.mkdirSync(path.join(dataDir, 'logs'), { recursive: true })

  logToFile('[electron] 数据目录检查完成，开始 spawn 后端进程')

  let proc
  try {
    // 净化环境：PYTHONPATH/PYTHONHOME 会污染 backend/venv 的解释器，
    // 曾导致加载到其他 venv 的 fastapi/pydantic_core 使后端无法启动
    const env = sanitizeBackendEnv(process.env, dataDir, runtimeIdentity)

    proc = spawn(
      venvPython,
      [
        '-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1',
        '--port', String(runtimeIdentity.port), '--workers', '1',
      ],
      {
        cwd: backendCwd,
        stdio: 'pipe',
        env,
      }
    )
    processTracker.setCurrent(proc)
  } catch (spawnErr) {
    errToFile('[electron] spawn 后端进程抛出异常:', spawnErr)
    return false
  }

  logToFile(`[electron] 后端进程已 spawn，pid=${proc.pid}`)

  proc.stdout.on('data', (data) => {
    logToFile(`[backend] ${data.toString().trim()}`)
  })
  proc.stderr.on('data', (data) => {
    errToFile(`[backend] ${data.toString().trim()}`)
  })

  proc.on('error', (err) => {
    errToFile('[electron] 后端进程错误:', err)
  })

  proc.on('exit', (code, signal) => {
    errToFile(`[electron] 后端进程退出，code=${code}, signal=${signal}`)
    // 进程已退出，释放引用，避免后续 kill 操作打到已退出的对象上
    const wasCurrent = processTracker.clearIfCurrent(proc)
    if (!wasCurrent || processTracker.wasStopped(proc)) return

    // 主动 kill（退出应用）时不重启；窗口已销毁时也不重启
    const now = Date.now()
    const windowAlive = Boolean(mainWindow && !mainWindow.isDestroyed())
    if (!shouldRestartBackend({
      intentionalKill: false,
      windowAlive,
      now,
      lastRestartAt,
    })) {
      if (!windowAlive) return
      errToFile('[electron] 后端 30 秒内再次退出，放弃自动重启，请查看日志排查')
      return
    }
    lastRestartAt = now
    logToFile('[electron] 后端意外退出且窗口仍存活，尝试一次自动重启...')
    startBackend().then((ok) => {
      if (!ok) {
        errToFile('[electron] 后端自动重启失败，不再重试')
      } else {
        logToFile('[electron] 后端自动重启成功')
      }
    }).catch((err) => {
      errToFile('[electron] 后端自动重启抛出异常，不再重试:', err)
    })
  })

  const alive = await waitForBackend({
    timeoutMs: 15000,
    probe: () => isBackendAlive({ ...runtimeIdentity, timeoutMs: 1000 }),
  })
  logToFile(`[electron] waitForBackend 结果: ${alive}`)
  if (!alive) stopBackendProcess(proc)
  return alive
}

function stopBackendProcess(proc) {
  if (!isProcessRunning(proc)) return

  // 主动终止状态按具体进程记录，旧进程迟到退出不得影响新进程。
  processTracker.markStopped(proc)

  // 先 SIGTERM 优雅退出；3 秒后仍未退出则 SIGKILL 兜底（uvicorn 偶发卡死）
  try {
    proc.kill('SIGTERM')
  } catch (err) {
    errToFile('[electron] SIGTERM 后端进程失败:', err)
  }
  setTimeout(() => {
    // exitCode/signalCode 均为 null 表示进程仍未退出（此时引用可能已被 exit 回调置 null，故用局部变量 proc）
    if (isProcessRunning(proc)) {
      errToFile('[electron] 后端进程 3 秒内未退出，发送 SIGKILL')
      try {
        proc.kill('SIGKILL')
      } catch (err) {
        errToFile('[electron] SIGKILL 后端进程失败:', err)
      }
    }
  }, 3000)
}

function killBackend() {
  stopBackendProcess(processTracker.getCurrent())
}

const hasSingleInstanceLock = app.requestSingleInstanceLock()

if (!hasSingleInstanceLock) {
  app.quit()
} else {
  ipcMain.handle('papermind:get-runtime-config', (event) => {
    const { distPath } = getProjectPaths()
    if (!runtimeIdentity || !isAllowedRuntimeConfigRequest(
      event,
      mainWindow?.webContents,
      pathToFileURL(distPath).href,
    )) {
      throw new Error('拒绝不受信的运行配置请求')
    }
    return {
      apiBaseUrl: buildBackendUrl(runtimeIdentity.port),
      apiToken: runtimeIdentity.token,
    }
  })
  app.on('second-instance', () => focusExistingWindow(mainWindow))
  app.whenReady().then(async () => {
    runtimeIdentity = isDev
      ? { port: 8000, token: '', instanceId: '' }
      : await createRuntimeIdentity()
    createWindow()
  }).catch((error) => {
    errToFile('[electron] 初始化运行身份失败:', error)
    app.quit()
  })

  app.on('window-all-closed', () => {
    killBackend()
    if (process.platform !== 'darwin') app.quit()
  })

  app.on('activate', () => {
    if (BrowserWindow.getAllWindows().length === 0) createWindow()
  })

  app.on('before-quit', killBackend)
  app.on('will-quit', killBackend)
}
