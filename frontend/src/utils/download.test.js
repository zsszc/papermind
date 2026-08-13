import { describe, expect, it, vi } from 'vitest'

import { downloadUrl } from './download'


describe('downloadUrl', () => {
  it('通过 fetch 和临时 Blob 链接下载，不依赖 window.open', async () => {
    const blob = new Blob(['pdf'], { type: 'application/pdf' })
    const fetchImpl = vi.fn().mockResolvedValue({ ok: true, blob: async () => blob })
    const link = { click: vi.fn(), remove: vi.fn() }
    const documentRef = {
      body: { appendChild: vi.fn() },
      createElement: vi.fn().mockReturnValue(link),
    }
    const urlApi = {
      createObjectURL: vi.fn().mockReturnValue('blob:download'),
      revokeObjectURL: vi.fn(),
    }

    await downloadUrl('http://127.0.0.1:8000/static/papers/a%20b.pdf', {
      fetchImpl,
      documentRef,
      urlApi,
      headers: { 'X-PaperMind-Token': 'test-token' },
    })

    expect(fetchImpl).toHaveBeenCalledWith(
      'http://127.0.0.1:8000/static/papers/a%20b.pdf',
      { credentials: 'omit', headers: { 'X-PaperMind-Token': 'test-token' } },
    )
    expect(link.href).toBe('blob:download')
    expect(link.download).toBe('a b.pdf')
    expect(link.click).toHaveBeenCalledOnce()
    expect(link.remove).toHaveBeenCalledOnce()
    expect(urlApi.revokeObjectURL).toHaveBeenCalledWith('blob:download')
  })

  it('HTTP 失败时不创建下载链接', async () => {
    const fetchImpl = vi.fn().mockResolvedValue({ ok: false, status: 404 })
    await expect(downloadUrl('/missing.pdf', { fetchImpl })).rejects.toThrow('HTTP 404')
  })
})
