import { useState, useRef, useEffect } from 'react'
import { Document, Page, pdfjs } from 'react-pdf'
import 'react-pdf/dist/esm/Page/AnnotationLayer.css'
import 'react-pdf/dist/esm/Page/TextLayer.css'
import {
  Spin,
  Button,
  Space,
  Typography,
  InputNumber,
  Tooltip,
  Modal,
  Input,
  List,
  message,
} from 'antd'
import {
  ZoomInOutlined,
  ZoomOutOutlined,
  ColumnWidthOutlined,
  DownloadOutlined,
  HighlightOutlined,
  DeleteOutlined,
} from '@ant-design/icons'
import {
  getReadProgress,
  updateReadProgress,
  listAnnotations,
  createAnnotation,
  deleteAnnotation,
} from '../api'
import { colors } from '../theme'

pdfjs.GlobalWorkerOptions.workerSrc = './pdf.worker.min.js'

// 忽略 PDF.js worker 在组件卸载时被终止的正常错误
if (typeof window !== 'undefined') {
  window.addEventListener('unhandledrejection', (event) => {
    const msg = event.reason?.message || String(event.reason)
    if (msg.includes('Worker was terminated') || msg.includes('Loading aborted')) {
      event.preventDefault()
    }
  })
}

const { Text } = Typography
const { TextArea } = Input

