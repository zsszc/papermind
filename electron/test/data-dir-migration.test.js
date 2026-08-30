// Batch 24 T4（24D）：安装/升级/回滚数据目录验证。
//
// 模拟 v1 数据目录（papers.db 旧 schema / vector_db / config.yaml 旧配置），
// 以「新实例身份」真启后端子进程，断言三条升级契约：
//   1. 升级保留数据：旧库文献经新后端完整可读；
//   2. ensure_schema 迁移幂等：两次启动后 schema 快照逐字节一致；
//   3. 回滚不炸 / 配置不被覆盖：旧配置缺字段按模板补默认值（仅内存，不回写），
//      用户 API Key 与自定义项逐字节保留。
//
// 纪律：LLM 指向本机回环死端口，除本机回环零网络；子进程必清理（泄漏即失败）。

const assert = require('node:assert/strict')
const { spawn, spawnSync } = require('node:child_process')
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
const VENV_PYTHON = path.join(PROJECT_ROOT, 'backend', 'venv', 'bin', 'python')
const BACKEND_CWD = path.join(PROJECT_ROOT, 'backend')

const BOOT_TIMEOUT_MS = 60_000
const REQUEST_TIMEOUT_MS = 15_000

// v1 旧库建库脚本：刻意缺少后增列（papers.metadata_json/last_read_page、
// chunks.page_start/page_end、conversations.paper_ids/summary、
// messages.citations/skill_used/token_usage/revision）与后增表，模拟旧版安装。
const SEED_V1_PY = `
import sqlite3, sys
db = sys.argv[1]
conn = sqlite3.connect(db)
conn.executescript("""
CREATE TABLE papers (
  id INTEGER PRIMARY KEY,
  title VARCHAR(500),
  authors TEXT,
  year INTEGER,
  journal VARCHAR(500),
  abstract TEXT,
  doi VARCHAR(200),
  pages INTEGER,
  file_path VARCHAR(1000) NOT NULL,
  filename VARCHAR(500) NOT NULL,
  status VARCHAR(50),
  source VARCHAR(50),
  processed VARCHAR(50),
  created_at DATETIME,
  updated_at DATETIME
);
INSERT INTO papers (title, authors, year, journal, abstract, file_path, filename,
                    status, source, processed, created_at, updated_at)
VALUES ('v1 遗留文献：结直肠癌 T 分期', '张三, 李四', 2021, '中华病理学杂志',
        '多实例学习用于结直肠癌 T 分期预测。', 'papers/v1-old.pdf', 'v1-old.pdf',
        'read', 'local', 'done', '2024-01-01 00:00:00', '2024-01-01 00:00:00');
CREATE TABLE chunks (
  id INTEGER PRIMARY KEY,
  paper_id INTEGER NOT NULL REFERENCES papers(id),
  content TEXT NOT NULL,
  page_number INTEGER,
  chunk_index INTEGER NOT NULL DEFAULT 0,
  section_title VARCHAR(500),
  chunk_type VARCHAR(50),
  token_count INTEGER,
  created_at DATETIME
);
INSERT INTO chunks (paper_id, content, page_number, chunk_index, chunk_type,
                    token_count, created_at)
VALUES (1, 'v1 遗留 chunk：T 分期多实例学习。', 1, 0, 'abstract', 12,
        '2024-01-01 00:00:00');
CREATE TABLE conversations (
  id INTEGER PRIMARY KEY,
  title VARCHAR(500),
  message_count INTEGER,
  created_at DATETIME,
  updated_at DATETIME
);
INSERT INTO conversations (title, message_count, created_at, updated_at)
VALUES ('v1 旧会话', 1, '2024-01-01 00:00:00', '2024-01-01 00:00:00');
CREATE TABLE messages (
  id INTEGER PRIMARY KEY,
  conversation_id INTEGER NOT NULL REFERENCES conversations(id),
  role VARCHAR(50) NOT NULL,
  content TEXT NOT NULL,
  created_at DATETIME
);
INSERT INTO messages (conversation_id, role, content, created_at)
VALUES (1, 'user', 'v1 旧消息', '2024-01-01 00:00:00');
""")
conn.commit()
conn.close()
`

// v1 旧配置：缺 retrieval/memory/ui/export 等后增段与 llm.max_tokens 等后增键，
// 含用户真实 Key 与自定义 model；base_url 指向回环死端口，保证零外网。
const V1_CONFIG_YAML = `app:
  name: "PaperMind"
  version: "1.0.0"
  data_dir: "./data"
llm:
  provider: "moonshot"
  api_key: "sk-v1-user-key-abcdef0123456789"
  base_url: "http://127.0.0.1:9/v1"
  model: "user-custom-model"
`

// 只读 dump 全库表结构（表名 -> 列名列表），用于迁移幂等性快照对比。
const SCHEMA_DUMP_PY = `
import json, sqlite3, sys
conn = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
out = {}
for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"):
    out[name] = [row[1] for row in conn.execute(f"PRAGMA table_info({name})")]
print(json.dumps(out, sort_keys=True))
`

