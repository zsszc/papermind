import { useCallback, useEffect, useRef, useState } from 'react'
import { Button, Card, Row, Col, Statistic, Spin, Empty, Tag } from 'antd'
import ReactEChartsCore from 'echarts-for-react/lib/core'
import { dispose, getInstanceByDom, init, use } from 'echarts/core'
import { BarChart, GraphChart, PieChart } from 'echarts/charts'
import { GridComponent, TitleComponent, TooltipComponent } from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'
import { getBenchmarkV2Readiness, getPaperStats } from '../api'
import { colors, componentStyles } from '../theme'

use([
  BarChart,
  GraphChart,
  PieChart,
  GridComponent,
  TitleComponent,
  TooltipComponent,
  CanvasRenderer,
])

// echarts-for-react 仅依赖这三个运行时方法；避免传入整个 namespace 阻碍 tree-shaking。
const echarts = { dispose, getInstanceByDom, init }

const readinessCountFields = [
  'physical_pdf_files',
  'unique_pdf_contents',
  'duplicate_pdf_files',
  'covered_unique_contents',
  'eligible_imported_papers',
  'unimported_unique_contents',
  'missing_new_papers',
]

const normalizeReadiness = (value) => {
  if (!value || !['PASS', 'WAIT'].includes(value.status)) return null
  if (value.minimum_new_papers !== 12) return null
  if (typeof value.ready !== 'boolean') return null
  if ((value.status === 'PASS') !== value.ready) return null
  if (readinessCountFields.some((field) => !Number.isInteger(value[field]) || value[field] < 0)) {
    return null
  }
  if (value.physical_pdf_files !== value.unique_pdf_contents + value.duplicate_pdf_files) return null
  if (value.covered_unique_contents > value.unique_pdf_contents) return null
  if (value.eligible_imported_papers > value.unique_pdf_contents) return null
  if (value.unimported_unique_contents > value.unique_pdf_contents) return null
  if (value.missing_new_papers !== Math.max(
    0,
    value.minimum_new_papers - value.eligible_imported_papers,
  )) return null
  if (value.ready !== (value.eligible_imported_papers >= value.minimum_new_papers)) return null
  return Object.fromEntries([
    ['status', value.status],
    ['ready', value.ready],
    ['minimum_new_papers', value.minimum_new_papers],
    ...readinessCountFields.map((field) => [field, value[field]]),
  ])
}

function BenchmarkReadinessCard({ state, onRetry }) {
  const isReady = state.phase === 'ready'
  const data = isReady ? state.data : null
  return (
    <Card title="Benchmark v2 就绪度" style={{ ...componentStyles.card, marginBottom: 16 }}>
      {state.phase === 'loading' && <Spin size="small" aria-label="正在检查就绪度" />}
      {state.phase === 'error' && (
        <div role="alert">
          <Tag color="red">不可用</Tag>
          <span>不可用，已按未就绪处理</span>
          <div style={{ marginTop: 12 }}>合格新论文—</div>
          <Button size="small" onClick={onRetry} style={{ marginTop: 12 }}>
            重新检查就绪度
          </Button>
        </div>
      )}
      {isReady && (
        <div role="status" aria-live="polite">
          <div style={{ display: 'flex', alignItems: 'center', gap: 12, flexWrap: 'wrap' }}>
            <Tag color={data.ready ? 'green' : 'gold'}>{data.status}</Tag>
            <strong>{data.eligible_imported_papers} / {data.minimum_new_papers}</strong>
            {!data.ready && <span>尚缺 {data.missing_new_papers} 篇真正不同的新论文</span>}
          </div>
          <Row gutter={[12, 8]} style={{ marginTop: 12 }}>
            <Col xs={12} md={4}>物理 PDF<strong>{data.physical_pdf_files}</strong></Col>
            <Col xs={12} md={4}>唯一内容<strong>{data.unique_pdf_contents}</strong></Col>
            <Col xs={12} md={4}>重复副本<strong>{data.duplicate_pdf_files}</strong></Col>
            <Col xs={12} md={4}>v1 已覆盖<strong>{data.covered_unique_contents}</strong></Col>
            <Col xs={12} md={4}>合格新论文<strong>{data.eligible_imported_papers}</strong></Col>
            <Col xs={12} md={4}>未导入内容<strong>{data.unimported_unique_contents}</strong></Col>
          </Row>
          <div style={{ marginTop: 12, color: colors.textSecondary }}>
            {data.ready
              ? '已达到门槛，可进入 QA 冻结流程。'
              : '重复副本不计入进度；请导入真正不同的新论文。'}
          </div>
        </div>
      )}
    </Card>
  )
}

