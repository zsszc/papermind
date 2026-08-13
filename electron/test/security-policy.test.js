const assert = require('node:assert/strict')
const fs = require('node:fs')
const path = require('node:path')
const test = require('node:test')

const {
  createSecureWebPreferences,
  focusExistingWindow,
  installPermissionGuards,
  installWindowGuards,
  isAllowedNavigation,
} = require('../security-policy')


test('BrowserWindow 使用隔离、sandbox 与 webSecurity 最小权限', () => {
  assert.deepEqual(createSecureWebPreferences('/safe/preload.js'), {
    preload: '/safe/preload.js',
    contextIsolation: true,
    nodeIntegration: false,
    sandbox: true,
    webSecurity: true,
    allowRunningInsecureContent: false,
  })
})


test('导航只允许当前开发入口或生产 file 页面', () => {
  const development = { isDev: true }
  const production = {
    isDev: false,
    productionEntryUrl: 'file:///Applications/PaperMind/frontend/dist/index.html',
  }
  assert.equal(isAllowedNavigation('http://localhost:5173/', development), true)
  assert.equal(isAllowedNavigation('http://127.0.0.1:5173/search?q=a', development), true)
  assert.equal(isAllowedNavigation('http://localhost:5174/', development), false)
  assert.equal(isAllowedNavigation('https://example.com/', development), false)
  assert.equal(isAllowedNavigation(
    'file:///Applications/PaperMind/frontend/dist/index.html#search',
    production,
  ), true)
  assert.equal(isAllowedNavigation('file:///etc/passwd', production), false)
  assert.equal(isAllowedNavigation('javascript:alert(1)', production), false)
  assert.equal(isAllowedNavigation('https://example.com/', production), false)
  assert.equal(isAllowedNavigation('not a url', production), false)
})


test('窗口守卫拒绝弹窗并阻止不受信导航', () => {
  let navigationHandler
  let openHandler
  const webContents = {
    on: (event, handler) => {
      if (event === 'will-navigate') navigationHandler = handler
    },
    setWindowOpenHandler: (handler) => { openHandler = handler },
  }
  installWindowGuards(webContents, { isDev: true })

  assert.deepEqual(openHandler({ url: 'https://example.com' }), { action: 'deny' })

  let prevented = false
  navigationHandler({ preventDefault: () => { prevented = true } }, 'https://example.com')
  assert.equal(prevented, true)

  prevented = false
  navigationHandler({ preventDefault: () => { prevented = true } }, 'http://localhost:5173/search')
  assert.equal(prevented, false)
})


test('session 默认拒绝检查、请求与设备权限', () => {
  const handlers = {}
  const session = {
    setPermissionCheckHandler: (handler) => { handlers.check = handler },
    setPermissionRequestHandler: (handler) => { handlers.request = handler },
    setDevicePermissionHandler: (handler) => { handlers.device = handler },
  }
  installPermissionGuards(session)

  assert.equal(handlers.check(null, 'clipboard-read'), false)
  assert.equal(handlers.device({ deviceType: 'usb' }), false)
  let decision
  handlers.request(null, 'notifications', (allowed) => { decision = allowed })
  assert.equal(decision, false)
})


test('第二实例恢复并聚焦已有窗口', () => {
  const calls = []
  const window = {
    isDestroyed: () => false,
    isMinimized: () => true,
    restore: () => calls.push('restore'),
    show: () => calls.push('show'),
    focus: () => calls.push('focus'),
  }
  assert.equal(focusExistingWindow(window), true)
  assert.deepEqual(calls, ['restore', 'show', 'focus'])
  assert.equal(focusExistingWindow(null), false)
  assert.equal(focusExistingWindow({ isDestroyed: () => true }), false)
})


test('前端入口声明 Electron CSP 安全边界', () => {
  const html = fs.readFileSync(path.join(__dirname, '../../frontend/index.html'), 'utf8')
  assert.match(html, /http-equiv=["']Content-Security-Policy["']/)
  assert.match(html, /default-src 'self'/)
  assert.match(html, /object-src 'none'/)
  assert.match(html, /frame-src 'none'/)
  assert.match(html, /connect-src[^;]*http:\/\/127\.0\.0\.1:\*/)
  assert.doesNotMatch(html, /connect-src[^;]*http:\/\/\*/)
  assert.doesNotMatch(html, /script-src[^;]*unsafe-inline/)
})