// 以目标数据目录加载配置单例并 dump 关键键，验证「补默认 + 不覆盖」契约。
const CONFIG_DUMP_PY = `
import json
from app.core.config import config
print(json.dumps({
  "chat_profile": config.get("retrieval.chat_profile"),
  "page_size": config.get("ui.page_size"),
  "max_tokens": config.get("llm.max_tokens"),
  "api_key": config.get("llm.api_key"),
  "model": config.get("llm.model"),
}, ensure_ascii=False))
`

function sha256File(filePath) {
  return crypto.createHash('sha256').update(fs.readFileSync(filePath)).digest('hex')
}

// 子进程环境白名单：与 release-flow 同纪律，剥离代理与 PYTHONPATH 污染。
function buildChildEnv(extra = {}) {
  return {
    PATH: process.env.PATH || '/usr/bin:/bin',
    HOME: process.env.HOME || os.homedir(),
    TMPDIR: process.env.TMPDIR || os.tmpdir(),
    HF_HUB_OFFLINE: '1',
    TRANSFORMERS_OFFLINE: '1',
    NO_PROXY: '127.0.0.1,localhost',
    ...extra,
  }
}

function runPython(args, env) {
  const result = spawnSync(VENV_PYTHON, args, {
    cwd: BACKEND_CWD,
    env: buildChildEnv(env),
    encoding: 'utf8',
    timeout: 60_000,
  })
  assert.equal(result.error, undefined, `python 子进程执行失败: ${result.error}`)
  assert.equal(result.status, 0, `python 子进程退出码非零:\n${result.stderr}`)
  return result.stdout.trim()
}

function httpJson({ port, token, path: reqPath }) {
  return new Promise((resolve, reject) => {
    const request = http.get(
      {
        hostname: '127.0.0.1',
        port,
        path: reqPath,
        headers: token ? { 'X-PaperMind-Token': token } : {},
      },
      (response) => {
        let raw = ''
        response.on('data', (chunk) => { raw += chunk.toString() })
        response.on('end', () => resolve({ status: response.statusCode, json: JSON.parse(raw) }))
        response.on('error', reject)
      },
    )
    request.on('error', reject)
    request.setTimeout(REQUEST_TIMEOUT_MS, () => {
      request.destroy(new Error(`请求超时: ${reqPath}`))
    })
  })
}

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

// 以新实例身份在指定数据目录上拉起后端；返回句柄供断言与关闭。
async function bootBackend(t, dataDir, label) {
  const port = await getAvailablePort()
  const identity = {
    port,
    token: crypto.randomBytes(32).toString('hex'),
    instanceId: crypto.randomUUID(),
  }
  const logTail = []
  const proc = spawn(
    VENV_PYTHON,
    ['-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1',
     '--port', String(port), '--workers', '1'],
    {
      cwd: BACKEND_CWD,
      stdio: 'pipe',
      env: buildChildEnv({
        PAPERMIND_DATA_DIR: dataDir,
        PAPERMIND_API_TOKEN: identity.token,
        PAPERMIND_INSTANCE_ID: identity.instanceId,
      }),
    },
  )
  proc.stdout.on('data', (chunk) => logTail.push(chunk.toString().trim()))
  proc.stderr.on('data', (chunk) => logTail.push(chunk.toString().trim()))

  const alive = await waitForBackend({
    timeoutMs: BOOT_TIMEOUT_MS,
    probe: () => isBackendAlive({ ...identity, timeoutMs: 1_000 }),
  })
  if (!alive) {
    await stopChild(proc)
    assert.fail(`[${label}] 后端未能在 ${BOOT_TIMEOUT_MS}ms 内就绪:\n${logTail.join('\n')}`)
  }
  t.diagnostic(`[${label}] 后端就绪: http://127.0.0.1:${port}`)
  return { proc, ...identity }
}

// --- 套件共享夹具：v1 数据目录只建一次，两次启动共用 ---
const fixture = { dir: '', dbPath: '', configPath: '', configSha256Before: '' }
const active = { proc: null }

test.before(() => {
  fixture.dir = fs.mkdtempSync(path.join(os.tmpdir(), 'papermind-v1-'))
  fs.mkdirSync(path.join(fixture.dir, 'vector_db'), { recursive: true })
  fixture.dbPath = path.join(fixture.dir, 'papers.db')
  fixture.configPath = path.join(fixture.dir, 'config.yaml')
  runPython(['-c', SEED_V1_PY, fixture.dbPath], {})
  fs.writeFileSync(fixture.configPath, V1_CONFIG_YAML)
  fixture.configSha256Before = sha256File(fixture.configPath)
})

test.after(async () => {
  // 兜底清理：任何失败路径下都不许泄漏后端子进程
  if (active.proc) await stopChild(active.proc)
  if (fixture.dir) fs.rmSync(fixture.dir, { recursive: true, force: true })
})

