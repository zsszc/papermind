import { beforeEach, describe, expect, it, vi } from 'vitest'

const { apiFetchMock } = vi.hoisted(() => ({
  apiFetchMock: vi.fn(),
}))

vi.mock('./utils/apiUrl', () => ({
  apiFetch: apiFetchMock,
  applyApiRequestConfig: (config) => config,
}))

import { analyzeImage, regenerateMessage } from './api'


describe('chat API abort signal', () => {
  beforeEach(() => {
    apiFetchMock.mockResolvedValue({ ok: true })
  })

  it('重新生成传递 AbortSignal', async () => {
    const controller = new AbortController()

    await regenerateMessage(2, 9, 3, { signal: controller.signal })

    expect(apiFetchMock).toHaveBeenCalledWith(
      '/api/chat/conversations/2/messages/9/regenerate',
      expect.objectContaining({
        method: 'POST',
        signal: controller.signal,
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ expected_revision: 3 }),
      })
    )
  })

  it('图片分析传递 AbortSignal', async () => {
    const controller = new AbortController()
    const file = new File(['image'], 'figure.png', { type: 'image/png' })

    await analyzeImage(file, '解读', { signal: controller.signal })

    expect(apiFetchMock).toHaveBeenCalledWith(
      '/api/chat/analyze-image',
      expect.objectContaining({ method: 'POST', signal: controller.signal })
    )
  })
})
