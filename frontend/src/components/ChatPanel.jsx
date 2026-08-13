import { useEffect, useRef, useState, useCallback, memo } from 'react'
import {
  Card,
  Input,
  Button,
  List,
  Space,
  Typography,
  Tag,
  message,
  Spin,
  Drawer,
  Popconfirm,
  Empty,
  Tooltip,
} from 'antd'
import {
  MessageOutlined,
  CloseOutlined,
  SendOutlined,
  PlusOutlined,
  HistoryOutlined,
  DeleteOutlined,
  EditOutlined,
  ReloadOutlined,
  PauseCircleOutlined,
  GlobalOutlined,
  TranslationOutlined,
  CheckCircleOutlined,
  DiffOutlined,
  ProfileOutlined,
  BarChartOutlined,
  PictureOutlined,
} from '@ant-design/icons'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import {
  listConversations,
  createConversation,
  getHistory,
  deleteConversation,
  deleteMessagesFrom,
  regenerateMessage,
  analyzeImage,
} from '../api'
import { apiFetch } from '../utils/apiUrl'
import { readSSEStream } from '../utils/sse'
import {
  beginChatOperation,
  finishChatOperation,
  updateMessageByIdentity,
} from '../utils/chatOperation'
import { colors, componentStyles } from '../theme'
// ResizableVertical 不再用于聊天面板，改为消息区滚动 + 底部固定输入

const { TextArea } = Input
const { Text } = Typography

function generateTempId() {
  return `${Date.now()}-${Math.random().toString(36).substr(2, 9)}`
}

const MessageBubble = memo(function MessageBubble({
  role,
  content,
  image,
  isInterrupted,
  onEdit,
  onRegenerate,
  loading,
}) {
  const isUser = role === 'user'
  return (
    <div
      style={{
        alignSelf: isUser ? 'flex-end' : 'flex-start',
        maxWidth: '80%',
        display: 'flex',
        flexDirection: 'column',
        gap: 4,
      }}
    >
      <div
        style={{
          padding: '12px 16px',
          borderRadius: isUser ? '18px 18px 4px 18px' : '18px 18px 18px 4px',
          background: isUser ? colors.primary : '#f0f2f5',
          color: isUser ? '#fff' : colors.textPrimary,
          boxShadow: '0 2px 8px rgba(0, 0, 0, 0.04)',
          lineHeight: 1.6,
        }}
      >
        {image && (
          <img
            src={image}
            alt="upload"
            style={{
              maxWidth: '100%',
              maxHeight: 160,
              borderRadius: 8,
              marginBottom: 8,
              objectFit: 'cover',
            }}
          />
        )}
        {role === 'assistant' ? (
          <ReactMarkdown remarkPlugins={[remarkGfm]} className="markdown-body">
            {content || '...'}
          </ReactMarkdown>
        ) : (
          <Text style={{ color: '#fff' }}>{content}</Text>
        )}
        {isInterrupted && (
          <div style={{ marginTop: 4, fontSize: 12, opacity: 0.7 }}>（已停止）</div>
        )}
      </div>
      <div style={{ alignSelf: isUser ? 'flex-end' : 'flex-start' }}>
        {isUser ? (
          <Tooltip title="编辑">
            <Button
              type="text"
              size="small"
              icon={<EditOutlined />}
              onClick={onEdit}
              style={{ color: colors.textTertiary, padding: '0 4px' }}
            />
          </Tooltip>
        ) : (
          <Tooltip title="重新生成">
            <Button
              type="text"
              size="small"
              icon={loading ? <Spin size="small" /> : <ReloadOutlined />}
              onClick={onRegenerate}
              disabled={loading}
              style={{ color: colors.textTertiary, padding: '0 4px' }}
            />
          </Tooltip>
        )}
      </div>
    </div>
  )
})

function SkillButton({ active, icon, label, onClick }) {
  return (
    <Button
      size="small"
      type={active ? 'primary' : 'default'}
      icon={icon}
      onClick={onClick}
      style={{ borderRadius: 12 }}
    >
      {label}
    </Button>
  )
}