function PdfViewer({ url, paperId, initialPage = 1 }) {
  const [numPages, setNumPages] = useState(null)
  const [pageNumber, setPageNumber] = useState(initialPage)
  const [scale, setScale] = useState(1.2)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)
  const [selectedText, setSelectedText] = useState('')
  const [annotations, setAnnotations] = useState([])
  const [annoModalOpen, setAnnoModalOpen] = useState(false)
  const [annoNote, setAnnoNote] = useState('')
  const containerRef = useRef(null)

  useEffect(() => {
    if (initialPage >= 1) setPageNumber(initialPage)
  }, [initialPage])

  // 加载上次阅读进度（仅当没有外部指定初始页时）
  useEffect(() => {
    if (!paperId || initialPage >= 1) return
    getReadProgress(paperId)
      .then((res) => {
        const page = res.data?.last_read_page
        if (page && page >= 1) setPageNumber(page)
      })
      .catch(() => {})
  }, [paperId, initialPage])

  // 自动保存阅读进度（防抖 1 秒）
  useEffect(() => {
    if (!paperId) return
    const timer = setTimeout(() => {
      updateReadProgress(paperId, pageNumber).catch(() => {})
    }, 1000)
    return () => clearTimeout(timer)
  }, [pageNumber, paperId])

  // 加载批注
  const loadAnnotations = () => {
    if (!paperId) return
    listAnnotations(paperId)
      .then((res) => setAnnotations(res.data || []))
      .catch(() => {})
  }

  useEffect(() => {
    loadAnnotations()
  }, [paperId])

  function onDocumentLoadSuccess({ numPages }) {
    setNumPages(numPages)
    setPageNumber((p) => Math.min(Math.max(1, p), numPages))
    setLoading(false)
    setError(null)
  }

  const goToPage = (value) => {
    if (!numPages) return
    setPageNumber(Math.min(Math.max(1, value), numPages))
  }
  const zoomIn = () => setScale((s) => Math.min(2.5, s + 0.2))
  const zoomOut = () => setScale((s) => Math.max(0.5, s - 0.2))
  const fitWidth = () => {
    if (containerRef.current && numPages) {
      const width = containerRef.current.clientWidth - 48
      setScale(Math.min(2.5, Math.max(0.5, width / 612)))
    }
  }

  const handleMouseUp = () => {
    const text = window.getSelection()?.toString().trim() || ''
    setSelectedText(text)
  }

  const handleAddAnnotation = async () => {
    if (!selectedText || !paperId) return
    try {
      await createAnnotation(paperId, {
        page_number: pageNumber,
        selected_text: selectedText,
        note: annoNote,
        color: 'yellow',
      })
      message.success('批注已保存')
      setAnnoModalOpen(false)
      setAnnoNote('')
      setSelectedText('')
      window.getSelection()?.removeAllRanges()
      loadAnnotations()
    } catch (err) {
      // 全局拦截器已处理错误
    }
  }

  const handleDeleteAnnotation = async (id) => {
    try {
      await deleteAnnotation(paperId, id)
      message.success('批注已删除')
      loadAnnotations()
    } catch (err) {
      // 全局拦截器已处理错误
    }
  }

  const pageAnnotations = annotations.filter((a) => a.page_number === pageNumber)

  return (
    <div style={{ height: '100%', display: 'flex', flexDirection: 'column' }}>
      <Space style={{ marginBottom: 8, padding: '0 12px', flexWrap: 'wrap', rowGap: 8 }}>
        <Button size="small" disabled={pageNumber <= 1} onClick={() => goToPage(pageNumber - 1)}>
          上一页
        </Button>
        <Space size={4}>
          <Text>第</Text>
          <InputNumber
            min={1}
            max={numPages || 1}
            value={pageNumber}
            onChange={goToPage}
            size="small"
            style={{ width: 60 }}
            controls={false}
          />
          <Text>/ {numPages || '-'} 页</Text>
        </Space>
        <Button
          size="small"
          disabled={!numPages || pageNumber >= numPages}
          onClick={() => goToPage(pageNumber + 1)}
        >
          下一页
        </Button>
        <Tooltip title="放大">
          <Button size="small" icon={<ZoomInOutlined />} onClick={zoomIn} />
        </Tooltip>
        <Tooltip title="缩小">
          <Button size="small" icon={<ZoomOutOutlined />} onClick={zoomOut} />
        </Tooltip>
        <Tooltip title="适应宽度">
          <Button size="small" icon={<ColumnWidthOutlined />} onClick={fitWidth} />
        </Tooltip>
        <Tooltip title="添加批注">
          <Button
            size="small"
            icon={<HighlightOutlined />}
            disabled={!selectedText}
            onClick={() => setAnnoModalOpen(true)}
          >
            批注
          </Button>
        </Tooltip>
        <Button size="small" icon={<DownloadOutlined />} href={url} target="_blank">
          下载
        </Button>
      </Space>

      <div
        ref={containerRef}
        style={{
          flex: 1,
          overflow: 'auto',
          background: '#f5f5f5',
          textAlign: 'center',
          padding: '0 12px',
        }}
        onMouseUp={handleMouseUp}
      >
        {loading && (
          <div style={{ padding: 40 }}>
            <Spin tip="加载 PDF..." />
          </div>
        )}
        {error && (
          <div style={{ padding: 40 }}>
            <Text type="danger">{error}</Text>
            <br />
            <a href={url} target="_blank" rel="noopener noreferrer">
              点击下载查看
            </a>
          </div>
        )}
        <Document
          file={url}
          onLoadSuccess={onDocumentLoadSuccess}
          onLoadError={(err) => {
            setLoading(false)
            setError(err?.message || 'PDF 加载失败')
          }}
          loading={null}
        >
          <Page
            pageNumber={pageNumber}
            scale={scale}
            renderTextLayer
            renderAnnotationLayer={false}
          />
        </Document>
      </div>

      {pageAnnotations.length > 0 && (
        <div
          style={{
            height: 120,
            flexShrink: 0,
            borderTop: `1px solid ${colors.border}`,
            background: '#fff',
            overflow: 'auto',
            padding: '8px 12px',
          }}
        >
          <Text type="secondary" style={{ fontSize: 12 }}>
            本页批注（{pageAnnotations.length}）
          </Text>
          <List
            size="small"
            dataSource={pageAnnotations}
            renderItem={(item) => (
              <List.Item
                actions={[
                  <Button
                    key="delete"
                    type="text"
                    size="small"
                    icon={<DeleteOutlined />}
                    danger
                    onClick={() => handleDeleteAnnotation(item.id)}
                  />,
                ]}
              >
                <div style={{ fontSize: 13 }}>
                  <Text strong>{item.selected_text}</Text>
                  {item.note && (
                    <div style={{ color: colors.textSecondary, marginTop: 2 }}>{item.note}</div>
                  )}
                </div>
              </List.Item>
            )}
          />
        </div>
      )}

      <Modal
        title="添加批注"
        open={annoModalOpen}
        onCancel={() => setAnnoModalOpen(false)}
        onOk={handleAddAnnotation}
        okText="保存"
        cancelText="取消"
      >
        <div style={{ marginBottom: 12 }}>
          <Text type="secondary">选中内容：</Text>
          <div
            style={{
              marginTop: 4,
              padding: 8,
              background: '#fffbe6',
              borderRadius: 6,
              fontSize: 13,
            }}
          >
            {selectedText}
          </div>
        </div>
        <TextArea
          rows={4}
          placeholder="添加批注笔记..."
          value={annoNote}
          onChange={(e) => setAnnoNote(e.target.value)}
        />
      </Modal>
    </div>
  )
}

export default PdfViewer
