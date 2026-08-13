const TOKEN_HEADER = 'X-PaperMind-Token'
let runtimeConfig = { apiBaseUrl: '', apiToken: '' }


function validateRuntimeConfig(value) {
  const base = new URL(value?.apiBaseUrl)
  const port = Number(base.port)
  if (
    base.protocol !== 'http:'
    || base.hostname !== '127.0.0.1'
    || base.pathname !== '/'
    || base.search
    || base.hash
    || !Number.isInteger(port)
    || port < 1024
    || port > 65535
    || !/^[0-9a-f]{64}$/i.test(value?.apiToken || '')
  ) {
    throw new Error('Electron 后端运行配置非法')
  }
  return {
    apiBaseUrl: base.origin,
    apiToken: value.apiToken,
  }
}


export async function initializeRuntimeConfig({
  protocol = typeof window === 'undefined' ? '' : window.location.protocol,
  electronAPI = typeof window === 'undefined' ? undefined : window.electronAPI,
} = {}) {
  if (protocol !== 'file:') {
    runtimeConfig = { apiBaseUrl: '', apiToken: '' }
    return runtimeConfig
  }
  if (typeof electronAPI?.getRuntimeConfig !== 'function') {
    throw new Error('无法读取 Electron 后端运行配置')
  }
  runtimeConfig = validateRuntimeConfig(await electronAPI.getRuntimeConfig())
  return runtimeConfig
}


export function resetRuntimeConfigForTest() {
  runtimeConfig = { apiBaseUrl: '', apiToken: '' }
}


export function getApiBaseUrl() {
  return runtimeConfig.apiBaseUrl
}


export function getApiUrl(path) {
  if (/^https?:\/\//i.test(path)) {
    const absolute = new URL(path)
    if (!runtimeConfig.apiBaseUrl || absolute.origin !== runtimeConfig.apiBaseUrl) {
      throw new Error('拒绝访问非 PaperMind 后端地址')
    }
    return absolute.href
  }
  const normalized = path.startsWith('/') ? path : `/${path}`
  return `${runtimeConfig.apiBaseUrl}${normalized}`
}


export function getCapabilityHeaders() {
  return runtimeConfig.apiToken ? { [TOKEN_HEADER]: runtimeConfig.apiToken } : {}
}


export function applyApiRequestConfig(config) {
  return {
    ...config,
    baseURL: `${runtimeConfig.apiBaseUrl}/api`,
    headers: {
      ...config.headers,
      ...getCapabilityHeaders(),
    },
  }
}


export function apiFetch(path, options = {}, fetchImpl = fetch) {
  const headers = new Headers(options.headers || {})
  for (const [name, value] of Object.entries(getCapabilityHeaders())) headers.set(name, value)
  return fetchImpl(getApiUrl(path), { ...options, headers, credentials: 'omit' })
}


export function getProtectedResource(path) {
  const url = getApiUrl(path)
  if (!runtimeConfig.apiToken) return url
  return {
    url,
    httpHeaders: getCapabilityHeaders(),
    withCredentials: false,
  }
}
