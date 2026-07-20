import { useEffect, useState } from 'react'
import {
  Card,
  Typography,
  Button,
  List,
  Tag,
  Space,
  Select,
  message,
  Spin,
  Tree,
  Tooltip,
  Empty,
} from 'antd'
import { ArrowLeftOutlined, FileSearchOutlined } from '@ant-design/icons'
import {
  getThesis,
  getThesisCitations,
  getThesisCitationMap,
  analyzeThesis,
  updateThesisCitation,
  listPapers,
} from '../api'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { colors, componentStyles } from '../theme'

const { Title, Text } = Typography

function ThesisDetail({ thesisId, onBack, onSelectPaper }) {
  const [thesis, setThesis] = useState(null)
  const [citations, setCitations] = useState([])
  const [citationMap, setCitationMap] = useState(null)
  const [papers, setPapers] = useState([])
  const [selectedChapter, setSelectedChapter] = useState(null)
  const [suggestions, setSuggestions] = useState('')
  const [loading, setLoading] = useState(false)

  useEffect(() => {
    fetchData()
  }, [thesisId])

  const fetchData = async () => {
    setLoading(true)
    try {
      const [thesisRes, citationRes, mapRes, papersRes] = await Promise.allSettled([
        getThesis(thesisId),
        getThesisCitations(thesisId),
        getThesisCitationMap(thesisId),
        listPapers({ limit: 1000 }),
      ])
      if (thesisRes.status === 'fulfilled') {
        setThesis(thesisRes.value.data)
      } else {
        message.error('加载论文详情失败：' + (thesisRes.reason?.message || '未知错误'))
      }
      setCitations(citationRes.status === 'fulfilled' ? citationRes.value.data || [] : [])
      setCitationMap(mapRes.status === 'fulfilled' ? mapRes.value.data || null : null)
      setPapers(papersRes.status === 'fulfilled' ? papersRes.value.data?.items || [] : [])
    } catch (err) {
      message.error('加载数据失败：' + (err.message || '未知错误'))
    } finally {
      setLoading(false)
    }
  }

  const handleLinkCitation = async (citationId, paperId) => {
    try {
      await updateThesisCitation(thesisId, citationId, { paper_id: paperId })
      message.success('关联已更新')
      setCitations((prev) =>
        prev.map((c) => (c.id === citationId ? { ...c, paper_id: paperId } : c))
      )
      // 刷新映射视图
      const mapRes = await getThesisCitationMap(thesisId)
      setCitationMap(mapRes.data || null)
    } catch (err) {
      message.error('更新关联失败')
    }
  }

  const handleAnalyze = async (chapterIndex) => {
    setLoading(true)
    setSelectedChapter(chapterIndex)
    try {
      const res = await analyzeThesis(thesisId, { chapter_index: chapterIndex })
      setSuggestions(res.data.suggestions)
    } catch (err) {
      const detail = err.response?.data?.detail || err.message || '未知错误'
      message.error('分析失败：' + detail)
    } finally {
      setLoading(false)
    }
  }

  const chapterTreeData = buildChapterTree(thesis?.chapter_structure || [])

  if (!thesis) {
    return (
      <div style={{ padding: 40, textAlign: 'center' }}>
        {loading ? <Spin tip="加载大论文详情..." /> : <Empty description="未找到论文详情" />}
      </div>
    )
  }

  return (
    <div>
      <Space style={{ marginBottom: 16 }}>
        <Button
          icon={<ArrowLeftOutlined />}
          onClick={onBack}
          style={{ borderRadius: 20 }}
        >
          返回
        </Button>
      </Space>

      <Title level={5} style={{ color: colors.textPrimary, marginBottom: 4 }}>
        {thesis.title || thesis.filename}
      </Title>
      <Text style={{ color: colors.textSecondary }}>
        字数：{thesis.word_count || '-'} | 章节数：{thesis.chapter_structure?.length || 0}
      </Text>

      <Card
        title="章节结构"
        style={{ ...componentStyles.card, marginTop: 20 }}
        bodyStyle={{ padding: '16px 20px' }}
      >
        {chapterTreeData.length > 0 ? (
          <Tree
            treeData={chapterTreeData}
            defaultExpandAll
            titleRender={(node) => (
              <Space>
                <span style={{ color: colors.textPrimary }}>{node.title}</span>
                <Tooltip title="AI 评审">
                  <Button
                    type="text"
                    size="small"
                    icon={<FileSearchOutlined />}
                    onClick={() => handleAnalyze(node.chapterIndex)}
                    loading={loading && selectedChapter === node.chapterIndex}
                    style={{ color: colors.primary }}
                  />
                </Tooltip>
              </Space>
            )}
          />
        ) : (
          <Text type="secondary">未识别到章节结构</Text>
        )}
      </Card>

      <Card
        title="章节-文献映射"
        style={{ ...componentStyles.card, marginTop: 16 }}
        bodyStyle={{ padding: '16px 20px' }}
      >
        {(!citationMap?.chapters || citationMap.chapters.length === 0) ? (
          <Text type="secondary">未检测到章节引用数据</Text>
        ) : (
          <div>
            <div style={{ marginBottom: 12, color: colors.textSecondary, fontSize: 13 }}>
              总引用标记：{citationMap?.total_citations || 0}，已关联文献：{citationMap?.matched_citations || 0}
            </div>
            <List
              size="small"
              dataSource={citationMap.chapters}
              renderItem={(ch) => (
                <List.Item>
                  <div style={{ width: '100%' }}>
                    <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                      <Text strong style={{ color: colors.textPrimary }}>
                        {ch.chapter_title || `第${(ch.chapter_index ?? 0) + 1}章`}
                      </Text>
                      <Tag color={(ch.paper_ids?.length || 0) > 0 ? 'success' : 'default'} style={{ borderRadius: 10, border: 'none' }}>
                        {ch.citation_count || 0} 处引用
                      </Tag>
                    </div>
                    <div style={{ marginTop: 8, display: 'flex', flexWrap: 'wrap', gap: 8 }}>
                      {(ch.paper_ids?.length || 0) === 0 ? (
                        <Text type="secondary" style={{ fontSize: 12 }}>未关联文献（引用盲区）</Text>
                      ) : (
                        (ch.paper_ids || []).map((pid) => (
                          <Tag
                            key={pid}
                            color="blue"
                            style={{ borderRadius: 10, border: 'none', cursor: onSelectPaper ? 'pointer' : 'default' }}
                            onClick={() => onSelectPaper?.(pid)}
                          >
                            {ch.paper_titles?.[pid] || `文献 #${pid}`}
                          </Tag>
                        ))
                      )}
                    </div>
                  </div>
                </List.Item>
              )}
            />
          </div>
        )}
      </Card>

      <Card
        title="引用检测与关联"
        style={{ ...componentStyles.card, marginTop: 16 }}
        bodyStyle={{ padding: '16px 20px' }}
      >
        {citations.length === 0 ? (
          <Text type="secondary">未检测到引用标记</Text>
        ) : (
          <List
            size="small"
            dataSource={citations}
            renderItem={(item) => (
              <List.Item>
                <div style={{ width: '100%' }}>
                  <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: 12 }}>
                    <Tag
                      color={item.paper_id ? colors.success : 'default'}
                      style={{ borderRadius: 10, border: 'none', flexShrink: 0 }}
                    >
                      {item.citation_text || `引用 #${item.id}`}
                    </Tag>
                    <Select
                      showSearch
                      allowClear
                      placeholder="关联文献"
                      style={{ minWidth: 220, flex: 1 }}
                      value={item.paper_id || undefined}
                      onChange={(value) => handleLinkCitation(item.id, value)}
                      filterOption={(input, option) =>
                        (option?.label || '').toLowerCase().includes(input.toLowerCase())
                      }
                      options={papers.map((p) => ({
                        value: p.id,
                        label: p.title || p.filename || `文献 #${p.id}`,
                      }))}
                    />
                  </div>
                  {item.context && (
                    <Text type="secondary" style={{ fontSize: 12, marginTop: 6, display: 'block' }}>
                      上下文：{item.context}
                    </Text>
                  )}
                </div>
              </List.Item>
            )}
          />
        )}
      </Card>

      {loading && <Spin style={{ marginTop: 24 }} tip="正在生成评审意见..." />}
      {suggestions && !loading && (
        <Card
          title="AI 评审意见"
          style={{ ...componentStyles.card, marginTop: 16 }}
          bodyStyle={{ padding: '16px 20px' }}
        >
          <ReactMarkdown remarkPlugins={[remarkGfm]} className="markdown-body">
            {suggestions}
          </ReactMarkdown>
        </Card>
      )}
    </div>
  )
}

function buildChapterTree(chapters) {
  if (!chapters || chapters.length === 0) return []

  const roots = []
  const stack = []

  chapters.forEach((ch, idx) => {
    const node = {
      title: ch.title,
      key: `${ch.title}-${idx}`,
      chapterIndex: idx,
      level: ch.level,
      children: [],
    }

    while (stack.length > 0 && stack[stack.length - 1].level >= ch.level) {
      stack.pop()
    }

    if (stack.length === 0) {
      roots.push(node)
    } else {
      stack[stack.length - 1].children.push(node)
    }
    stack.push(node)
  })

  return roots
}

export default ThesisDetail
