import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { collectA11yViolations, expectA11yContract } from '../test/a11yContract'

// Batch 24 / T2 可访问性契约基线：
// 对 ChatPanel 输入区、App 主导航、SettingsModal 锁定契约——
// 必填可访问名称、按钮可聚焦、Esc 关闭弹窗。新组件上架前必须过同一断言组。

const apiMocks = vi.hoisted(() => ({
  listConversations: vi.fn(),
  createConversation: vi.fn(),
  getHistory: vi.fn(),
  deleteConversation: vi.fn(),
  deleteMessagesFrom: vi.fn(),
  regenerateMessage: vi.fn(),
  analyzeImage: vi.fn(),
  importPapers: vi.fn(),
  getSettings: vi.fn(),
  updateSettings: vi.fn(),
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
  importPapers: apiMocks.importPapers,
  getSettings: apiMocks.getSettings,
  updateSettings: apiMocks.updateSettings,
}))

vi.mock('../utils/apiUrl', () => ({
  apiFetch: apiMocks.apiFetch,
}))

// App 的懒加载页面与契约无关，stub 掉避免触发各自的数据请求
vi.mock('../pages/PaperList', () => ({ default: () => <div data-testid="stub-page" /> }))
vi.mock('../pages/PaperDetail', () => ({ default: () => <div data-testid="stub-page" /> }))
vi.mock('../pages/SearchPage', () => ({ default: () => <div data-testid="stub-page" /> }))
vi.mock('../pages/ThesisList', () => ({ default: () => <div data-testid="stub-page" /> }))
vi.mock('../pages/ThesisDetail', () => ({ default: () => <div data-testid="stub-page" /> }))
vi.mock('../pages/WritingDesk', () => ({ default: () => <div data-testid="stub-page" /> }))
vi.mock('../pages/DataExport', () => ({ default: () => <div data-testid="stub-page" /> }))
vi.mock('../pages/StatsPage', () => ({ default: () => <div data-testid="stub-page" /> }))

import App from '../App'
import ChatPanel from './ChatPanel'
import SettingsModal from './SettingsModal'

describe('a11y 断言组自证（破坏性反例）', () => {
  afterEach(() => cleanup())

  it('缺可访问名称的图标按钮与仅 placeholder 的输入框必被判定违规', () => {
    const { container } = render(
      <div>
        {/* 故意破坏：无 aria-label、无可见文本的图标按钮 */}
        <button type="button">
          <svg aria-hidden="true" />
        </button>
        {/* 故意破坏：只有 placeholder，无显式标签关联 */}
        <textarea placeholder="输入问题" />
        {/* 对照组：合规写法不得误报 */}
        <button type="button" aria-label="合规按钮" />
        <input type="text" aria-label="合规输入" />
      </div>
    )
    const violations = collectA11yViolations(container)
    expect(violations.unnamedButtons).toHaveLength(1)
    expect(violations.unlabeledTextboxes).toHaveLength(1)
    expect(violations.unfocusableButtons).toHaveLength(0)
  })

  it('tabIndex=-1 的按钮必被判定不可聚焦', () => {
    const { container } = render(
      <div>
        {/* 故意破坏：移出 Tab 序的按钮 */}
        <button type="button" tabIndex={-1}>
          跳过
        </button>
        <button type="button">正常</button>
      </div>
    )
    expect(collectA11yViolations(container).unfocusableButtons).toHaveLength(1)
  })
})

