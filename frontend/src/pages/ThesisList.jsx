import { useEffect, useState } from 'react'
import { Table, Button, Upload, message, Typography, Tag, Popconfirm, Space, Tooltip } from 'antd'
import { UploadOutlined, EyeOutlined, DeleteOutlined, ReloadOutlined } from '@ant-design/icons'
import { listThesis, uploadThesis, deleteThesis } from '../api'
import { colors, componentStyles } from '../theme'

const { Title } = Typography

function ThesisList({ onSelect }) {
  const [thesisList, setThesisList] = useState([])
  const [loading, setLoading] = useState(false)

  const fetchThesis = async () => {
    setLoading(true)
    try {
      const res = await listThesis()
      setThesisList(res.data.items || [])
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    fetchThesis()
  }, [])

  const handleUpload = async ({ file }) => {
    try {
      await uploadThesis(file)
      message.success('上传成功')
      fetchThesis()
    } catch (err) {
      message.error('上传失败')
    }
  }

  const handleDelete = async (id) => {
    try {
      await deleteThesis(id)
      message.success('已删除')
      fetchThesis()
    } catch (err) {
      message.error('删除失败')
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
      title: '文件名',
      dataIndex: 'filename',
      key: 'filename',
      ellipsis: true,
      render: (text) => <span style={{ color: colors.textSecondary }}>{text}</span>,
    },
    {
      title: '章节数',
      key: 'chapters',
      width: 80,
      render: (_, record) => record.chapter_structure?.length || 0,
    },
    {
      title: '字数',
      dataIndex: 'word_count',
      key: 'word_count',
      width: 100,
      render: (text) => <span style={{ color: colors.textSecondary }}>{text || '-'}</span>,
    },
    {
      title: '状态',
      key: 'status',
      width: 110,
      render: (_, record) => {
        const count = record.metadata_json?.citations_detected || 0
        return count > 0 ? (
          <Tag
            color={colors.primary}
            style={{ borderRadius: 12, padding: '2px 10px', border: 'none' }}
          >
            引用 {count}
          </Tag>
        ) : (
          <Tag style={{ borderRadius: 12, padding: '2px 10px', border: 'none' }}>无引用</Tag>
        )
      },
    },
    {
      title: '操作',
      key: 'action',
      width: 110,
      fixed: 'right',
      render: (_, record) => (
        <Space size="small">
          <Tooltip title="查看">
            <Button
              type="text"
              size="small"
              icon={<EyeOutlined />}
              onClick={() => onSelect(record.id)}
              style={{ color: colors.primary }}
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

  return (
    <div>
      <div style={{ ...componentStyles.card, padding: '16px 20px', marginBottom: 16 }}>
        <Space wrap>
          <Upload
            accept=".docx"
            beforeUpload={() => false}
            onChange={handleUpload}
            showUploadList={false}
            maxCount={1}
          >
            <Button
              type="primary"
              icon={<UploadOutlined />}
              style={componentStyles.buttonPrimary}
            >
              上传 Word 论文
            </Button>
          </Upload>
          <Button icon={<ReloadOutlined />} onClick={fetchThesis}>
            刷新
          </Button>
        </Space>
      </div>
      <div style={{ ...componentStyles.card, padding: '20px 24px' }}>
        <Table
          rowKey="id"
          columns={columns}
          dataSource={thesisList}
          loading={loading}
          scroll={{ x: 700 }}
          pagination={{ pageSize: 10 }}
        />
      </div>
    </div>
  )
}

export default ThesisList
