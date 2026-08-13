const assert = require('node:assert/strict')
const test = require('node:test')

const {
  createRuntimeIdentity,
  getAvailablePort,
  isAllowedRuntimeConfigRequest,
} = require('../runtime-identity')


test('每次启动生成 256-bit token、UUID instance 与合法高位端口', async () => {
  const identity = await createRuntimeIdentity({
    getPort: async () => 49152,
    randomBytes: (size) => Buffer.alloc(size, 0xab),
    randomUUID: () => '12345678-1234-4234-8234-123456789abc',
  })

  assert.equal(identity.port, 49152)
  assert.equal(identity.token, 'ab'.repeat(32))
  assert.equal(identity.token.length, 64)
  assert.equal(identity.instanceId, '12345678-1234-4234-8234-123456789abc')
})


test('端口选择只监听 127.0.0.1 并在读取后释放探测 socket', async () => {
  const calls = []
  const server = {
    once(event, handler) { calls.push(`once:${event}`); this.errorHandler = handler },
    listen(options, callback) {
      calls.push(options)
      this.address = () => ({ port: 32768 })
      callback()
    },
    close(callback) { calls.push('close'); callback() },
  }

  const port = await getAvailablePort({ createServer: () => server })

  assert.equal(port, 32768)
  assert.deepEqual(calls, ['once:error', { host: '127.0.0.1', port: 0, exclusive: true }, 'close'])
})


test('运行配置 IPC 只允许主窗口且仅在 file 页面完成加载后读取', () => {
  const sender = { id: 7, getURL: () => 'file:///app/frontend/dist/index.html' }
  const entry = 'file:///app/frontend/dist/index.html'
  assert.equal(isAllowedRuntimeConfigRequest({ sender }, sender, entry), true)
  assert.equal(isAllowedRuntimeConfigRequest({ sender: { id: 8, getURL: sender.getURL } }, sender, entry), false)
  assert.equal(isAllowedRuntimeConfigRequest({ sender: { id: 7, getURL: sender.getURL } }, sender, entry), false)
  sender.getURL = () => 'file:///etc/passwd'
  assert.equal(isAllowedRuntimeConfigRequest({ sender }, sender, entry), false)
  sender.getURL = () => 'https://evil.test'
  assert.equal(isAllowedRuntimeConfigRequest({ sender }, sender, entry), false)
})
