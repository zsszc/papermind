export function getApiBaseUrl() {
  if (typeof window === 'undefined') return ''
  return window.location.protocol === 'file:' ? 'http://127.0.0.1:8000' : ''
}

export function getApiUrl(path) {
  const base = getApiBaseUrl()
  const normalized = path.startsWith('/') ? path : `/${path}`
  return `${base}${normalized}`
}
