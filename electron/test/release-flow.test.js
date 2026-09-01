// Batch 24 T1（24A）：关键流程 E2E 基线。
//
// 真实拉起后端子进程（随机回环端口 + 能力令牌 + 临时数据目录），经 HTTP 断言
// 发布候选关键流程闭环：
//   GET /api/health → 文献列表 → 检索（语义不可用时走降级路径）→ 对话 SSE 帧契约 → 统计页数据
//
// 纪律：
// - LLM 指向本机回环死端口（127.0.0.1:9），除本机回环零网络；
//   断言只到「SSE 帧格式与错误帧契约」，不断言生成内容。
// - Embedding 指向不存在的模型 + HF 离线模式，锁定语义检索降级路径。
// - 子进程必清理（泄漏即失败）；单文件总耗时硬上限 120s。
// - 调试：RELEASE_FLOW_BACKEND_URL=http://127.0.0.1:<port> 可外挂已运行后端
//   （此时必须提供 RELEASE_FLOW_BACKEND_TOKEN），不 spawn 也不清理子进程。

const assert = require('node:assert/strict')
const { spawn } = require('node:child_process')
const crypto = require('node:crypto')
const fs = require('node:fs')
const http = require('node:http')
const os = require('node:os')
const path = require('node:path')
const test = require('node:test')

const {
  isBackendAlive,
  waitForBackend,
  isProcessRunning,
} = require('../backend-lifecycle')
const { getAvailablePort } = require('../runtime-identity')

const PROJECT_ROOT = path.join(__dirname, '..', '..')
const REPO_VENV_PYTHON = path.join(PROJECT_ROOT, 'backend', 'venv', 'bin', 'python')
const VENV_PYTHON = process.env.PAPERMIND_PYTHON || REPO_VENV_PYTHON
const BACKEND_CWD = path.join(PROJECT_ROOT, 'backend')
const RUN_RELEASE_E2E = process.env.PAPERMIND_RELEASE_E2E === '1'

// 硬上限：后端启动 60s（冷启动含 torch 导入失败路径）、单请求 30s、全程 120s
const BOOT_TIMEOUT_MS = 60_000
const REQUEST_TIMEOUT_MS = 30_000
const TOTAL_BUDGET_MS = 120_000
const SUITE_STARTED_AT = Date.now()

// 测试专用配置：LLM 指向本机回环死端口（连接即失败，绝不触达外网），
// Embedding 模型名刻意不存在并配合 HF_HUB_OFFLINE 使语义检索确定性地走降级路径。
function buildTestConfigYaml() {
  return `app:
  name: "PaperMind"
  version: "1.0.0"
  data_dir: "./data"
llm:
  provider: "moonshot"
  api_key: "sk-e2e-dead-loopback-key-000000"
  base_url: "http://127.0.0.1:9/v1"
  model: "kimi-k2.6"
  max_tokens: 4096
  temperature: 1.0
embedding:
  provider: "local"
  local_model: "papermind-e2e/nonexistent-model"
  device: "cpu"
  chunk_size: 512
  chunk_overlap: 50
retrieval:
  top_k: 10
  chat_profile: "hybrid"
  lexical_profile: "bm25-bilingual"
  rerank: false
memory:
  enabled: true
  session_limit: 50
  summary_interval: 5
  max_short_term: 10
skills:
  auto_load: true
  skills_dir: "./skills"
ui:
  theme: "light"
  language: "zh-CN"
  page_size: 20
  default_view: "list"
export:
  citation_format: "GB/T 7714"
`
}

// 子进程环境白名单：剥掉 PYTHONPATH/PYTHONHOME 与一切代理变量，
// 保证「除本机回环零网络」不依赖宿主环境的 NO_PROXY 配置。
function buildChildEnv(dataDir, identity) {
  const env = {
    PATH: process.env.PATH || '/usr/bin:/bin',
    HOME: process.env.HOME || os.homedir(),
    TMPDIR: process.env.TMPDIR || os.tmpdir(),
    PAPERMIND_DATA_DIR: dataDir,
    PAPERMIND_API_TOKEN: identity.token,
    PAPERMIND_INSTANCE_ID: identity.instanceId,
    HF_HUB_OFFLINE: '1',
    TRANSFORMERS_OFFLINE: '1',
    NO_PROXY: '127.0.0.1,localhost',
  }
  return env
}

