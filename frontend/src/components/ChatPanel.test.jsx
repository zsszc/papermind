import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

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


function responseFromEvents(events) {
  const encoder = new TextEncoder()
  let sent = false
  return {
    ok: true,
    body: {
      getReader: () => ({
        read: vi.fn(async () => {
          if (sent) return { done: true, value: undefined }
          sent = true
          return {
            done: false,
            value: encoder.encode(events.map((event) => `data: ${JSON.stringify(event)}\n\n`).join('')),
          }
        }),
        cancel: vi.fn(),
        releaseLock: vi.fn(),
      }),
    },
  }
}


describe('ChatPanel operation lifecycle', () => {
  beforeEach(() => {
    vi.resetAllMocks()
    apiMocks.listConversations.mockResolvedValue({ data: [] })
    apiMocks.createConversation.mockResolvedValue({
      data: { id: 7, title: '新对话', message_count: 0 },
    })
    Element.prototype.scrollIntoView = vi.fn()
  })

  afterEach(() => cleanup())

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

  it('finished 原子替换 provisional 正文并展示实际引用', async () => {
    apiMocks.apiFetch.mockResolvedValue(responseFromEvents([
      { delta: '答案[^1^]越界[^9^]', finished: false },
      {
        delta: '',
        finished: true,
        content: '答案[^1^]越界',
        citations: [{ paper_id: 1, title: '公开合成论文', year: 2026 }],
        verification: { total: 2, valid: 1, removed: 1, verified: false },
      },
    ]))
    render(<ChatPanel fullHeight />)
    fireEvent.change(screen.getByPlaceholderText('输入问题，基于文献库回答...'), {
      target: { value: '引用问题' },
    })

    fireEvent.click(screen.getByRole('button', { name: /发送/ }))

    await waitFor(() => expect(document.body.textContent).toContain('答案[^1^]越界'))
    expect(document.body.textContent).not.toContain('[^9^]')
    expect(screen.getByText('公开合成论文（2026）')).toBeInTheDocument()
  })

  it('delta 后 error 丢弃半条正文且不执行成功刷新', async () => {
    apiMocks.apiFetch.mockResolvedValue(responseFromEvents([
      { delta: '不应保留的半条秘密', finished: false },
      { error: '服务失败' },
    ]))
    render(<ChatPanel fullHeight />)
    fireEvent.change(screen.getByPlaceholderText('输入问题，基于文献库回答...'), {
      target: { value: '失败问题' },
    })

    fireEvent.click(screen.getByRole('button', { name: /发送/ }))

    expect(await screen.findByText('[请求失败，请稍后重试]')).toBeInTheDocument()
    expect(document.body.textContent).not.toContain('不应保留的半条秘密')
    expect(apiMocks.listConversations).toHaveBeenCalledTimes(1)
  })

  it('历史引用只渲染在所属 assistant 消息内', async () => {
    apiMocks.listConversations.mockResolvedValue({
      data: [{ id: 3, title: '引用历史', message_count: 4 }],
    })
    apiMocks.getHistory.mockResolvedValue({
      data: {
        messages: [
          { id: 1, role: 'user', content: '问题一', citations: [] },
          {
            id: 2,
            role: 'assistant',
            content: '回答一',
            citations: [{ paper_id: 1, title: '第一篇证据', year: 2024 }],
          },
          { id: 3, role: 'user', content: '问题二', citations: [] },
          {
            id: 4,
            role: 'assistant',
            content: '回答二',
            citations: [{ paper_id: 2, title: '第二篇证据', year: 2025 }],
          },
        ],
      },
    })
    render(<ChatPanel fullHeight />)

    fireEvent.click(screen.getByRole('button', { name: /历史/ }))
    fireEvent.click(await screen.findByText('引用历史'))

    const firstAnswer = await screen.findByText('回答一')
    const secondAnswer = await screen.findByText('回答二')
    expect(firstAnswer.closest('[data-message-role="assistant"]')).toHaveTextContent('第一篇证据（2024）')
    expect(firstAnswer.closest('[data-message-role="assistant"]')).not.toHaveTextContent('第二篇证据')
    expect(secondAnswer.closest('[data-message-role="assistant"]')).toHaveTextContent('第二篇证据（2025）')
  })

  it('finished 缺少 content 时丢弃 provisional 并按协议失败', async () => {
    apiMocks.apiFetch.mockResolvedValue(responseFromEvents([
      { delta: '不完整的半条回答', finished: false },
      { delta: '', finished: true, citations: [{ paper_id: 1, title: '不应提交的引用' }] },
    ]))
    render(<ChatPanel fullHeight />)
    fireEvent.change(screen.getByPlaceholderText('输入问题，基于文献库回答...'), {
      target: { value: '协议问题' },
    })

    fireEvent.click(screen.getByRole('button', { name: /发送/ }))

    expect(await screen.findByText('[请求失败，请稍后重试]')).toBeInTheDocument()
    expect(document.body.textContent).not.toContain('不完整的半条回答')
    expect(document.body.textContent).not.toContain('不应提交的引用')
  })

  it('图片 delta 后 error 丢弃半条正文', async () => {
    apiMocks.analyzeImage.mockResolvedValue(responseFromEvents([
      { delta: '图片半条秘密', finished: false },
      { error: '图片服务失败' },
    ]))
    const { container } = render(<ChatPanel fullHeight />)
    const fileInput = container.querySelector('input[type="file"]')
    fireEvent.change(fileInput, {
      target: { files: [new File(['image'], 'figure.png', { type: 'image/png' })] },
    })
    fireEvent.change(screen.getByPlaceholderText('输入问题，基于文献库回答...'), {
      target: { value: '分析图片' },
    })

    fireEvent.click(screen.getByRole('button', { name: /发送/ }))

    expect(await screen.findByText('[图片分析失败，请稍后重试]')).toBeInTheDocument()
    expect(document.body.textContent).not.toContain('图片半条秘密')
  })
})
