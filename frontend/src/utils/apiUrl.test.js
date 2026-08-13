import { beforeEach, describe, expect, it, vi } from 'vitest'
import {
  apiFetch,
  applyApiRequestConfig,
  getApiUrl,
  getProtectedResource,
  initializeRuntimeConfig,
  resetRuntimeConfigForTest,
} from './apiUrl'


describe('Electron 运行配置', () => {
  beforeEach(() => resetRuntimeConfigForTest())

  it('浏览器开发模式保持相对 URL 且不请求 IPC', async () => {
    const getRuntimeConfig = vi.fn()
    await initializeRuntimeConfig({ protocol: 'http:', electronAPI: { getRuntimeConfig } })
    expect(getApiUrl('/api/health')).toBe('/api/health')
    expect(getRuntimeConfig).not.toHaveBeenCalled()
  })

  it('file 模式只接受回环动态端口和 256-bit 十六进制令牌', async () => {
    await initializeRuntimeConfig({
      protocol: 'file:',
      electronAPI: { getRuntimeConfig: async () => ({
        apiBaseUrl: 'http://127.0.0.1:32768',
        apiToken: 'ab'.repeat(32),
      }) },
    })
    expect(getApiUrl('/api/health')).toBe('http://127.0.0.1:32768/api/health')
    expect(applyApiRequestConfig({ url: '/papers' })).toMatchObject({
      baseURL: 'http://127.0.0.1:32768/api',
      headers: { 'X-PaperMind-Token': 'ab'.repeat(32) },
    })
    expect(getProtectedResource('/api/papers/1/pdf')).toEqual({
      url: 'http://127.0.0.1:32768/api/papers/1/pdf',
      httpHeaders: { 'X-PaperMind-Token': 'ab'.repeat(32) },
      withCredentials: false,
    })
    expect(getProtectedResource('http://127.0.0.1:32768/api/papers/1/pdf').url)
      .toBe('http://127.0.0.1:32768/api/papers/1/pdf')
    expect(() => getProtectedResource('http://127.0.0.1:40000/api/papers/1/pdf')).toThrow()
  })

  it('file 模式缺失或伪造配置时安全失败', async () => {
    await expect(initializeRuntimeConfig({ protocol: 'file:' })).rejects.toThrow()
    await expect(initializeRuntimeConfig({
      protocol: 'file:',
      electronAPI: { getRuntimeConfig: async () => ({
        apiBaseUrl: 'http://evil.test:8000',
        apiToken: 'ab'.repeat(32),
      }) },
    })).rejects.toThrow()
  })

  it('统一 fetch 合并调用方 header 并注入能力头', async () => {
    await initializeRuntimeConfig({
      protocol: 'file:',
      electronAPI: { getRuntimeConfig: async () => ({
        apiBaseUrl: 'http://127.0.0.1:54321',
        apiToken: 'cd'.repeat(32),
      }) },
    })
    const fetchImpl = vi.fn(async () => ({ ok: true }))
    await apiFetch('/api/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
    }, fetchImpl)
    const [url, options] = fetchImpl.mock.calls[0]
    expect(url).toBe('http://127.0.0.1:54321/api/chat')
    expect(options.headers.get('Content-Type')).toBe('application/json')
    expect(options.headers.get('X-PaperMind-Token')).toBe('cd'.repeat(32))
  })
})
