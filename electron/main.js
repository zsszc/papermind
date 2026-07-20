const { app, BrowserWindow } = require('electron')
const path = require('path')
const { spawn } = require('child_process')
const fs = require('fs')

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
  mainWindow = new BrowserWindow({
    width: 1400,
    height: 900,
    minWidth: 1000,
    minHeight: 700,
    title: 'PaperMind',
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
    },
  })

  if (isDev) {
    mainWindow.loadURL('http://localhost:5173')
    mainWindow.webContents.openDevTools()
  } else {
    // 生产模式：先检查后端是否已运行，未运行则自动启动，然后加载 dist
    mainWindow.webContents.openDevTools()
    loadProductionApp()
  }
}

async function loadProductionApp() {
  const { distPath } = getProjectPaths()

  // 先检查是否已有后端在运行，避免端口冲突和重复启动
  const alreadyRunning = await isBackendAlive(2000)
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
  fs.mkdirSync(path.join(dataDir, 'my-thesis'), { recursive: true })
  fs.mkdirSync(path.join(dataDir, 'vector_db'), { recursive: true })
  fs.mkdirSync(path.join(dataDir, 'backups'), { recursive: true })
  fs.mkdirSync(path.join(dataDir, 'logs'), { recursive: true })

  logToFile('[electron] 数据目录检查完成，开始 spawn 后端进程')

  try {
    backendProcess = spawn(
      venvPython,
      ['-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', '8000', '--workers', '1'],
      {
        cwd: backendCwd,
        stdio: 'pipe',
        env: {
          ...process.env,
          PAPERMIND_DATA_DIR: dataDir,
        },
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
  })

  const alive = await waitForBackend(15000)
  logToFile(`[electron] waitForBackend 结果: ${alive}`)
  return alive
}

function isBackendAlive(timeoutMs = 2000) {
  return new Promise((resolve) => {
    const http = require('http')
    const req = http.get('http://127.0.0.1:8000/api/health', (res) => {
      resolve(res.statusCode === 200)
    })
    req.on('error', () => resolve(false))
    req.setTimeout(timeoutMs, () => {
      req.abort()
      resolve(false)
    })
  })
}

function waitForBackend(timeoutMs) {
  const start = Date.now()
  return new Promise((resolve) => {
    const check = () => {
      if (Date.now() - start > timeoutMs) {
        resolve(false)
        return
      }
      isBackendAlive(1000).then((alive) => {
        if (alive) {
          resolve(true)
        } else {
          setTimeout(check, 300)
        }
      })
    }
    check()
  })
}

function killBackend() {
  if (backendProcess && !backendProcess.killed) {
    backendProcess.kill()
    backendProcess = null
  }
}

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
