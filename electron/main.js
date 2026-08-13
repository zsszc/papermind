const { app, BrowserWindow } = require('electron')
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
} = require('./backend-lifecycle')
const {
  createSecureWebPreferences,
  focusExistingWindow,
  installPermissionGuards,
  installWindowGuards,
} = require('./security-policy')

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
let backendProcess = null
// 是否为主动 kill（退出应用时置 true，避免触发自动重启）
let intentionalKill = false
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
    loadProductionApp()
  }
}

async function loadProductionApp() {
  const { distPath } = getProjectPaths()

  // 先检查是否已有后端在运行，避免端口冲突和重复启动
  const alreadyRunning = await isBackendAlive({ timeoutMs: 2000 })
  if (alreadyRunning) {
    console.log('[electron] 检测到后端已运行，直接加载前端')
    mainWindow.loadFile(distPath)
    return
  }

  console.log('[electron] 未检测到后端，尝试自动启动...')
  const started = await startBackend()
  if (!started) {
    console.error('[electron] 未能自动启动后端，将加载前端并等待用户手动启动后端')
  }
  mainWindow.loadFile(distPath)
}

async function startBackend() {
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

  try {
    // 净化环境：PYTHONPATH/PYTHONHOME 会污染 backend/venv 的解释器，
    // 曾导致加载到其他 venv 的 fastapi/pydantic_core 使后端无法启动
    const env = sanitizeBackendEnv(process.env, dataDir)

    backendProcess = spawn(
      venvPython,
      ['-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', '8000', '--workers', '1'],
      {
        cwd: backendCwd,
        stdio: 'pipe',
        env,
      }
    )
  } catch (spawnErr) {
    errToFile('[electron] spawn 后端进程抛出异常:', spawnErr)
    return false
  }

  logToFile(`[electron] 后端进程已 spawn，pid=${backendProcess.pid}`)

  backendProcess.stdout.on('data', (data) => {
    logToFile(`[backend] ${data.toString().trim()}`)
  })
  backendProcess.stderr.on('data', (data) => {
    errToFile(`[backend] ${data.toString().trim()}`)
  })

  backendProcess.on('error', (err) => {
    errToFile('[electron] 后端进程错误:', err)
  })

  backendProcess.on('exit', (code, signal) => {
    errToFile(`[electron] 后端进程退出，code=${code}, signal=${signal}`)
    // 进程已退出，释放引用，避免后续 kill 操作打到已退出的对象上
    backendProcess = null

    // 主动 kill（退出应用）时不重启；窗口已销毁时也不重启
    const now = Date.now()
    const windowAlive = Boolean(mainWindow && !mainWindow.isDestroyed())
    if (!shouldRestartBackend({
      intentionalKill,
      windowAlive,
      now,
      lastRestartAt,
    })) {
      if (intentionalKill || !windowAlive) return
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

  const alive = await waitForBackend({ timeoutMs: 15000 })
  logToFile(`[electron] waitForBackend 结果: ${alive}`)
  return alive
}

function killBackend() {
  const proc = backendProcess
  if (!isProcessRunning(proc)) return

  // 标记为主动 kill，exit 回调中不会触发自动重启
  intentionalKill = true

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

const hasSingleInstanceLock = app.requestSingleInstanceLock()

if (!hasSingleInstanceLock) {
  app.quit()
} else {
  app.on('second-instance', () => focusExistingWindow(mainWindow))
  app.whenReady().then(createWindow)

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