describe('ChatPanel 输入区可访问性契约', () => {
  beforeEach(() => {
    vi.resetAllMocks()
    apiMocks.listConversations.mockResolvedValue({ data: [] })
    Element.prototype.scrollIntoView = vi.fn()
  })

  afterEach(() => cleanup())

  it('输入区所有按钮与输入框具备可访问名称且可聚焦', () => {
    const { container } = render(<ChatPanel fullHeight />)
    expectA11yContract(container)
    // 对话输入框必须有显式可访问名称（placeholder 不算）
    expect(screen.getByRole('textbox', { name: '对话输入框' })).toBeInTheDocument()
  })

  it('发送按钮有名称且可键盘聚焦', () => {
    render(<ChatPanel fullHeight />)
    const send = screen.getByRole('button', { name: /发送/ })
    send.focus()
    expect(send).toHaveFocus()
  })

  it('悬浮模式 FAB 与关闭按钮具备中文可访问名称', () => {
    render(<ChatPanel />)
    // antd 图标自带的英文 aria-label（message/close）不算合规中文名称
    const fab = screen.getByRole('button', { name: /打开对话面板/ })
    fireEvent.click(fab)
    expect(screen.getByRole('button', { name: /关闭对话面板/ })).toBeInTheDocument()
  })

  it('会话历史抽屉支持 Esc 关闭', async () => {
    render(<ChatPanel fullHeight />)
    fireEvent.click(screen.getByRole('button', { name: /历史/ }))
    const drawer = await screen.findByRole('dialog')
    fireEvent.keyDown(drawer, { key: 'Escape', keyCode: 27 })
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
  })
})

describe('App 主导航可访问性契约', () => {
  beforeEach(() => {
    vi.resetAllMocks()
    apiMocks.listConversations.mockResolvedValue({ data: [] })
    Element.prototype.scrollIntoView = vi.fn()
    localStorage.clear()
  })

  afterEach(() => cleanup())

  it('七个主导航菜单项均有可访问名称', () => {
    render(<App />)
    for (const label of ['文献', '检索', '论文', '写作', '统计', '对话', '导出']) {
      expect(screen.getByRole('menuitem', { name: new RegExp(label) })).toBeInTheDocument()
    }
  })

  it('侧栏折叠按钮具备中文可访问名称且可聚焦', () => {
    render(<App />)
    const collapse = screen.getByRole('button', { name: /侧边栏/ })
    collapse.focus()
    expect(collapse).toHaveFocus()
  })

  it('顶栏设置与导入按钮有名称；导航区整体零违规', () => {
    const { container } = render(<App />)
    expect(screen.getByRole('button', { name: /设置/ })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /导入 PDF/ })).toBeInTheDocument()
    expectA11yContract(container)
  })
})

describe('SettingsModal 可访问性契约', () => {
  beforeEach(() => {
    vi.resetAllMocks()
    apiMocks.getSettings.mockResolvedValue({
      data: { llm_api_key: 'sk-****', llm_model: 'kimi-k2.6', llm_base_url: 'https://api.moonshot.cn/v1' },
    })
  })

  afterEach(() => cleanup())

  it('表单字段均有显式标签关联，按钮可聚焦，整体零违规', async () => {
    render(<SettingsModal open onClose={() => {}} />)
    // 等待加载完成（loading Spin 消失后表单才渲染）
    expect(await screen.findByLabelText('Kimi API Key')).toBeInTheDocument()
    expect(screen.getByLabelText('模型')).toBeInTheDocument()
    expect(screen.getByLabelText('Base URL')).toBeInTheDocument()
    // antd 会给两个汉字的按钮文本自动插空格（保 存），用正则兼容
    const save = screen.getByRole('button', { name: /保\s*存/ })
    save.focus()
    expect(save).toHaveFocus()
    expect(screen.getByRole('button', { name: /取\s*消/ })).toBeInTheDocument()
    // Modal 渲染在 body portal，扫描整个 body（含关闭 X 按钮）
    expectA11yContract(document.body)
  })

  it('Esc 关闭弹窗', async () => {
    const onClose = vi.fn()
    render(<SettingsModal open onClose={onClose} />)
    const dialog = await screen.findByRole('dialog')
    fireEvent.keyDown(dialog, { key: 'Escape', keyCode: 27 })
    await waitFor(() => expect(onClose).toHaveBeenCalled())
  })
})