// 带能力令牌与超时上限的 JSON 请求；非 2xx 也正常返回（由断言层判定）。
function httpJson({ port, token, method = 'GET', path: reqPath, body }) {
  return new Promise((resolve, reject) => {
    const payload = body === undefined ? null : Buffer.from(JSON.stringify(body))
    const request = http.request(
      {
        hostname: '127.0.0.1',
        port,
        path: reqPath,
        method,
        headers: {
          ...(token ? { 'X-PaperMind-Token': token } : {}),
          ...(payload
            ? { 'Content-Type': 'application/json', 'Content-Length': payload.length }
            : {}),
        },
      },
      (response) => {
        let raw = ''
        response.on('data', (chunk) => {
          raw += chunk.toString()
        })
        response.on('end', () => {
          let json = null
          try {
            json = JSON.parse(raw)
          } catch {
            // 非 JSON 响应保留原文，交由断言层判定
          }
          resolve({ status: response.statusCode, headers: response.headers, json, raw })
        })
        response.on('error', reject)
      },
    )
    request.on('error', reject)
    request.setTimeout(REQUEST_TIMEOUT_MS, () => {
      request.destroy(new Error(`请求超时（${REQUEST_TIMEOUT_MS}ms）: ${method} ${reqPath}`))
    })
    if (payload) request.write(payload)
    request.end()
  })
}

// 读取 SSE 流直到服务端结束，返回原始文本；帧解析由断言层负责。
function readSseStream({ port, token, path: reqPath, body }) {
  return new Promise((resolve, reject) => {
    const payload = Buffer.from(JSON.stringify(body))
    const request = http.request(
      {
        hostname: '127.0.0.1',
        port,
        path: reqPath,
        method: 'POST',
        headers: {
          'X-PaperMind-Token': token,
          'Content-Type': 'application/json',
          'Content-Length': payload.length,
        },
      },
      (response) => {
        let raw = ''
        response.on('data', (chunk) => {
          raw += chunk.toString()
        })
        response.on('end', () =>
          resolve({ status: response.statusCode, headers: response.headers, raw }),
        )
        response.on('error', reject)
      },
    )
    request.on('error', reject)
    request.setTimeout(REQUEST_TIMEOUT_MS, () => {
      request.destroy(new Error(`SSE 流超时（${REQUEST_TIMEOUT_MS}ms）: ${reqPath}`))
    })
    request.write(payload)
    request.end()
  })
}

// 解析 `data: <json>\n\n` 帧序列；帧格式破损直接抛错（契约失败）。
function parseSseFrames(raw) {
  return raw
    .split('\n\n')
    .map((block) => block.trim())
    .filter(Boolean)
    .map((block) => {
      assert.match(block, /^data: /, 'SSE 帧必须以 data: 开头')
      return JSON.parse(block.slice('data: '.length))
    })
}

// 干净停止子进程：先 SIGTERM，3s 未退出则 SIGKILL 兜底；返回是否正常退出。
async function stopChild(proc) {
  if (!isProcessRunning(proc)) return true
  const exited = new Promise((resolve) => proc.once('exit', () => resolve(true)))
  proc.kill('SIGTERM')
  const grace = await Promise.race([
    exited,
    new Promise((resolve) => setTimeout(() => resolve(false), 3_000)),
  ])
  if (!grace && isProcessRunning(proc)) {
    proc.kill('SIGKILL')
    await Promise.race([exited, new Promise((resolve) => setTimeout(resolve, 2_000))])
  }
  return !isProcessRunning(proc)
}

// --- 套件共享状态：一个后端实例贯穿全流程，最后一个用例负责关闭并断言零泄漏 ---
const backend = {
  proc: null,
  port: 0,
  token: '',
  instanceId: '',
  dataDir: '',
  // 子进程输出环形缓冲：启动失败时带进断言信息，便于定位
  logTail: [],
}

