import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const apiMocks = vi.hoisted(() => ({
  getPaper: vi.fn(),
  getPaperNote: vi.fn(),
  listTags: vi.fn(),
  getPaperSummary: vi.fn(),
  savePaperNote: vi.fn(),
}))

vi.mock('../api', () => ({
  ...apiMocks,
  updatePaper: vi.fn(),
  addTag: vi.fn(),
  removeTag: vi.fn(),
}))
vi.mock('../components/PdfViewer', () => ({ default: () => <div>PDF</div> }))
vi.mock('../components/ResizablePanels', () => ({
  default: ({ panels }) => <>{panels.map((panel) => <div key={panel.key}>{panel.content}</div>)}</>,
}))

import PaperDetail from './PaperDetail'

describe('PaperDetail 笔记 flush', () => {
  afterEach(cleanup)

  beforeEach(() => {
    apiMocks.getPaper.mockResolvedValue({
      data: { id: 1, title: '论文', tags: [], metadata_json: {} },
    })
    apiMocks.getPaperNote.mockResolvedValue({ data: { content: '旧笔记' } })
    apiMocks.listTags.mockResolvedValue({ data: [] })
    apiMocks.getPaperSummary.mockRejectedValue(new Error('none'))
    apiMocks.savePaperNote.mockResolvedValue({ data: { status: 'ok' } })
  })

  it('不足自动保存等待时间点击返回也会先保存最新内容', async () => {
    const onBack = vi.fn()
    render(<PaperDetail paperId={1} onBack={onBack} />)
    const editor = await screen.findByPlaceholderText('在此记录阅读笔记...')

    fireEvent.change(editor, { target: { value: '离开前的最新笔记' } })
    fireEvent.click(screen.getByRole('button', { name: /返回看板/ }))

    await waitFor(() => {
      expect(apiMocks.savePaperNote).toHaveBeenCalledWith(1, '离开前的最新笔记')
      expect(onBack).toHaveBeenCalledOnce()
    })
  })

  it('最终保存失败时保留页面和重试状态', async () => {
    apiMocks.savePaperNote.mockRejectedValue(new Error('offline'))
    const onBack = vi.fn()
    render(<PaperDetail paperId={1} onBack={onBack} />)
    const editor = await screen.findByPlaceholderText('在此记录阅读笔记...')

    fireEvent.change(editor, { target: { value: '未保存内容' } })
    fireEvent.click(screen.getByRole('button', { name: /返回看板/ }))

    expect(await screen.findByText('保存失败，请重试')).toBeInTheDocument()
    expect(onBack).not.toHaveBeenCalled()
  })
})