function ChatPanel({ fullHeight = false, onSelectPaper }) {
  const [visible, setVisible] = useState(false)
  const [drawerOpen, setDrawerOpen] = useState(false)
  const [conversations, setConversations] = useState([])
  const [currentId, setCurrentId] = useState(null)
  const [messages, setMessages] = useState([])
  const [input, setInput] = useState('')
  const [loading, setLoading] = useState(false)
  const [citations, setCitations] = useState([])
  const [regeneratingMsgId, setRegeneratingMsgId] = useState(null)
  const [enableWebSearch, setEnableWebSearch] = useState(false)
  const [activeSkill, setActiveSkill] = useState(null)
  const [selectedImage, setSelectedImage] = useState(null)
  const [imagePreview, setImagePreview] = useState(null)
  const fileInputRef = useRef(null)
  const messagesEndRef = useRef(null)
  const abortCtrlRef = useRef(null)

  const fetchConversations = useCallback(async () => {
    try {
      const res = await listConversations()
      setConversations(res.data || [])
    } catch (err) {
      console.error(err)
    }
  }, [])

  useEffect(() => {
    fetchConversations()
  }, [fetchConversations])

  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages])

  // 浮窗“关闭”只是 display:none，组件仍存活；视图切换才会真正卸载。
  // 卸载后必须取消请求，避免无界后台 SSE 与对已卸载实例的状态更新。
  useEffect(() => {
    return () => {
      abortCtrlRef.current?.abort()
    }
  }, [])

  const loadHistory = useCallback(async (id) => {
    abortCtrlRef.current?.abort()
    try {
      const res = await getHistory(id)
      setCurrentId(id)
      setMessages(res.data.messages || [])
      setCitations([])
    } catch (err) {
      message.error('加载对话历史失败')
    }
  }, [])

  const handleNewConversation = useCallback(async ({ cancelActive = true } = {}) => {
    if (cancelActive) abortCtrlRef.current?.abort()
    try {
      const res = await createConversation()
      const newConv = res.data
      setConversations((prev) => [newConv, ...prev])
      setCurrentId(newConv.id)
      setMessages([])
      setCitations([])
      setDrawerOpen(false)
      return newConv.id
    } catch (err) {
      message.error('创建会话失败')
      return null
    }
  }, [])

  const handleDeleteConversation = useCallback(
    async (e, id) => {
      e?.stopPropagation()
      if (currentId === id) abortCtrlRef.current?.abort()
      try {
        await deleteConversation(id)
        message.success('会话已删除')
        setConversations((prev) => prev.filter((c) => c.id !== id))
        if (currentId === id) {
          setCurrentId(null)
          setMessages([])
          setCitations([])
        }
      } catch (err) {
        message.error('删除会话失败')
      }
    },
    [currentId]
  )

  const handleSend = useCallback(async () => {
    if (!input.trim() && !selectedImage) return

    const controller = new AbortController()
    // ref 是同步门禁，可阻止 React loading state 重渲染前的快速双击。
    if (!beginChatOperation(abortCtrlRef, controller)) return

    let assistantTempId = null
    try {
      let activeConvId = currentId
      if (!activeConvId) {
        activeConvId = await handleNewConversation({ cancelActive: false })
        if (!activeConvId) return
      }

      const userContent = input.trim() || '请分析这张图片'
      const currentImage = selectedImage
      const currentImagePreview = imagePreview

      setInput('')
      setSelectedImage(null)
      setImagePreview(null)

      const userTempId = generateTempId()
      assistantTempId = generateTempId()
      setMessages((prev) => [
        ...prev,
        { role: 'user', content: userContent, tempId: userTempId, image: currentImagePreview },
        { role: 'assistant', content: '', tempId: assistantTempId },
      ])
      setLoading(true)
      setCitations([])

      // 如果有图片，走图片分析接口
      if (currentImage) {
        try {
          const response = await analyzeImage(currentImage, userContent, { signal: controller.signal })
          if (!response.ok) {
            throw new Error(`HTTP ${response.status}`)
          }

          let assistantContent = ''
          await readSSEStream(
            response,
            (delta) => {
              assistantContent += delta
              setMessages((prev) => updateMessageByIdentity(
                prev,
                { tempId: assistantTempId },
                { content: assistantContent }
              ))
            },
            () => {},
            // 后端通过 SSE 下发的 error 事件：提示并结束 loading（finally 中统一收尾）
            (errorMsg) => {
              message.error('图片分析失败：' + errorMsg)
              setMessages((prev) => {
                const target = prev.find((item) => item.tempId === assistantTempId)
                return updateMessageByIdentity(
                  prev,
                  { tempId: assistantTempId },
                  { content: target?.content || '[图片分析失败，请稍后重试]' }
                )
              })
            }
          )
        } catch (err) {
          if (err.name !== 'AbortError') {
            message.error('图片分析失败：' + (err.message || '未知错误'))
            setMessages((prev) => updateMessageByIdentity(
              prev,
              { tempId: assistantTempId },
              { content: '[图片分析失败，请稍后重试]' }
            ))
          }
        }
        return
      }

      try {
        const response = await apiFetch('/api/chat', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            message: userContent,
            conversation_id: activeConvId,
            stream: true,
            enable_web_search: enableWebSearch,
            skill: activeSkill,
          }),
          signal: controller.signal,
        })

        if (!response.ok) {
          const errText = await response.text()
          throw new Error(errText || `HTTP ${response.status}`)
        }

        let assistantContent = ''
        await readSSEStream(
          response,
          (delta) => {
            assistantContent += delta
            setMessages((prev) => updateMessageByIdentity(
              prev,
              { tempId: assistantTempId },
              { content: assistantContent }
            ))
          },
          (finalCitations) => {
            if (finalCitations?.length) {
              setCitations(finalCitations)
            }
          },
          // 后端通过 SSE 下发的 error 事件：提示并结束 loading（finally 中统一收尾）
          (errorMsg) => {
            message.error('对话失败：' + errorMsg)
            setMessages((prev) => {
              const target = prev.find((item) => item.tempId === assistantTempId)
              return updateMessageByIdentity(
                prev,
                { tempId: assistantTempId },
                { content: target?.content || '[请求失败，请稍后重试]' }
              )
            })
          }
        )
        fetchConversations()
      } catch (err) {
        if (err.name === 'AbortError') {
          setMessages((prev) => updateMessageByIdentity(
            prev,
            { tempId: assistantTempId },
            { isInterrupted: true }
          ))
        } else {
          // POST /api/chat 已在 SSE 前落用户消息，任何自动重放都可能重复落库/计费。
          message.error('对话请求失败：' + (err.message || '未知错误'))
          setMessages((prev) => updateMessageByIdentity(
            prev,
            { tempId: assistantTempId },
            { content: '[请求失败，请稍后重试]' }
          ))
        }
      }
    } finally {
      if (finishChatOperation(abortCtrlRef, controller)) setLoading(false)
    }
  }, [
    input,
    selectedImage,
    imagePreview,
    currentId,
    handleNewConversation,
    fetchConversations,
    enableWebSearch,
    activeSkill,
  ])

  const handleStop = useCallback(() => {
    abortCtrlRef.current?.abort()
  }, [])

  const handleEditMessage = useCallback(
    async (index) => {
      const msg = messages[index]
      if (!msg || msg.role !== 'user' || !msg.id) return
      try {
        await deleteMessagesFrom(currentId, msg.id)
        // 服务端成功后再提交本地截断，失败时保留原历史。
        setInput(msg.content)
        setMessages((prev) => prev.slice(0, index))
      } catch (err) {
        message.error('编辑消息失败')
      }
    },
    [messages, currentId]
  )

  const handleRegenerate = useCallback(
    async (index) => {
      const msg = messages[index]
      if (!msg || msg.role !== 'assistant' || !msg.id) return

      const controller = new AbortController()
      if (!beginChatOperation(abortCtrlRef, controller)) return

      setRegeneratingMsgId(msg.id)
      setLoading(true)
      setCitations([])

      try {
        const response = await regenerateMessage(currentId, msg.id, { signal: controller.signal })
        if (!response.ok) {
          throw new Error(`HTTP ${response.status}`)
        }

        let newContent = ''
        await readSSEStream(
          response,
          (delta) => {
            newContent += delta
            setMessages((prev) => updateMessageByIdentity(
              prev,
              { id: msg.id },
              { content: newContent, isInterrupted: false }
            ))
          },
          (finalCitations) => {
            if (finalCitations?.length) {
              setCitations(finalCitations)
            }
          },
          // 后端通过 SSE 下发的 error 事件：提示即可，loading 由 finally 收尾
          (errorMsg) => {
            message.error('重新生成失败：' + errorMsg)
          }
        )
        fetchConversations()
      } catch (err) {
        if (err.name !== 'AbortError') {
          message.error('重新生成失败：' + (err.message || '未知错误'))
        }
      } finally {
        if (finishChatOperation(abortCtrlRef, controller)) {
          setLoading(false)
          setRegeneratingMsgId(null)
        }
      }
    },
    [currentId, messages, fetchConversations, readSSEStream]
  )

  const panelStyle = fullHeight
    ? { width: '100%', height: '100%' }
    : {
        position: 'fixed',
        right: 24,
        bottom: 24,
        width: 520,
        height: 640,
        zIndex: 1000,
        display: visible ? 'flex' : 'none',
        flexDirection: 'column',
      }

  const conversationDrawer = (
    <Drawer
      title="会话历史"
      placement="left"
      width={280}
      onClose={() => setDrawerOpen(false)}
      open={drawerOpen}
      bodyStyle={{ padding: 12 }}
    >
      <Button
        type="primary"
        icon={<PlusOutlined />}
        block
        style={{ ...componentStyles.buttonPrimary, marginBottom: 12 }}
        onClick={() => handleNewConversation()}
      >
        新会话
      </Button>
      <List
        dataSource={conversations}
        renderItem={(item) => (
          <List.Item
            style={{
              padding: '8px 12px',
              cursor: 'pointer',
              background: item.id === currentId ? '#e6f4ff' : 'transparent',
              borderRadius: 10,
              transition: 'background 0.2s',
            }}
            onClick={() => {
              loadHistory(item.id)
              setDrawerOpen(false)
            }}
            actions={[
              <Popconfirm
                key="delete"
                title="删除此会话？"
                onConfirm={(e) => handleDeleteConversation(e, item.id)}
                onClick={(e) => e.stopPropagation()}
              >
                <DeleteOutlined style={{ color: colors.error }} />
              </Popconfirm>,
            ]}
          >
            <List.Item.Meta
              title={
                <Text ellipsis style={{ maxWidth: 160, color: colors.textPrimary }}>
                  {item.title || `会话 #${item.id}`}
                </Text>
              }
              description={
                <span style={{ color: colors.textTertiary, fontSize: 12 }}>
                  {item.message_count || 0} 条消息
                </span>
              }
            />
          </List.Item>
        )}
      />
    </Drawer>
  )

  const content = (
    <Card
      title={
        <Space>
          <Button
            type="text"
            icon={<HistoryOutlined />}
            size="small"
            onClick={() => setDrawerOpen(true)}
            style={{ color: colors.textSecondary }}
          >
            历史
          </Button>
          <span style={{ color: colors.textPrimary, fontWeight: 500 }}>Agent 对话</span>
          <Button size="small" icon={<PlusOutlined />} onClick={handleNewConversation}>
            新会话
          </Button>
        </Space>
      }
      extra={
        fullHeight ? null : (
          <Button type="text" icon={<CloseOutlined />} onClick={() => setVisible(false)} />
        )
      }
      style={{
        ...panelStyle,
        borderRadius: fullHeight ? 16 : 20,
        ...componentStyles.card,
        // 悬浮模式：显隐由 panelStyle 的 display 控制（visible 切换），不能在此覆盖
        ...(fullHeight ? { display: 'flex', flexDirection: 'column' } : {}),
        overflow: 'hidden',
      }}
      bodyStyle={{
        flex: 1,
        display: 'flex',
        flexDirection: 'column',
        padding: 0,
        overflow: 'hidden',
        minHeight: 0,
      }}
    >
      {conversationDrawer}
      {/* 主体：消息区可滚动 + 输入区固定底部 */}
      <div
        style={{
          flex: 1,
          display: 'flex',
          flexDirection: 'column',
          overflow: 'hidden',
          minHeight: 0,
        }}
      >
        {/* 消息滚动区 */}
        <div
          className="chat-messages"
          style={{
            flex: 1,
            minHeight: 0,
            overflowY: 'auto',
            padding: '16px 20px',
            display: 'flex',
            flexDirection: 'column',
            gap: 12,
          }}
        >
          {messages.length === 0 && (
            <Empty description="开始一个新对话吧" style={{ marginTop: 40 }} />
          )}
          {messages.map((m, idx) => (
            <MessageBubble
              key={m.id || m.tempId || idx}
              role={m.role}
              content={m.content}
              image={m.image}
              isInterrupted={m.isInterrupted}
              loading={regeneratingMsgId === m.id}
              onEdit={m.role === 'user' ? () => handleEditMessage(idx) : undefined}
              onRegenerate={m.role === 'assistant' ? () => handleRegenerate(idx) : undefined}
            />
          ))}
          {loading &&
            regeneratingMsgId === null &&
            messages[messages.length - 1]?.role === 'assistant' &&
            messages[messages.length - 1]?.content === '' && (
              <Spin size="small" style={{ alignSelf: 'flex-start', margin: 12 }} />
            )}
          <div ref={messagesEndRef} />
        </div>

        {/* 底部固定输入区 */}
        <div
          style={{
            flexShrink: 0,
            borderTop: `1px solid ${colors.border}`,
            background: colors.cardBg,
          }}
        >
          {citations.length > 0 && (
            <div
              style={{
                padding: '8px 12px',
                borderBottom: `1px solid ${colors.border}`,
                background: colors.pageBg,
              }}
            >
              <Text type="secondary" style={{ fontSize: 12 }}>
                引用来源：
              </Text>
              <Space size="small" wrap>
                {citations.map((c, idx) => {
                  const label = c.title ? `${c.title}${c.year ? `（${c.year}）` : ''}` : `文献 #${c.paper_id}`
                  return (
                    <Tag
                      key={idx}
                      size="small"
                      color="blue"
                      style={{
                        borderRadius: 10,
                        border: 'none',
                        cursor: c.paper_id && onSelectPaper ? 'pointer' : 'default',
                      }}
                      onClick={() => c.paper_id && onSelectPaper?.(c.paper_id)}
                    >
                      {label}
                    </Tag>
                  )
                })}
              </Space>
            </div>
          )}
          <div style={{ padding: '12px 16px' }}>
            <div style={{ marginBottom: 8, display: 'flex', gap: 8, flexWrap: 'wrap', alignItems: 'center' }}>
              <Button
                size="small"
                type={enableWebSearch ? 'primary' : 'default'}
                icon={<GlobalOutlined />}
                onClick={() => setEnableWebSearch((v) => !v)}
                style={{ borderRadius: 12 }}
              >
                联网搜索
              </Button>
              <SkillButton
                active={activeSkill === 'translator'}
                icon={<TranslationOutlined />}
                label="翻译"
                onClick={() => setActiveSkill((s) => (s === 'translator' ? null : 'translator'))}
              />
              <SkillButton
                active={activeSkill === 'proofreader'}
                icon={<CheckCircleOutlined />}
                label="校对"
                onClick={() => setActiveSkill((s) => (s === 'proofreader' ? null : 'proofreader'))}
              />
              <SkillButton
                active={activeSkill === 'method_comparator'}
                icon={<DiffOutlined />}
                label="方法对比"
                onClick={() => setActiveSkill((s) => (s === 'method_comparator' ? null : 'method_comparator'))}
              />
              <SkillButton
                active={activeSkill === 'outline_generator'}
                icon={<ProfileOutlined />}
                label="大纲生成"
                onClick={() => setActiveSkill((s) => (s === 'outline_generator' ? null : 'outline_generator'))}
              />
              <SkillButton
                active={activeSkill === 'data_analyst'}
                icon={<BarChartOutlined />}
                label="数据分析"
                onClick={() => setActiveSkill((s) => (s === 'data_analyst' ? null : 'data_analyst'))}
              />
              <Button
                size="small"
                icon={<PictureOutlined />}
                onClick={() => fileInputRef.current?.click()}
                style={{ borderRadius: 12 }}
              >
                图片
              </Button>
              <input
                ref={fileInputRef}
                type="file"
                accept="image/*"
                style={{ display: 'none' }}
                onChange={(e) => {
                  const file = e.target.files?.[0]
                  if (!file) return
                  setSelectedImage(file)
                  const reader = new FileReader()
                  reader.onload = (ev) => setImagePreview(ev.target.result)
                  reader.readAsDataURL(file)
                  e.target.value = ''
                }}
              />
              {activeSkill && (
                <Button size="small" type="link" onClick={() => setActiveSkill(null)} style={{ padding: 0 }}>
                  清除
                </Button>
              )}
            </div>
            {imagePreview && (
              <div style={{ marginBottom: 8, position: 'relative', display: 'inline-block' }}>
                <img
                  src={imagePreview}
                  alt="preview"
                  style={{ maxHeight: 120, borderRadius: 8, border: `1px solid ${colors.border}` }}
                />
                <Button
                  size="small"
                  type="text"
                  danger
                  style={{ position: 'absolute', top: 4, right: 4, background: 'rgba(255,255,255,0.9)' }}
                  onClick={() => {
                    setSelectedImage(null)
                    setImagePreview(null)
                  }}
                >
                  移除
                </Button>
              </div>
            )}
            <Space.Compact style={{ width: '100%' }}>
              <TextArea
                rows={2}
                value={input}
                onChange={(e) => setInput(e.target.value)}
                placeholder={
                  activeSkill
                    ? `当前 Skill：${activeSkill}，输入内容后点击发送...`
                    : '输入问题，基于文献库回答...'
                }
                style={{
                  borderRadius: '20px 0 0 20px',
                  border: `1px solid ${colors.border}`,
                  background: colors.pageBg,
                  resize: 'none',
                }}
                onPressEnter={(e) => {
                  if (!e.shiftKey) {
                    e.preventDefault()
                    if (loading) {
                      handleStop()
                    } else {
                      handleSend()
                    }
                  }
                }}
              />
              <Button
                type="primary"
                icon={loading ? <PauseCircleOutlined /> : <SendOutlined />}
                onClick={loading ? handleStop : handleSend}
                loading={loading && regeneratingMsgId !== null}
                style={{
                  borderRadius: '0 20px 20px 0',
                  width: 56,
                }}
              >
                {loading ? '停止' : '发送'}
              </Button>
            </Space.Compact>
          </div>
        </div>
      </div>
    </Card>
  )

  if (fullHeight) return content

  return (
    <>
      {!visible && (
        <Button
          type="primary"
          shape="circle"
          icon={<MessageOutlined />}
          size="large"
          style={{
            ...componentStyles.fab,
            width: 56,
            height: 56,
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
          }}
          onClick={() => setVisible(true)}
        />
      )}
      {content}
    </>
  )
}

export default ChatPanel