let firstBootSchema = null

test('升级：v1 旧库启动新实例后数据完整可读且 schema 已迁移', { timeout: BOOT_TIMEOUT_MS + 30_000 }, async (t) => {
  const boot = await bootBackend(t, fixture.dir, '首次启动')
  active.proc = boot.proc
  try {
    // 数据完整可读：旧文献经列表与详情两条路径均可取回
    const list = await httpJson({ port: boot.port, token: boot.token, path: '/api/papers' })
    assert.equal(list.status, 200)
    assert.equal(list.json.total, 1)
    assert.equal(list.json.items[0].title, 'v1 遗留文献：结直肠癌 T 分期')
    assert.equal(list.json.items[0].year, 2021)
    assert.equal(list.json.items[0].status, 'read')

    const detail = await httpJson({ port: boot.port, token: boot.token, path: '/api/papers/1' })
    assert.equal(detail.status, 200)
    assert.equal(detail.json.title, 'v1 遗留文献：结直肠癌 T 分期')

    // schema 迁移生效：后增列与后增表全部就位
    firstBootSchema = JSON.parse(runPython(['-c', SCHEMA_DUMP_PY, fixture.dbPath], {}))
    assert.ok(firstBootSchema.papers.includes('metadata_json'), 'papers 缺 metadata_json 迁移列')
    assert.ok(firstBootSchema.papers.includes('last_read_page'), 'papers 缺 last_read_page 迁移列')
    assert.ok(firstBootSchema.chunks.includes('page_start'), 'chunks 缺 page_start 迁移列')
    assert.ok(firstBootSchema.chunks.includes('page_end'), 'chunks 缺 page_end 迁移列')
    assert.ok(firstBootSchema.conversations.includes('paper_ids'), 'conversations 缺 paper_ids 迁移列')
    assert.ok(firstBootSchema.conversations.includes('summary'), 'conversations 缺 summary 迁移列')
    for (const col of ['citations', 'skill_used', 'token_usage', 'revision']) {
      assert.ok(firstBootSchema.messages.includes(col), `messages 缺 ${col} 迁移列`)
    }
    assert.ok('paper_citations' in firstBootSchema, '缺 paper_citations 迁移表')
    assert.ok('papers_fts' in firstBootSchema, '缺 papers_fts 全文检索表')
  } finally {
    const stopped = await stopChild(boot.proc)
    active.proc = null
    assert.equal(stopped, true, '首次启动的后端子进程泄漏')
  }
})

test('幂等：同一数据目录二次启动迁移无副作用且数据仍在', { timeout: BOOT_TIMEOUT_MS + 30_000 }, async (t) => {
  const boot = await bootBackend(t, fixture.dir, '二次启动')
  active.proc = boot.proc
  try {
    const list = await httpJson({ port: boot.port, token: boot.token, path: '/api/papers' })
    assert.equal(list.status, 200)
    assert.equal(list.json.total, 1, '二次启动后旧数据丢失')

    // ensure_schema 幂等：两次启动后的全库 schema 快照逐字节一致
    const secondBootSchema = JSON.parse(runPython(['-c', SCHEMA_DUMP_PY, fixture.dbPath], {}))
    assert.deepEqual(secondBootSchema, firstBootSchema, '二次启动后 schema 发生变化，迁移不幂等')
  } finally {
    const stopped = await stopChild(boot.proc)
    active.proc = null
    assert.equal(stopped, true, '二次启动的后端子进程泄漏')
  }
})

test('回滚：旧配置缺字段按模板补默认且用户 Key 与自定义项不丢', { timeout: 60_000 }, () => {
  // 配置不被覆盖：经历两次完整启动后文件逐字节不变（用户 Key 不落任何安装内容）
  assert.equal(
    sha256File(fixture.configPath),
    fixture.configSha256Before,
    'config.yaml 在升级启动后被改写，违反「配置不被覆盖」契约',
  )

  // 缺省补齐：旧配置缺失的后增键按公开模板补默认值（内存级，不回写磁盘）
  const loaded = JSON.parse(
    runPython(['-c', CONFIG_DUMP_PY], { PAPERMIND_DATA_DIR: fixture.dir }),
  )
  assert.equal(loaded.chat_profile, 'hybrid', '缺失的 retrieval.chat_profile 未按模板补默认值')
  assert.equal(loaded.page_size, 20, '缺失的 ui.page_size 未按模板补默认值')
  assert.equal(loaded.max_tokens, 4096, '缺失的 llm.max_tokens 未按模板补默认值')
  // 不丢用户 Key：既有值一律保留，模板默认不得覆盖
  assert.equal(loaded.api_key, 'sk-v1-user-key-abcdef0123456789', '用户 API Key 被覆盖')
  assert.equal(loaded.model, 'user-custom-model', '用户自定义 model 被模板覆盖')
})
