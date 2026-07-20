import { useEffect, useState } from 'react'
import { Card, Row, Col, Statistic, Spin, Empty, Tag } from 'antd'
import ReactECharts from 'echarts-for-react'
import { getPaperStats } from '../api'
import { colors, componentStyles } from '../theme'

function StatsPage({ onSelectPaper }) {
  const [stats, setStats] = useState(null)
  const [loading, setLoading] = useState(false)

  useEffect(() => {
    setLoading(true)
    getPaperStats()
      .then((res) => setStats(res.data))
      .catch(() => {})
      .finally(() => setLoading(false))
  }, [])

  if (loading) return <Spin tip="加载统计中..." style={{ marginTop: 40 }} />
  if (!stats || stats.total === 0) {
    return <Empty description="暂无文献数据，请先导入 PDF" style={{ marginTop: 60 }} />
  }

  const yearOption = {
    title: { text: '文献年份分布', left: 'center', textStyle: { fontSize: 14 } },
    tooltip: { trigger: 'axis' },
    xAxis: { type: 'category', data: Object.keys(stats.by_year || {}) },
    yAxis: { type: 'value' },
    series: [
      {
        data: Object.values(stats.by_year || {}),
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
        data: Object.entries(stats.by_status || {}).map(([name, value]) => ({ name, value })),
      },
    ],
  }

  const tagOption = {
    title: { text: '标签分布 Top 20', left: 'center', textStyle: { fontSize: 14 } },
    tooltip: { trigger: 'axis' },
    xAxis: { type: 'category', data: Object.keys(stats.by_tag || {}) },
    yAxis: { type: 'value' },
    series: [
      {
        data: Object.values(stats.by_tag || {}),
        type: 'bar',
        itemStyle: { color: colors.success, borderRadius: [4, 4, 0, 0] },
      },
    ],
  }

  const graphData = stats.citation_graph || { nodes: [], links: [] }
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

  const statusMap = {
    unread: '未读',
    read: '已读',
    important: '重要',
    todo: '待精读',
  }

  return (
    <div>
      <Row gutter={[16, 16]} style={{ marginBottom: 16 }}>
        <Col xs={12} md={6}>
          <Card style={componentStyles.card}>
            <Statistic title="文献总数" value={stats.total} />
          </Card>
        </Col>
        <Col xs={12} md={6}>
          <Card style={componentStyles.card}>
            <Statistic
              title="已读"
              value={stats.by_status?.read || 0}
              suffix={`/ ${stats.total}`}
            />
          </Card>
        </Col>
        <Col xs={12} md={6}>
          <Card style={componentStyles.card}>
            <Statistic title="标签数" value={Object.keys(stats.by_tag || {}).length} />
          </Card>
        </Col>
        <Col xs={12} md={6}>
          <Card style={componentStyles.card}>
            <Statistic title="高频作者" value={stats.top_authors?.[0]?.name || '-'} />
          </Card>
        </Col>
      </Row>

      <Row gutter={[16, 16]}>
        <Col xs={24} md={12}>
          <Card style={componentStyles.card}>
            <ReactECharts option={yearOption} style={{ height: 300 }} />
          </Card>
        </Col>
        <Col xs={24} md={12}>
          <Card style={componentStyles.card}>
            <ReactECharts option={statusOption} style={{ height: 300 }} />
          </Card>
        </Col>
        <Col xs={24}>
          <Card style={componentStyles.card}>
            <ReactECharts option={tagOption} style={{ height: 320 }} />
          </Card>
        </Col>
        <Col xs={24}>
          <Card style={componentStyles.card}>
            <ReactECharts option={graphOption} style={{ height: 450 }} />
          </Card>
        </Col>
        <Col xs={24}>
          <Card title="高频作者" style={componentStyles.card}>
            <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8 }}>
              {(stats.top_authors || []).map((author) => (
                <Tag key={author.name} color="blue" style={{ borderRadius: 10, border: 'none' }}>
                  {author.name} ({author.count})
                </Tag>
              ))}
            </div>
          </Card>
        </Col>
      </Row>
    </div>
  )
}

export default StatsPage