function pushLog(line) {
  backend.logTail.push(line)
  if (backend.logTail.length > 80) backend.logTail.shift()
}

function attachTarget() {
  // 调试入口：外挂已运行后端（例如手工 uvicorn），不 spawn 子进程
  const url = new URL(process.env.RELEASE_FLOW_BACKEND_URL)
  backend.port = Number(url.port)
  backend.token = process.env.RELEASE_FLOW_BACKEND_TOKEN || ''
  backend.instanceId = '' // 外挂模式无法预知实例 ID，相关断言降级为存在性
}

async function spawnBackend(t) {
  if (path.isAbsolute(VENV_PYTHON)) {
    assert.ok(fs.existsSync(VENV_PYTHON), `Python 解释器不存在: ${VENV_PYTHON}`)
  }
  backend.port = await getAvailablePort()
  backend.token = crypto.randomBytes(32).toString('hex')
  backend.instanceId = crypto.randomUUID()
  backend.dataDir = fs.mkdtempSync(path.join(os.tmpdir(), 'papermind-e2e-'))
  fs.writeFileSync(path.join(backend.dataDir, 'config.yaml'), buildTestConfigYaml())

  backend.proc = spawn(
    VENV_PYTHON,
    [
      '-m', 'uvicorn', 'app.main:app',
      '--host', '127.0.0.1',
      '--port', String(backend.port),
      '--workers', '1',
    ],
    { cwd: BACKEND_CWD, stdio: 'pipe', env: buildChildEnv(backend.dataDir, backend) },
  )
  backend.proc.stdout.on('data', (chunk) => pushLog(`[stdout] ${chunk.toString().trim()}`))
  backend.proc.stderr.on('data', (chunk) => pushLog(`[stderr] ${chunk.toString().trim()}`))

  const identity = { port: backend.port, token: backend.token, instanceId: backend.instanceId }
  const alive = await waitForBackend({
    timeoutMs: BOOT_TIMEOUT_MS,
    probe: () => isBackendAlive({ ...identity, timeoutMs: 1_000 }),
  })
  if (!alive) {
    await stopChild(backend.proc)
    assert.fail(`后端未能在 ${BOOT_TIMEOUT_MS}ms 内就绪:\n${backend.logTail.join('\n')}`)
  }
  t.diagnostic(`后端已就绪: http://127.0.0.1:${backend.port}（启动含探针共 ${Date.now() - SUITE_STARTED_AT}ms）`)
}

