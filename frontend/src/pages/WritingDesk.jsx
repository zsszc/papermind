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
import { listThesis, getThesis, getChapterText, suggestCitations } from '../api'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { colors, componentStyles } from '../theme'
import { readWritingDeskState, writeWritingDeskState } from './writingDeskDraft'

const { TextArea } = Input
const { Title } = Typography
const { Option } = Select

function WritingDesk({ onSelectPaper }) {
  const initialStateRef = useRef(null)
  if (initialStateRef.current === null) initialStateRef.current = readWritingDeskState()
  const [thesisList, setThesisList] = useState([])
  const [selectedThesis, setSelectedThesis] = useState(initialStateRef.current.selectedThesis)
  const [drafts, setDrafts] = useState(initialStateRef.current.drafts)
  const [paragraph, setParagraph] = useState(
    initialStateRef.current.drafts[initialStateRef.current.selectedThesis] || ''
  )
  const [suggestions, setSuggestions] = useState('')
  const [citations, setCitations] = useState([])
  const [loading, setLoading] = useState(false)
  const [chapterTree, setChapterTree] = useState([])
  const [treeLoading, setTreeLoading] = useState(false)
  const abortCtrlRef = useRef(null)
  const suggestRequestRef = useRef(0)
  const thesisRequestRef = useRef(0)
  const chapterRequestRef = useRef(0)
  const selectedThesisRef = useRef(selectedThesis)
  const draftStateRef = useRef({ selectedThesis, drafts })

  selectedThesisRef.current = selectedThesis

  useEffect(() => {
    listThesis().then((res) => {
      const items = res.data.items || []
      setThesisList(items)

      if (
        initialStateRef.current.selectedThesis
        && !items.some((t) => t.id === initialStateRef.current.selectedThesis)
      ) {
        setSelectedThesis(null)
        setParagraph('')
      }
    }).catch(() => message.error('加载论文列表失败'))
  }, [])

  useEffect(() => {
    draftStateRef.current = { selectedThesis, drafts }
    const timer = setTimeout(() => {
      writeWritingDeskState(localStorage, draftStateRef.current)
    }, 400)
    return () => clearTimeout(timer)
  }, [selectedThesis, drafts])

  useEffect(() => () => {
    suggestRequestRef.current += 1
    abortCtrlRef.current?.abort()
    writeWritingDeskState(localStorage, draftStateRef.current)
  }, [])

  useEffect(() => {
    if (!selectedThesis) {
      setChapterTree([])
      return
    }
    const requestId = ++thesisRequestRef.current
    setTreeLoading(true)
    getThesis(selectedThesis)
      .then((res) => {
        if (requestId !== thesisRequestRef.current) return
        const chapters = res.data.chapter_structure || []
        const treeData = buildTreeData(chapters)
        setChapterTree(treeData)
      })
      .catch(() => {
        if (requestId === thesisRequestRef.current) message.error('加载章节结构失败')
      })
      .finally(() => {
        if (requestId === thesisRequestRef.current) setTreeLoading(false)
      })
    return () => {
      thesisRequestRef.current += 1
    }
  }, [selectedThesis])

  const handleSuggest = async () => {
    if (!selectedThesis || !paragraph.trim()) {
      message.warning('请选择论文并输入段落')
      return
    }
    abortCtrlRef.current?.abort()
    const requestId = suggestRequestRef.current + 1
    suggestRequestRef.current = requestId
    setLoading(true)
    setSuggestions('')
    setCitations([])
    abortCtrlRef.current = new AbortController()
    try {
      const response = await suggestCitations(selectedThesis, paragraph, {
        signal: abortCtrlRef.current.signal,
        skipGlobalError: true,
      })
      if (requestId !== suggestRequestRef.current) return
      const data = response.data
      setSuggestions(data.suggestions || '')
      setCitations(data.citations || [])
    } catch (err) {
      if (requestId !== suggestRequestRef.current) return
      if (err.name === 'AbortError' || err.name === 'CanceledError' || err.code === 'ERR_CANCELED') {
        setSuggestions('（已停止生成）')
      } else {
        message.error('引用建议失败')
      }
    } finally {
      if (requestId === suggestRequestRef.current) {
        setLoading(false)
        abortCtrlRef.current = null
      }
    }
  }

  const handleStopSuggest = () => {
    abortCtrlRef.current?.abort()
  }

  const cancelSuggest = () => {
    suggestRequestRef.current += 1
    abortCtrlRef.current?.abort()
    abortCtrlRef.current = null
    setLoading(false)
  }

  const handleThesisChange = (thesisId) => {
    cancelSuggest()
    chapterRequestRef.current += 1
    setSelectedThesis(thesisId)
    setParagraph(drafts[thesisId] || '')
    setSuggestions('')
    setCitations([])
  }

  const handleParagraphChange = (value) => {
    setParagraph(value)
    if (selectedThesis) {
      setDrafts((current) => ({ ...current, [selectedThesis]: value }))
    }
  }

  const handleSelectChapter = useCallback(
    (_, { node }) => {
      const chapterIndex = node?.chapterIndex
      if (chapterIndex == null || !selectedThesis) return
      const thesisId = selectedThesis
      const requestId = ++chapterRequestRef.current
      setTreeLoading(true)
      getChapterText(thesisId, chapterIndex)
        .then((res) => {
          if (
            requestId !== chapterRequestRef.current
            || selectedThesisRef.current !== thesisId
          ) return
          handleParagraphChange(res.data.text || '')
        })
        .catch(() => {
          if (requestId === chapterRequestRef.current) message.error('加载章节内容失败')
        })
        .finally(() => {
          if (requestId === chapterRequestRef.current) setTreeLoading(false)
        })
    },
    [selectedThesis, drafts]
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
              onChange={handleThesisChange}
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
              onChange={(e) => handleParagraphChange(e.target.value)}
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
                  cancelSuggest()
                  handleParagraphChange('')
                  setSuggestions('')
                  setCitations([])
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
