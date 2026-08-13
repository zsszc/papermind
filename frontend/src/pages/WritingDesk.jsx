import { useState, useEffect, useCallback, useRef } from 'react'
import {
  Row,
  Col,
  Card,
  Input,
  Button,
  Select,
  message,
  Typography,
  Tree,
  Spin,
} from 'antd'
import { FileSearchOutlined, ClearOutlined, PauseCircleOutlined } from '@ant-design/icons'
import { listThesis, getThesis, getChapterText } from '../api'
import { apiFetch } from '../utils/apiUrl'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { colors, componentStyles } from '../theme'

const { TextArea } = Input
const { Title } = Typography
const { Option } = Select

const STORAGE_KEY = 'writing-desk-state'

function WritingDesk({ onSelectPaper }) {
  const [thesisList, setThesisList] = useState([])
  const [selectedThesis, setSelectedThesis] = useState(null)
  const [paragraph, setParagraph] = useState('')
  const [suggestions, setSuggestions] = useState('')
  const [citations, setCitations] = useState([])
  const [loading, setLoading] = useState(false)
  const [chapterTree, setChapterTree] = useState([])
  const [treeLoading, setTreeLoading] = useState(false)
  const abortCtrlRef = useRef(null)

  useEffect(() => {
    listThesis().then((res) => {
      const items = res.data.items || []
      setThesisList(items)

      try {
        const saved = JSON.parse(localStorage.getItem(STORAGE_KEY) || '{}')
        if (saved.selectedThesis && items.some((t) => t.id === saved.selectedThesis)) {
          setSelectedThesis(saved.selectedThesis)
          if (saved.paragraph) setParagraph(saved.paragraph)
        }
      } catch {
        // ignore
      }
    })
  }, [])

  useEffect(() => {
    localStorage.setItem(
      STORAGE_KEY,
      JSON.stringify({ selectedThesis, paragraph })
    )
  }, [selectedThesis, paragraph])

  useEffect(() => {
    if (!selectedThesis) {
      setChapterTree([])
      return
    }
    setTreeLoading(true)
    getThesis(selectedThesis)
      .then((res) => {
        const chapters = res.data.chapter_structure || []
        const treeData = buildTreeData(chapters)
        setChapterTree(treeData)
      })
      .finally(() => setTreeLoading(false))
  }, [selectedThesis])

  const handleSuggest = async () => {
    if (!selectedThesis || !paragraph.trim()) {
      message.warning('请选择论文并输入段落')
      return
    }
    setLoading(true)
    setSuggestions('')
    setCitations([])
    abortCtrlRef.current = new AbortController()
    try {
      const response = await apiFetch(
        `/api/thesis/${selectedThesis}/suggest-citations?paragraph=${encodeURIComponent(paragraph)}`,
        {
          method: 'POST',
          signal: abortCtrlRef.current.signal,
        }
      )
      if (!response.ok) {
        throw new Error(`HTTP ${response.status}`)
      }
      const data = await response.json()
      setSuggestions(data.suggestions || '')
      setCitations(data.citations || [])
    } catch (err) {
      if (err.name === 'AbortError') {
        setSuggestions('（已停止生成）')
      } else {
        message.error('引用建议失败')
      }
    } finally {
      setLoading(false)
      abortCtrlRef.current = null
    }
  }

  const handleStopSuggest = () => {
    abortCtrlRef.current?.abort()
  }

  const handleSelectChapter = useCallback(
    (_, { node }) => {
      const chapterIndex = node?.chapterIndex
      if (chapterIndex == null || !selectedThesis) return
      setTreeLoading(true)
      getChapterText(selectedThesis, chapterIndex)
        .then((res) => {
          setParagraph(res.data.text || '')
        })
        .catch(() => message.error('加载章节内容失败'))
        .finally(() => setTreeLoading(false))
    },
    [selectedThesis]
  )

  return (
    <div style={{ height: 'calc(100vh - 112px)' }}>
      <Title level={5} style={{ color: colors.textPrimary, marginBottom: 16 }}>
        写作工作台
      </Title>
      <Row gutter={[16, 16]} style={{ height: 'calc(100% - 40px)' }}>
        <Col xs={24} lg={12} style={{ height: '100%' }}>
          <Card
            title="段落编辑"
            style={{ ...componentStyles.card, height: '100%' }}
            bodyStyle={{ display: 'flex', flexDirection: 'column', height: 'calc(100% - 46px)', padding: '16px 20px' }}
          >
            <Select
              placeholder="选择论文"
              style={{ width: '100%', marginBottom: 12 }}
              onChange={setSelectedThesis}
              value={selectedThesis}
            >
              {thesisList.map((t) => (
                <Option key={t.id} value={t.id}>
                  {t.title || t.filename}
                </Option>
              ))}
            </Select>

            {selectedThesis && (
              <div style={{ marginBottom: 12 }}>
                <div style={{ marginBottom: 4, color: colors.textSecondary, fontSize: 13 }}>
                  点击章节可快速填充：
                </div>
                {treeLoading ? (
                  <Spin size="small" />
                ) : chapterTree.length > 0 ? (
                  <Tree
                    treeData={chapterTree}
                    onSelect={handleSelectChapter}
                    defaultExpandAll
                    height={140}
                    style={{ color: colors.textPrimary }}
                  />
                ) : (
                  <div style={{ color: colors.textTertiary }}>未识别到章节结构</div>
                )}
              </div>
            )}

            <TextArea
              value={paragraph}
              onChange={(e) => setParagraph(e.target.value)}
              placeholder="在此输入当前写作的段落..."
              style={{
                flex: 1,
                borderRadius: 12,
                border: `1px solid ${colors.border}`,
                background: colors.pageBg,
                resize: 'none',
                minHeight: 120,
              }}
            />
            <div style={{ marginTop: 12, display: 'flex', gap: 8 }}>
              <Button
                type="primary"
                icon={loading ? <PauseCircleOutlined /> : <FileSearchOutlined />}
                onClick={loading ? handleStopSuggest : handleSuggest}
                style={componentStyles.buttonPrimary}
              >
                {loading ? '停止生成' : '获取引用建议'}
              </Button>
              <Button
                icon={<ClearOutlined />}
                onClick={() => {
                  setParagraph('')
                  setSuggestions('')
                }}
                style={{ borderRadius: 20 }}
              >
                清空
              </Button>
            </div>
          </Card>
        </Col>
        <Col xs={24} lg={12} style={{ height: '100%' }}>
          <Card
            title="引用建议 / Agent 助手"
            style={{ ...componentStyles.card, height: '100%', overflow: 'auto' }}
            bodyStyle={{ padding: '16px 20px', height: 'calc(100% - 46px)', overflow: 'auto' }}
          >
            {suggestions ? (
              <ReactMarkdown remarkPlugins={[remarkGfm]} className="markdown-body">
                {suggestions}
              </ReactMarkdown>
            ) : (
              <div style={{ color: colors.textTertiary }}>
                输入段落后点击「获取引用建议」，或打开右下角 Agent 面板进行对话。
              </div>
            )}

            {citations.length > 0 && (
              <div style={{ marginTop: 20 }}>
                <div
                  style={{
                    fontSize: 14,
                    fontWeight: 600,
                    color: colors.textPrimary,
                    marginBottom: 10,
                  }}
                >
                  推荐文献
                </div>
                <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
                  {citations.map((item, idx) => (
                    <div
                      key={item.chunk_id || idx}
                      onClick={() => item.paper_id && onSelectPaper?.(item.paper_id)}
                      style={{
                        padding: '10px 12px',
                        borderRadius: 8,
                        background: colors.pageBg,
                        border: `1px solid ${colors.border}`,
                        cursor: item.paper_id && onSelectPaper ? 'pointer' : 'default',
                        transition: 'box-shadow 0.2s',
                      }}
                      onMouseEnter={(e) => {
                        if (item.paper_id && onSelectPaper) {
                          e.currentTarget.style.boxShadow = componentStyles.card.boxShadow
                        }
                      }}
                      onMouseLeave={(e) => {
                        e.currentTarget.style.boxShadow = 'none'
                      }}
                    >
                      <div
                        style={{
                          fontSize: 13,
                          fontWeight: 500,
                          color: colors.textPrimary,
                          marginBottom: 4,
                        }}
                      >
                        {item.title || '未知文献'}
                        {item.year ? `（${item.year}）` : ''}
                      </div>
                      <div
                        style={{
                          fontSize: 12,
                          color: colors.textSecondary,
                          lineHeight: 1.5,
                          display: '-webkit-box',
                          WebkitLineClamp: 2,
                          WebkitBoxOrient: 'vertical',
                          overflow: 'hidden',
                        }}
                      >
                        {item.content}
                      </div>
                    </div>
                  ))}
                </div>
              </div>
            )}
          </Card>
        </Col>
      </Row>
    </div>
  )
}

function buildTreeData(chapters) {
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

export default WritingDesk
