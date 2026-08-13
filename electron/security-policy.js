const DEV_ORIGINS = new Set([
  'http://localhost:5173',
  'http://127.0.0.1:5173',
])


function createSecureWebPreferences(preloadPath) {
  return {
    preload: preloadPath,
    contextIsolation: true,
    nodeIntegration: false,
    sandbox: true,
    webSecurity: true,
    allowRunningInsecureContent: false,
  }
}


function normalizedDocumentUrl(rawUrl) {
  const url = new URL(rawUrl)
  url.hash = ''
  url.search = ''
  return url.href
}


function isAllowedNavigation(targetUrl, { isDev, productionEntryUrl } = {}) {
  try {
    const target = new URL(targetUrl)
    if (isDev) return DEV_ORIGINS.has(target.origin)
    if (!productionEntryUrl || target.protocol !== 'file:') return false
    return normalizedDocumentUrl(target.href) === normalizedDocumentUrl(productionEntryUrl)
  } catch {
    return false
  }
}


function installWindowGuards(webContents, options) {
  webContents.setWindowOpenHandler(() => ({ action: 'deny' }))
  webContents.on('will-navigate', (event, targetUrl) => {
    if (!isAllowedNavigation(targetUrl, options)) event.preventDefault()
  })
}


function installPermissionGuards(session) {
  session.setPermissionCheckHandler(() => false)
  session.setPermissionRequestHandler((_webContents, _permission, callback) => callback(false))
  if (typeof session.setDevicePermissionHandler === 'function') {
    session.setDevicePermissionHandler(() => false)
  }
}


function focusExistingWindow(window) {
  if (!window || window.isDestroyed()) return false
  if (window.isMinimized()) window.restore()
  window.show()
  window.focus()
  return true
}


module.exports = {
  createSecureWebPreferences,
  focusExistingWindow,
  installPermissionGuards,
  installWindowGuards,
  isAllowedNavigation,
}
