import { useEffect, useState, useRef } from 'react'
import {
  Table,
  Tag,
  Space,
  Button,
  Input,
  Select,
  Popconfirm,
  message,
  Tooltip,
  Modal,
} from 'antd'
import {
  EyeOutlined,
  DeleteOutlined,
  FileTextOutlined,
  ReloadOutlined,
} from '@ant-design/icons'
import {
  listPapers,
  deletePaper,
  summarizePaper,
  listTags,
  batchDeletePapers,
  batchUpdateStatus,
  batchUpdateTags,
} from '../api'
import { colors, componentStyles } from '../theme'

const { Search } = Input
const { Option } = Select

const statusMap = {
  unread: { text: '未读', color: 'default' },
  read: { text: '已读', color: 'success' },
  important: { text: '重要', color: 'error' },
  todo: { text: '待精读', color: 'warning' },
}

function PaperList({ onSelectPaper, onRefresh }) {
  const [papers, setPapers] = useState([])
  const [total, setTotal] = useState(0)
  const [loading, setLoading] = useState(false)
  const [allTags, setAllTags] = useState([])
  const [params, setParams] = useState({ skip: 0, limit: 20, q: '', status: undefined, tag: undefined })
  const [selectedRowKeys, setSelectedRowKeys] = useState([])
  const pollingRef = useRef(null)
  // 保存最新的 fetchPapers，避免轮询定时器闭包捕获过期的查询参数
  const fetchPapersRef = useRef(null)

  const fetchPapers = async () => {
    setLoading(true)
    try {
      const reqParams = { ...params }
      if (Array.isArray(reqParams.tag) && reqParams.tag.length > 0) {
        reqParams.tag = reqParams.tag.join(',')
      } else if (Array.isArray(reqParams.tag) && reqParams.tag.length === 0) {
        reqParams.tag = undefined
      }
      const res = await listPapers(reqParams)
      const items = res.data.items || []
      setPapers(items)
      setTotal(res.data.total || 0)

      // 如果存在处理中的论文，启动轮询刷新
      const hasProcessing = items.some(
        (p) => p.processed === 'pending' || p.processed === 'processing'
      )
      if (hasProcessing && !pollingRef.current) {
        pollingRef.current = setInterval(() => fetchPapersRef.current?.(), 2000)
      } else if (!hasProcessing && pollingRef.current) {
        clearInterval(pollingRef.current)
        pollingRef.current = null
      }
    } finally {
      setLoading(false)
    }
  }
  fetchPapersRef.current = fetchPapers

  useEffect(() => {
    fetchPapers()
  }, [params])

  useEffect(() => {
    listTags()
      .then((res) => setAllTags(res.data || []))
      .catch(() => {})
  }, [])

  useEffect(() => {
    return () => {
      if (pollingRef.current) {
        clearInterval(pollingRef.current)
        pollingRef.current = null
      }
    }
  }, [])

  const handleDelete = async (id) => {
    await deletePaper(id)
    message.success('已删除')
    // 若删除的是当前页最后一条且不在第一页，自动回退一页，避免停留在空页
    if (papers.length === 1 && params.skip > 0) {
      setParams((p) => ({ ...p, skip: Math.max(0, p.skip - p.limit) }))
    } else {
      fetchPapers()
    }
    onRefresh?.()
  }

  const handleBatchDelete = () => {
    Modal.confirm({
      title: `确认删除选中的 ${selectedRowKeys.length} 篇论文？`,
      content: '删除后无法恢复',
      okText: '删除',
      okType: 'danger',
      cancelText: '取消',
      onOk: async () => {
        try {
          await batchDeletePapers(selectedRowKeys)
          message.success('批量删除成功')
          setSelectedRowKeys([])
          // 批量删除可能清空当前页，非第一页时回到第一页重新加载
          if (params.skip > 0) {
            setParams((p) => ({ ...p, skip: 0 }))
          } else {
            fetchPapers()
          }
          onRefresh?.()
        } catch (err) {
          // 全局拦截器已处理错误
        }
      },
    })
  }

  const handleBatchStatus = async (status) => {
    try {
      await batchUpdateStatus(selectedRowKeys, status)
      message.success('状态更新成功')
      fetchPapers()
    } catch (err) {
      // 全局拦截器已处理错误
    }
  }

  const handleBatchAddTag = async (tagName) => {
    if (!tagName) return
    try {
      await batchUpdateTags(selectedRowKeys, [tagName], 'add')
      message.success('标签添加成功')
      fetchPapers()
    } catch (err) {
      // 全局拦截器已处理错误
    }
  }

  const handleSummarize = async (id) => {
    const hideLoading = message.loading('正在生成 AI 概括，请耐心等待...', 0)
    try {
      await summarizePaper(id, { skipGlobalError: true })
      message.success('概括完成，已保存到笔记')
    } catch (err) {
      const detail = err.response?.data?.detail || err.message || '未知错误'
      message.error('概括失败：' + detail)
    } finally {
      hideLoading()
    }
  }

  const columns = [
    {
      title: '标题',
      dataIndex: 'title',
      key: 'title',
      ellipsis: true,
      render: (text) => (
        <span style={{ fontWeight: 500, color: colors.textPrimary }}>
          {text || '（未识别标题）'}
        </span>
      ),
    },
    {
      title: '作者',
      dataIndex: 'authors',
      key: 'authors',
      ellipsis: true,
      render: (text) => <span style={{ color: colors.textSecondary }}>{text || '-'}</span>,
    },
    {
      title: '年份',
      dataIndex: 'year',
      key: 'year',
      width: 80,
      render: (text) => <span style={{ color: colors.textSecondary }}>{text || '-'}</span>,
    },
    {
      title: '状态',
      dataIndex: 'status',
      key: 'status',
      width: 90,
      render: (s) => (
        <Tag
          color={statusMap[s]?.color}
          style={{ borderRadius: 12, padding: '2px 10px', border: 'none' }}
        >
          {statusMap[s]?.text || s}
        </Tag>
      ),
    },
    {
      title: '处理',
      dataIndex: 'processed',
      key: 'processed',
      width: 90,
      render: (p) => {
        const map = {
          pending: { text: '待处理', color: 'default' },
          processing: { text: '处理中', color: 'processing' },
          done: { text: '已完成', color: 'success' },
          error: { text: '失败', color: 'error' },
        }
        return (
          <Tag
            color={map[p]?.color}
            style={{ borderRadius: 12, padding: '2px 10px', border: 'none' }}
          >
            {map[p]?.text || p}
          </Tag>
        )
      },
    },
    {
      title: '标签',
      dataIndex: 'tags',
      key: 'tags',
      width: 140,
      render: (tags) =>
        tags?.slice(0, 3).map((t) => (
          <Tag
            key={t.id}
            color={t.color}
            style={{ borderRadius: 12, padding: '2px 8px', border: 'none' }}
          >
            {t.name}
          </Tag>
        )) || '-',
    },
    {
      title: '操作',
      key: 'action',
      width: 120,
      fixed: 'right',
      render: (_, record) => (
        <Space size="small">
          <Tooltip title="详情">
            <Button
              type="text"
              size="small"
              icon={<EyeOutlined />}
              onClick={() => onSelectPaper(record.id)}
              style={{ color: colors.primary }}
            />
          </Tooltip>
          <Tooltip title="AI 概括">
            <Button
              type="text"
              size="small"
              icon={<FileTextOutlined />}
              onClick={() => handleSummarize(record.id)}
              style={{ color: colors.textSecondary }}
            />
          </Tooltip>
          <Tooltip title="删除">
            <Popconfirm
              title="确认删除？"
              onConfirm={() => handleDelete(record.id)}
            >
              <Button
                type="text"
                size="small"
                danger
                icon={<DeleteOutlined />}
              />
            </Popconfirm>
          </Tooltip>
        </Space>
      ),
    },
  ]

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
      <div style={{ ...componentStyles.card, padding: '16px 20px', marginBottom: 16 }}>
        <Space size="middle" wrap>
          <Search
            placeholder="搜索标题/作者/摘要"
            allowClear
            onSearch={(v) => setParams((p) => ({ ...p, q: v, skip: 0 }))}
            style={{ width: 280 }}
          />
          <Select
            placeholder="阅读状态"
            allowClear
            style={{ width: 130 }}
            onChange={(v) => setParams((p) => ({ ...p, status: v, skip: 0 }))}
          >
            <Option value="unread">未读</Option>
            <Option value="read">已读</Option>
            <Option value="important">重要</Option>
            <Option value="todo">待精读</Option>
          </Select>
          <Select
            mode="multiple"
            placeholder="按标签筛选"
            allowClear
            style={{ minWidth: 200 }}
            onChange={(v) => setParams((p) => ({ ...p, tag: v, skip: 0 }))}
            options={tagOptions}
          />
          <Button icon={<ReloadOutlined />} onClick={fetchPapers}>
            刷新
          </Button>
        </Space>
      </div>
      <div style={{ ...componentStyles.card, padding: '20px 24px' }}>
        {selectedRowKeys.length > 0 && (
          <div
            style={{
              marginBottom: 16,
              padding: '10px 14px',
              background: '#f6ffed',
              borderRadius: 10,
              display: 'flex',
              gap: 12,
              alignItems: 'center',
              flexWrap: 'wrap',
            }}
          >
            <span style={{ fontWeight: 500, color: colors.textPrimary }}>
              已选择 {selectedRowKeys.length} 项
            </span>
            <Select
              placeholder="修改状态"
              style={{ width: 120 }}
              onChange={handleBatchStatus}
              value={undefined}
            >
              <Option value="unread">未读</Option>
              <Option value="read">已读</Option>
              <Option value="important">重要</Option>
              <Option value="todo">待精读</Option>
            </Select>
            <Select
              placeholder="添加标签"
              style={{ width: 150 }}
              onChange={handleBatchAddTag}
              value={undefined}
              options={tagOptions}
            />
            <Button danger onClick={handleBatchDelete}>
              批量删除
            </Button>
            <Button onClick={() => setSelectedRowKeys([])}>取消选择</Button>
          </div>
        )}
        <Table
          rowKey="id"
          rowSelection={{
            selectedRowKeys,
            onChange: setSelectedRowKeys,
            preserveSelectedRowKeys: false,
          }}
          columns={columns}
          dataSource={papers}
          loading={loading}
          scroll={{ x: 900 }}
          pagination={{
            pageSize: params.limit,
            current: Math.floor(params.skip / params.limit) + 1,
            total,
            showTotal: (t) => `共 ${t} 篇`,
            showSizeChanger: true,
            pageSizeOptions: ['10', '20', '50', '100'],
            onChange: (page, pageSize) =>
              setParams((p) => ({ ...p, skip: (page - 1) * pageSize, limit: pageSize })),
          }}
        />
      </div>
    </div>
  )
}

export default PaperList
