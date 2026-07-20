import { useEffect, useState, useCallback } from 'react'
import {
  Card,
  Button,
  Descriptions,
  Input,
  Select,
  Tag,
  Space,
  message,
  Modal,
  Form,
  Tooltip,
  Spin,
} from 'antd'
import {
  ArrowLeftOutlined,
  SaveOutlined,
  EditOutlined,
  ExclamationCircleOutlined,
} from '@ant-design/icons'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import {
  getPaper,
  updatePaper,
  getPaperNote,
  savePaperNote,
  getPaperSummary,
  listTags,
  addTag,
  removeTag,
} from '../api'
import PdfViewer from '../components/PdfViewer'
import ResizablePanels from '../components/ResizablePanels'
import { getApiBaseUrl } from '../utils/apiUrl'
import { colors, componentStyles } from '../theme'

const { TextArea } = Input
const { Option } = Select

const METADATA_FIELDS = [
  { key: 'title', label: '标题' },
  { key: 'authors', label: '作者' },
  { key: 'year', label: '年份', number: true },
  { key: 'journal', label: '期刊/会议' },
  { key: 'doi', label: 'DOI' },
]

function PaperDetail({ paperId, onBack, initialPage }) {
  const [paper, setPaper] = useState(null)
  const [note, setNote] = useState('')
  const [lastSavedNote, setLastSavedNote] = useState('')
  const [autoSaveStatus, setAutoSaveStatus] = useState('')
  const [summary, setSummary] = useState('')
  const [summaryLoading, setSummaryLoading] = useState(false)
  const [loading, setLoading] = useState(false)
  const [allTags, setAllTags] = useState([])
  const [editModalOpen, setEditModalOpen] = useState(false)
  const [editForm] = Form.useForm()

  const fetchPaper = useCallback(async () => {
    setLoading(true)
    try {
      const [paperRes, noteRes, tagsRes] = await Promise.all([
        getPaper(paperId),
        getPaperNote(paperId),
        listTags(),
      ])
      setPaper(paperRes.data)
      setNote(noteRes.data.content || '')
      setLastSavedNote(noteRes.data.content || '')
      setAllTags(tagsRes.data || [])
    } finally {
      setLoading(false)
    }
  }, [paperId])

  const fetchSummary = useCallback(async () => {
    setSummaryLoading(true)
    try {
      const res = await getPaperSummary(paperId)
      setSummary(res.data.summary || '')
    } catch (err) {
      // 尚未生成 AI 概括时忽略错误
      setSummary('')
    } finally {
      setSummaryLoading(false)
    }
  }, [paperId])

  useEffect(() => {
    fetchPaper()
    fetchSummary()
  }, [fetchPaper, fetchSummary])

  // 个人笔记自动保存：停止输入 1 秒后自动写入
  useEffect(() => {
    if (note === lastSavedNote) return
    setAutoSaveStatus('保存中...')
    const timer = setTimeout(() => {
      savePaperNote(paperId, note)
        .then(() => {
          setLastSavedNote(note)
          setAutoSaveStatus('已自动保存')
          setTimeout(() => setAutoSaveStatus(''), 2000)
        })
        .catch(() => {
          setAutoSaveStatus('自动保存失败')
        })
    }, 1000)
    return () => clearTimeout(timer)
  }, [note, lastSavedNote, paperId])

  const handleSaveNote = async () => {
    try {
      await savePaperNote(paperId, note)
      setLastSavedNote(note)
      message.success('笔记已保存')
    } catch (err) {
      // 全局拦截器已处理错误提示
    }
  }

  const handleStatusChange = async (value) => {
    await updatePaper(paperId, { status: value })
    setPaper((p) => ({ ...p, status: value }))
    message.success('状态已更新')
  }

  const handleTagChange = async (selectedNames) => {
    if (!paper) return
    const currentNames = new Set(paper.tags.map((t) => t.name))
    const nextNames = new Set(selectedNames)

    for (const name of selectedNames) {
      if (!currentNames.has(name)) {
        try {
          const res = await addTag(paperId, name)
          setPaper(res.data)
        } catch (err) {
          message.error(`添加标签 "${name}" 失败`)
        }
      }
    }

    for (const tag of paper.tags) {
      if (!nextNames.has(tag.name)) {
        try {
          const res = await removeTag(paperId, tag.id)
          setPaper(res.data)
        } catch (err) {
          message.error(`移除标签 "${tag.name}" 失败`)
        }
      }
    }
  }

  const handleOpenEditModal = () => {
    editForm.setFieldsValue({
      title: paper.title || '',
      authors: paper.authors || '',
      year: paper.year || undefined,
      journal: paper.journal || '',
      doi: paper.doi || '',
    })
    setEditModalOpen(true)
  }

  const handleSaveMetadata = async (values) => {
    const payload = {}
    for (const f of METADATA_FIELDS) {
      if (values[f.key] !== undefined && values[f.key] !== '') {
        payload[f.key] = f.number ? parseInt(values[f.key], 10) || null : values[f.key]
      } else {
        payload[f.key] = null
      }
    }
    try {
      const res = await updatePaper(paperId, payload)
      setPaper(res.data)
      message.success('元数据已更新')
      setEditModalOpen(false)
    } catch (err) {
      message.error('更新失败')
    }
  }

  const renderField = (key, label, value) => {
    const confidence = paper.metadata_json?.confidence?.[key]
    const lowConfidence = confidence !== undefined && confidence < 3
    return (
      <Descriptions.Item label={label}>
        <Space size={4}>
          <span>{value || '-'}</span>
          {lowConfidence && (
            <Tooltip title="该字段由 AI 自动识别，置信度较低，建议核对">
              <ExclamationCircleOutlined style={{ color: colors.warning }} />
            </Tooltip>
          )}
        </Space>
      </Descriptions.Item>
    )
  }

  if (!paper) return null

  const pdfUrl = `${getApiBaseUrl()}/api/papers/${paperId}/pdf`

  const tagOptions = allTags.map((t) => ({
    value: t.name,
    label: (
      <span>
        <Tag color={t.color} style={{ borderRadius: 12, border: 'none', marginRight: 4 }}>
          {t.name}
        </Tag>
      </span>
    ),
  }))

  return (
    <div>
      <Space style={{ marginBottom: 16 }}>
        <Button
          icon={<ArrowLeftOutlined />}
          onClick={onBack}
          style={{ borderRadius: 20 }}
        >
          返回看板
        </Button>
        <Button icon={<EditOutlined />} onClick={handleOpenEditModal}>
          编辑元数据
        </Button>
      </Space>

      <Card
        loading={loading}
        style={{ ...componentStyles.card, marginBottom: 16 }}
        bodyStyle={{ padding: '20px 24px' }}
      >
        <Descriptions
          title={
            <span style={{ fontSize: 18, fontWeight: 600, color: colors.textPrimary }}>
              {paper.title || paper.filename}
            </span>
          }
          column={{ xs: 1, sm: 2, md: 3 }}
          labelStyle={{ color: colors.textTertiary }}
          contentStyle={{ color: colors.textPrimary }}
        >
          {renderField('title', '作者', paper.authors)}
          {renderField('year', '年份', paper.year)}
          {renderField('journal', '期刊/会议', paper.journal)}
          {renderField('doi', 'DOI', paper.doi)}
          <Descriptions.Item label="页数">{paper.pages || '-'}</Descriptions.Item>
          <Descriptions.Item label="状态">
            <Select value={paper.status} style={{ width: 120 }} onChange={handleStatusChange}>
              <Option value="unread">未读</Option>
              <Option value="read">已读</Option>
              <Option value="important">重要</Option>
              <Option value="todo">待精读</Option>
            </Select>
          </Descriptions.Item>
          <Descriptions.Item label="标签" span={3}>
            <Select
              mode="tags"
              style={{ width: '100%', maxWidth: 600 }}
              placeholder="输入标签名并按回车添加"
              value={paper.tags.map((t) => t.name)}
              onChange={handleTagChange}
              options={tagOptions}
              tokenSeparators={[',']}
              tagRender={(props) => {
                const tag = paper.tags.find((t) => t.name === props.value)
                return (
                  <Tag
                    color={tag?.color || colors.primary}
                    style={{ borderRadius: 12, padding: '2px 10px', border: 'none' }}
                    closable={props.closable}
                    onClose={props.onClose}
                  >
                    {props.value}
                  </Tag>
                )
              }}
            />
          </Descriptions.Item>
        </Descriptions>
      </Card>

      <ResizablePanels
        storageKey="paperDetail"
        style={{ height: 'calc(100vh - 280px)' }}
        panels={[
          {
            key: 'pdf',
            title: 'PDF 预览',
            icon: 'pdf',
            defaultRatio: 0.38,
            content: (
              <div style={{ height: '100%', overflow: 'hidden' }}>
                <PdfViewer url={pdfUrl} paperId={paperId} initialPage={initialPage || paper?.last_read_page || 1} />
              </div>
            ),
          },
          {
            key: 'summary',
            title: 'AI 概括',
            icon: 'ai',
            defaultRatio: 0.31,
            content: (
              <div
                style={{
                  height: '100%',
                  overflow: 'auto',
                  padding: '16px 24px',
                }}
              >
                {summaryLoading ? (
                  <div style={{ padding: 40, textAlign: 'center' }}>
                    <Spin tip="加载 AI 概括..." />
                  </div>
                ) : summary ? (
                  <ReactMarkdown remarkPlugins={[remarkGfm]} className="markdown-body">
                    {summary}
                  </ReactMarkdown>
                ) : (
                  <div style={{ color: colors.textSecondary }}>
                    暂无 AI 概括。请在文献列表页点击“AI 概括”按钮生成。
                  </div>
                )}
              </div>
            ),
          },
          {
            key: 'note',
            title: '个人笔记',
            icon: 'note',
            defaultRatio: 0.31,
            content: (
              <div
                style={{
                  height: '100%',
                  overflow: 'auto',
                  padding: '16px 24px',
                }}
              >
                <TextArea
                  rows={14}
                  value={note}
                  onChange={(e) => setNote(e.target.value)}
                  placeholder="在此记录阅读笔记..."
                  style={{
                    borderRadius: 12,
                    border: `1px solid ${colors.border}`,
                    background: colors.pageBg,
                    resize: 'none',
                  }}
                />
                <div style={{ display: 'flex', alignItems: 'center', gap: 12, marginTop: 16 }}>
                  <Button
                    type="primary"
                    icon={<SaveOutlined />}
                    onClick={handleSaveNote}
                    style={{ ...componentStyles.buttonPrimary }}
                  >
                    保存笔记
                  </Button>
                  <span style={{ color: colors.textSecondary, fontSize: 13 }}>
                    {autoSaveStatus}
                  </span>
                </div>
              </div>
            ),
          },
        ]}
      />

      <Modal
        title="编辑元数据"
        open={editModalOpen}
        onCancel={() => setEditModalOpen(false)}
        onOk={() => editForm.submit()}
        okText="保存"
        cancelText="取消"
      >
        <Form form={editForm} layout="vertical" onFinish={handleSaveMetadata}>
          {METADATA_FIELDS.map((f) => (
            <Form.Item key={f.key} name={f.key} label={f.label}>
              {f.number ? <Input type="number" /> : <Input />}
            </Form.Item>
          ))}
        </Form>
      </Modal>
    </div>
  )
}

export default PaperDetail
