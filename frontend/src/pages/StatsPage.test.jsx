import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const apiMocks = vi.hoisted(() => ({
  getPaperStats: vi.fn(),
  getBenchmarkV2Readiness: vi.fn(),
}))

vi.mock('../api', () => apiMocks)
vi.mock('echarts-for-react/lib/core', () => ({ default: () => <div data-testid="chart" /> }))

import StatsPage from './StatsPage'

const waiting = {
  status: 'WAIT',
  ready: false,
  minimum_new_papers: 12,
  missing_new_papers: 11,
  physical_pdf_files: 36,
  unique_pdf_contents: 19,
  duplicate_pdf_files: 17,
  covered_unique_contents: 18,
  eligible_imported_papers: 1,
  unimported_unique_contents: 0,
  error_code: null,
}

describe('StatsPage Benchmark v2 就绪度', () => {
  beforeEach(() => {
    apiMocks.getPaperStats.mockResolvedValue({
      data: {
        total: 1,
        by_year: {},
        by_status: {},
        by_tag: {},
        top_authors: [],
        citation_graph: { nodes: [], links: [] },
      },
    })
    apiMocks.getBenchmarkV2Readiness.mockResolvedValue({ data: waiting })
  })

  afterEach(cleanup)

  it('WAIT 展示真实聚合计数与补库缺口', async () => {
    render(<StatsPage />)

    const status = await screen.findByRole('status')
    expect(status).toHaveTextContent('WAIT')
    expect(status).toHaveTextContent('1 / 12')
    expect(status).toHaveTextContent('尚缺 11 篇')
    expect(status).toHaveTextContent('物理 PDF36')
    expect(status).toHaveTextContent('唯一内容19')
    expect(status).toHaveTextContent('重复副本17')
    expect(status).toHaveTextContent('v1 已覆盖18')
    expect(status).toHaveTextContent('重复副本不计入进度')
  })

  it('PASS 明确表示可进入 QA 冻结且不再提示缺口', async () => {
    apiMocks.getBenchmarkV2Readiness.mockResolvedValue({
      data: {
        ...waiting,
        status: 'PASS',
        ready: true,
        eligible_imported_papers: 12,
        missing_new_papers: 0,
      },
    })

    render(<StatsPage />)

    const status = await screen.findByRole('status')
    expect(status).toHaveTextContent('PASS')
    expect(status).toHaveTextContent('可进入 QA 冻结流程')
    expect(status).not.toHaveTextContent('尚缺')
  })

  it('请求失败按不可用处理，未知计数不冒充 0 且可独立重试', async () => {
    apiMocks.getBenchmarkV2Readiness
      .mockRejectedValueOnce(new Error('offline'))
      .mockResolvedValueOnce({ data: waiting })

    render(<StatsPage />)

    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('不可用，已按未就绪处理')
    expect(alert).toHaveTextContent('合格新论文—')
    expect(alert).not.toHaveTextContent('合格新论文0')

    fireEvent.click(screen.getByRole('button', { name: '重新检查就绪度' }))
    await waitFor(() => expect(screen.getByRole('status')).toHaveTextContent('1 / 12'))
    expect(apiMocks.getBenchmarkV2Readiness).toHaveBeenCalledTimes(2)
    expect(apiMocks.getPaperStats).toHaveBeenCalledTimes(1)
  })

  it('文献库为空仍展示就绪卡片，且忽略响应中夹带的身份字段', async () => {
    apiMocks.getPaperStats.mockResolvedValue({ data: { total: 0 } })
    apiMocks.getBenchmarkV2Readiness.mockResolvedValue({
      data: {
        ...waiting,
        paper_uid: 'doi:10.1/secret',
        pdf_sha256: 'f'.repeat(64),
        path: '/private/secret.pdf',
        title: '秘密论文标题',
      },
    })

    render(<StatsPage />)

    expect(await screen.findByRole('status')).toHaveTextContent('WAIT')
    expect(screen.getByText('暂无文献数据，请先导入 PDF')).toBeInTheDocument()
    expect(screen.queryByText('doi:10.1/secret')).not.toBeInTheDocument()
    expect(screen.queryByText('/private/secret.pdf')).not.toBeInTheDocument()
    expect(screen.queryByText('秘密论文标题')).not.toBeInTheDocument()
  })

  it('矛盾的 PASS 响应失败关闭，不由 UI 自行放行', async () => {
    apiMocks.getBenchmarkV2Readiness.mockResolvedValue({
      data: { ...waiting, status: 'PASS', ready: true },
    })

    render(<StatsPage />)

    expect(await screen.findByRole('alert')).toHaveTextContent('不可用，已按未就绪处理')
    expect(screen.queryByText('可进入 QA 冻结流程')).not.toBeInTheDocument()
  })
})