function StatsPage({ onSelectPaper }) {
  const [stats, setStats] = useState(null)
  const [loading, setLoading] = useState(false)
  const [readinessState, setReadinessState] = useState({ phase: 'loading', data: null })
  const readinessRequestId = useRef(0)

  const loadReadiness = useCallback(() => {
    const requestId = ++readinessRequestId.current
    setReadinessState({ phase: 'loading', data: null })
    getBenchmarkV2Readiness()
      .then((res) => {
        if (requestId !== readinessRequestId.current) return
        const normalized = normalizeReadiness(res.data)
        setReadinessState(normalized
          ? { phase: 'ready', data: normalized }
          : { phase: 'error', data: null })
      })
      .catch(() => {
        if (requestId === readinessRequestId.current) {
          setReadinessState({ phase: 'error', data: null })
        }
      })
  }, [])

  useEffect(() => {
    let active = true
    setLoading(true)
    getPaperStats()
      .then((res) => {
        if (active) setStats(res.data)
      })
      .catch(() => {})
      .finally(() => {
        if (active) setLoading(false)
      })
    loadReadiness()
    return () => {
      active = false
      readinessRequestId.current += 1
    }
  }, [loadReadiness])

  const libraryStats = stats || {}

  const yearOption = {
    title: { text: '文献年份分布', left: 'center', textStyle: { fontSize: 14 } },
    tooltip: { trigger: 'axis' },
    xAxis: { type: 'category', data: Object.keys(libraryStats.by_year || {}) },
    yAxis: { type: 'value' },
    series: [
      {
        data: Object.values(libraryStats.by_year || {}),
        type: 'bar',
        itemStyle: { color: colors.primary, borderRadius: [4, 4, 0, 0] },
      },
    ],
  }

  const statusOption = {
    title: { text: '阅读状态分布', left: 'center', textStyle: { fontSize: 14 } },
    tooltip: { trigger: 'item' },
    series: [
      {
        type: 'pie',
        radius: '60%',
        data: Object.entries(libraryStats.by_status || {}).map(([name, value]) => ({ name, value })),
      },
    ],
  }

  const tagOption = {
    title: { text: '标签分布 Top 20', left: 'center', textStyle: { fontSize: 14 } },
    tooltip: { trigger: 'axis' },
    xAxis: { type: 'category', data: Object.keys(libraryStats.by_tag || {}) },
    yAxis: { type: 'value' },
    series: [
      {
        data: Object.values(libraryStats.by_tag || {}),
        type: 'bar',
        itemStyle: { color: colors.success, borderRadius: [4, 4, 0, 0] },
      },
    ],
  }

  const graphData = libraryStats.citation_graph || { nodes: [], links: [] }
  const graphOption = {
    title: { text: '章节-文献引用关系图', left: 'center', textStyle: { fontSize: 14 } },
    tooltip: {
      trigger: 'item',
      formatter: (params) => params.data.name,
    },
    series: [
      {
        type: 'graph',
        layout: 'force',
        roam: true,
        animation: false,
        label: { show: true, position: 'right', fontSize: 10 },
        draggable: true,
        data: graphData.nodes.map((n) => ({
          ...n,
          symbolSize: n.type === 'paper' ? 16 : 20,
          itemStyle: { color: n.type === 'paper' ? colors.primary : colors.warning },
        })),
        links: graphData.links,
        force: { repulsion: 200, edgeLength: 80 },
        lineStyle: { curveness: 0.1, opacity: 0.6 },
      },
    ],
  }

  return (
    <div>
      <BenchmarkReadinessCard state={readinessState} onRetry={loadReadiness} />

      {loading && <Spin aria-label="加载统计中" style={{ marginTop: 40 }} />}
      {!loading && (!stats || stats.total === 0) && (
        <Empty description="暂无文献数据，请先导入 PDF" style={{ marginTop: 60 }} />
      )}
      {!loading && stats && stats.total > 0 && (
        <>
          <Row gutter={[16, 16]} style={{ marginBottom: 16 }}>
        <Col xs={12} md={6}>
          <Card style={componentStyles.card}>
            <Statistic title="文献总数" value={libraryStats.total} />
          </Card>
        </Col>
        <Col xs={12} md={6}>
          <Card style={componentStyles.card}>
            <Statistic
              title="已读"
              value={libraryStats.by_status?.read || 0}
              suffix={`/ ${libraryStats.total}`}
            />
          </Card>
        </Col>
        <Col xs={12} md={6}>
          <Card style={componentStyles.card}>
            <Statistic title="标签数" value={Object.keys(libraryStats.by_tag || {}).length} />
          </Card>
        </Col>
        <Col xs={12} md={6}>
          <Card style={componentStyles.card}>
            <Statistic title="高频作者" value={libraryStats.top_authors?.[0]?.name || '-'} />
          </Card>
        </Col>
          </Row>

          <Row gutter={[16, 16]}>
        <Col xs={24} md={12}>
          <Card style={componentStyles.card}>
            <ReactEChartsCore echarts={echarts} option={yearOption} style={{ height: 300 }} />
          </Card>
        </Col>
        <Col xs={24} md={12}>
          <Card style={componentStyles.card}>
            <ReactEChartsCore echarts={echarts} option={statusOption} style={{ height: 300 }} />
          </Card>
        </Col>
        <Col xs={24}>
          <Card style={componentStyles.card}>
            <ReactEChartsCore echarts={echarts} option={tagOption} style={{ height: 320 }} />
          </Card>
        </Col>
        <Col xs={24}>
          <Card style={componentStyles.card}>
            <ReactEChartsCore echarts={echarts} option={graphOption} style={{ height: 450 }} />
          </Card>
        </Col>
        <Col xs={24}>
          <Card title="高频作者" style={componentStyles.card}>
            <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8 }}>
              {(libraryStats.top_authors || []).map((author) => (
                <Tag key={author.name} color="blue" style={{ borderRadius: 10, border: 'none' }}>
                  {author.name} ({author.count})
                </Tag>
              ))}
            </div>
          </Card>
        </Col>
          </Row>
        </>
      )}
    </div>
  )
}

export default StatsPage