if (!RUN_RELEASE_E2E) {
  test('发布候选真实流程仅由显式 Gate 调度', { skip: '需 PAPERMIND_RELEASE_E2E=1' }, () => {})
} else {
test.before(async (t) => {
  if (process.env.RELEASE_FLOW_BACKEND_URL) attachTarget()
  else await spawnBackend(t)
})

// 兜底清理：任何路径下都不许把后端子进程泄漏到套件之外
test.after(async () => {
  if (backend.proc) await stopChild(backend.proc)
  if (backend.dataDir) fs.rmSync(backend.dataDir, { recursive: true, force: true })
  const elapsed = Date.now() - SUITE_STARTED_AT
  assert.ok(elapsed < TOTAL_BUDGET_MS, `E2E 总耗时 ${elapsed}ms 超过 ${TOTAL_BUDGET_MS}ms 预算`)
})

test('health：状态、版本、实例 ID 与 LLM 降级标记', { timeout: REQUEST_TIMEOUT_MS + 5_000 }, async () => {
  const { status, json } = await httpJson({
    port: backend.port, token: backend.token, path: '/api/health',
  })
  assert.equal(status, 200)
  assert.equal(json.status, 'ok')
  assert.equal(typeof json.version, 'string')
  if (backend.instanceId) assert.equal(json.instance_id, backend.instanceId)
  // LLM 指向回环死端口，必须报告未就绪；绝不允许真实外呼成功
  assert.equal(json.llm_ready, false)
})

test('能力边界：缺失令牌一律 401', { timeout: REQUEST_TIMEOUT_MS + 5_000 }, async () => {
  const { status, json } = await httpJson({ port: backend.port, path: '/api/papers' })
  assert.equal(status, 401)
  assert.equal(json.error_code, 'invalid_capability')
})

test('文献列表：空库返回合法分页结构', { timeout: REQUEST_TIMEOUT_MS + 5_000 }, async () => {
  const { status, json } = await httpJson({
    port: backend.port, token: backend.token, path: '/api/papers?limit=5',
  })
  assert.equal(status, 200)
  assert.equal(typeof json.total, 'number')
  assert.ok(Array.isArray(json.items))
})

test('检索：Embedding 不可用时降级路径仍可响应且结构合法', { timeout: REQUEST_TIMEOUT_MS + 5_000 }, async () => {
  const { status, json } = await httpJson({
    port: backend.port,
    token: backend.token,
    method: 'POST',
    path: '/api/search',
    body: { query: '多实例学习 T 分期', use_semantic: true, use_keyword: true, top_k: 5 },
  })
  // 语义通道在离线模型下不可用，检索必须降级而非 500
  assert.equal(status, 200)
  assert.equal(json.query, '多实例学习 T 分期')
  assert.ok(Array.isArray(json.results))
})

test('对话：SSE 帧格式与错误帧契约（不断言生成内容）', { timeout: REQUEST_TIMEOUT_MS + 5_000 }, async () => {
  const { status, headers, raw } = await readSseStream({
    port: backend.port,
    token: backend.token,
    path: '/api/chat',
    body: { message: '你好，文献库里有结直肠癌相关文献吗？' },
  })
  assert.equal(status, 200)
  assert.match(String(headers['content-type']), /text\/event-stream/)

  const frames = parseSseFrames(raw)
  assert.ok(frames.length >= 1, 'SSE 至少应产生一个帧')

  // 逐帧校验：要么是增量帧 {delta, finished:false}，要么是终态帧
  for (const frame of frames.slice(0, -1)) {
    if ('error' in frame) continue // 错误即终态，允许提前结束
    assert.equal(typeof frame.delta, 'string')
    assert.equal(frame.finished, false)
    assert.equal(typeof frame.conversation_id, 'number')
  }
  const last = frames[frames.length - 1]
  if ('error' in last) {
    // LLM 不可用（本套件配置）时的公开错误帧契约：通用文案 + error_code + conversation_id
    assert.equal(typeof last.error, 'string')
    assert.ok(last.error.length > 0)
    assert.equal(typeof last.error_code, 'string')
    assert.equal(typeof last.conversation_id, 'number')
    assert.ok(last.conversation_id >= 1)
  } else {
    // LLM 可用环境的终态帧契约
    assert.equal(last.finished, true)
    assert.equal(typeof last.content, 'string')
    assert.ok(Array.isArray(last.citations))
  }
})

test('统计页数据：总览结构与引用图骨架', { timeout: REQUEST_TIMEOUT_MS + 5_000 }, async () => {
  const { status, json } = await httpJson({
    port: backend.port, token: backend.token, path: '/api/papers/stats/overview',
  })
  assert.equal(status, 200)
  assert.equal(typeof json.total, 'number')
  assert.equal(typeof json.by_year, 'object')
  assert.equal(typeof json.by_status, 'object')
  assert.equal(typeof json.by_tag, 'object')
  assert.ok(Array.isArray(json.top_authors))
  assert.ok(Array.isArray(json.citation_graph.nodes))
  assert.ok(Array.isArray(json.citation_graph.links))
})

test('后端子进程干净关闭（泄漏即失败）', { timeout: 15_000 }, async () => {
  if (!backend.proc) return // 外挂调试模式无子进程可关
  const stopped = await stopChild(backend.proc)
  assert.equal(stopped, true, '后端子进程未能在 SIGTERM+SIGKILL 后退出')
  assert.equal(isProcessRunning(backend.proc), false, '后端子进程泄漏：退出后仍存活')
})
}
