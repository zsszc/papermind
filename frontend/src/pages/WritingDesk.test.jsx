import React from 'react'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const apiMocks = vi.hoisted(() => ({
  listThesis: vi.fn(),
  getThesis: vi.fn(),
  getChapterText: vi.fn(),
  suggestCitations: vi.fn(),
}))

vi.mock('../api', () => apiMocks)

import WritingDesk from './WritingDesk'

const theses = [
  { id: 1, title: '论文一' },
  { id: 2, title: '论文二' },
]

describe('WritingDesk', () => {
  afterEach(() => cleanup())
  beforeEach(() => {
    localStorage.clear()
    apiMocks.listThesis.mockReset().mockResolvedValue({ data: { items: theses } })
    apiMocks.getThesis.mockReset().mockResolvedValue({ data: { chapter_structure: [] } })
    apiMocks.getChapterText.mockReset()
    apiMocks.suggestCitations.mockReset()
  })

  it('首屏同步恢复草稿，不被初始空值覆盖', async () => {
    let resolveList
    apiMocks.listThesis.mockReturnValue(new Promise((resolve) => { resolveList = resolve }))
    localStorage.setItem('writing-desk-state', JSON.stringify({
      selectedThesis: 1,
      drafts: { 1: '已保存草稿' },
    }))

    render(<WritingDesk />)

    expect(screen.getByPlaceholderText('在此输入当前写作的段落...')).toHaveValue('已保存草稿')
    expect(JSON.parse(localStorage.getItem('writing-desk-state')).drafts['1']).toBe('已保存草稿')

    await act(async () => resolveList({ data: { items: theses } }))
  })

  it('建议请求通过 JSON body 发送，切换论文会取消旧请求并隔离草稿', async () => {
    let resolveSuggest
    let requestSignal
    apiMocks.suggestCitations.mockImplementation((_id, _paragraph, config) =>
      new Promise((resolve, reject) => {
        resolveSuggest = resolve
        requestSignal = config.signal
        config.signal.addEventListener('abort', () => {
          const error = new Error('canceled')
          error.name = 'CanceledError'
          reject(error)
        })
      })
    )
    localStorage.setItem('writing-desk-state', JSON.stringify({
      selectedThesis: 1,
      drafts: { 1: '草稿 A', 2: '草稿 B' },
    }))
    render(<WritingDesk />)
    const editor = screen.getByPlaceholderText('在此输入当前写作的段落...')
    expect(editor).toHaveValue('草稿 A')

    fireEvent.click(screen.getByRole('button', { name: /获取引用建议/ }))
    await waitFor(() => expect(apiMocks.suggestCitations).toHaveBeenCalled())
    expect(apiMocks.suggestCitations.mock.calls[0][0]).toBe(1)
    expect(apiMocks.suggestCitations.mock.calls[0][1]).toBe('草稿 A')

    fireEvent.mouseDown(screen.getByRole('combobox'))
    fireEvent.click(await screen.findByText('论文二'))

    await waitFor(() => expect(screen.getByRole('button', { name: /获取引用建议/ })).toBeEnabled())
    expect(requestSignal.aborted).toBe(true)
    expect(editor).toHaveValue('草稿 B')
    expect(resolveSuggest).toBeTypeOf('function')
  })

  it('草稿延迟写入并在卸载时同步 flush', async () => {
    vi.useFakeTimers()
    localStorage.setItem('writing-desk-state', JSON.stringify({
      selectedThesis: 1,
      drafts: { 1: '旧草稿' },
    }))
    const { unmount } = render(<WritingDesk />)
    const editor = screen.getByPlaceholderText('在此输入当前写作的段落...')

    fireEvent.change(editor, { target: { value: '新草稿' } })
    expect(JSON.parse(localStorage.getItem('writing-desk-state')).drafts['1']).toBe('旧草稿')

    await act(async () => vi.advanceTimersByTime(399))
    expect(JSON.parse(localStorage.getItem('writing-desk-state')).drafts['1']).toBe('旧草稿')
    await act(async () => vi.advanceTimersByTime(1))
    expect(JSON.parse(localStorage.getItem('writing-desk-state')).drafts['1']).toBe('新草稿')

    fireEvent.change(editor, { target: { value: '卸载前最新草稿' } })

    unmount()
    expect(JSON.parse(localStorage.getItem('writing-desk-state')).drafts['1']).toBe('卸载前最新草稿')
    vi.useRealTimers()
  })

  it('卸载时取消进行中的建议请求', async () => {
    let requestSignal
    apiMocks.suggestCitations.mockImplementation((_id, _paragraph, config) => {
      requestSignal = config.signal
      return new Promise(() => {})
    })
    localStorage.setItem('writing-desk-state', JSON.stringify({
      selectedThesis: 1,
      drafts: { 1: '待检索草稿' },
    }))
    const { unmount } = render(<WritingDesk />)

    fireEvent.click(screen.getByRole('button', { name: /获取引用建议/ }))
    await waitFor(() => expect(requestSignal).toBeDefined())
    unmount()

    expect(requestSignal.aborted).toBe(true)
  })

  it('清空时取消进行中的建议请求，旧响应不得回填', async () => {
    apiMocks.suggestCitations.mockImplementation((_id, _paragraph, config) =>
      new Promise((resolve) => {
        config.signal.addEventListener('abort', () => resolve({
          data: { suggestions: '过期结果', citations: [] },
        }))
      })
    )
    localStorage.setItem('writing-desk-state', JSON.stringify({
      selectedThesis: 1,
      drafts: { 1: '待检索草稿' },
    }))
    render(<WritingDesk />)

    fireEvent.click(screen.getByRole('button', { name: /获取引用建议/ }))
    await waitFor(() => expect(apiMocks.suggestCitations).toHaveBeenCalled())
    fireEvent.click(screen.getByRole('button', { name: /清空/ }))

    await waitFor(() => expect(screen.queryByText('过期结果')).not.toBeInTheDocument())
    expect(screen.getByPlaceholderText('在此输入当前写作的段落...')).toHaveValue('')
  })
})
