import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const apiMocks = vi.hoisted(() => ({
  listConversations: vi.fn(),
  createConversation: vi.fn(),
  getHistory: vi.fn(),
  deleteConversation: vi.fn(),
  deleteMessagesFrom: vi.fn(),
  regenerateMessage: vi.fn(),
  analyzeImage: vi.fn(),
  apiFetch: vi.fn(),
}))

vi.mock('../api', () => ({
  listConversations: apiMocks.listConversations,
  createConversation: apiMocks.createConversation,
  getHistory: apiMocks.getHistory,
  deleteConversation: apiMocks.deleteConversation,
  deleteMessagesFrom: apiMocks.deleteMessagesFrom,
  regenerateMessage: apiMocks.regenerateMessage,
  analyzeImage: apiMocks.analyzeImage,
}))

vi.mock('../utils/apiUrl', () => ({
  apiFetch: apiMocks.apiFetch,
}))

import ChatPanel from './ChatPanel'


function pendingSSEForSignal(signal) {
  const abortError = new DOMException('aborted', 'AbortError')
  return {
    ok: true,
    body: {
      getReader: () => ({
        read: () => new Promise((resolve, reject) => {
          if (signal.aborted) reject(abortError)
          else signal.addEventListener('abort', () => reject(abortError), { once: true })
        }),
        cancel: vi.fn(),
        releaseLock: vi.fn(),
      }),
    },
  }
}


describe('ChatPanel operation lifecycle', () => {
  beforeEach(() => {
    apiMocks.listConversations.mockResolvedValue({ data: [] })
    apiMocks.createConversation.mockResolvedValue({
      data: { id: 7, title: '新对话', message_count: 0 },
    })
    Element.prototype.scrollIntoView = vi.fn()
  })

  it('快速双击发送只创建一个会话并只发出一个 POST', async () => {
    apiMocks.apiFetch.mockImplementation(async (_path, options) => pendingSSEForSignal(options.signal))
    const { unmount } = render(<ChatPanel fullHeight />)
    fireEvent.change(screen.getByPlaceholderText('输入问题，基于文献库回答...'), {
      target: { value: '一个问题' },
    })
    const send = screen.getByRole('button', { name: /发送/ })

    fireEvent.click(send)
    fireEvent.click(send)

    await waitFor(() => expect(apiMocks.apiFetch).toHaveBeenCalledOnce())
    expect(apiMocks.createConversation).toHaveBeenCalledOnce()
    unmount()
  })

  it('组件卸载时 abort 在途 SSE', async () => {
    let requestSignal
    apiMocks.apiFetch.mockImplementation(async (_path, options) => {
      requestSignal = options.signal
      return pendingSSEForSignal(options.signal)
    })
    const { unmount } = render(<ChatPanel fullHeight />)
    fireEvent.change(screen.getByPlaceholderText('输入问题，基于文献库回答...'), {
      target: { value: '长请求' },
    })
    fireEvent.click(screen.getByRole('button', { name: /发送/ }))
    await waitFor(() => expect(requestSignal).toBeDefined())

    unmount()

    expect(requestSignal.aborted).toBe(true)
  })

  it('对话 POST 失败时不自动重放非幂等请求', async () => {
    apiMocks.apiFetch.mockRejectedValue(new TypeError('connection lost'))
    render(<ChatPanel fullHeight />)
    fireEvent.change(screen.getByPlaceholderText('输入问题，基于文献库回答...'), {
      target: { value: '不应重放' },
    })

    fireEvent.click(screen.getByRole('button', { name: /发送/ }))

    await waitFor(() => expect(screen.getByText('[请求失败，请稍后重试]')).toBeInTheDocument())
    expect(apiMocks.apiFetch).toHaveBeenCalledOnce()
  })
})
