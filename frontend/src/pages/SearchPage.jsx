import { useState } from 'react'
import {
  Input,
  Card,
  List,
  Tag,
  Radio,
  Space,
  Typography,
  Button,
  Empty,
} from 'antd'
import { SearchOutlined } from '@ant-design/icons'
import { searchPapers } from '../api'
import { colors, componentStyles } from '../theme'

const { Search } = Input
const { Text } = Typography

function highlight(text, keyword) {
  if (!text || !keyword) return text
  const normalizedKeyword = keyword.toLowerCase()
  const escaped = keyword.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
  const parts = text.split(new RegExp(`(${escaped})`, 'i'))
  return parts.map((part, index) =>
    part.toLowerCase() === normalizedKeyword ? (
      <mark
        key={index}
        style={{
          background: '#fff566',
          padding: '0 3px',
          borderRadius: 4,
          fontWeight: 500,
        }}
      >
        {part}
      </mark>
    ) : (
      <span key={index}>{part}</span>
    )
  )
}

function SearchPage({ onSelectPaper }) {
  const [query, setQuery] = useState('')
  const [mode, setMode] = useState('hybrid')
  const [results, setResults] = useState([])
  const [loading, setLoading] = useState(false)

  const handleSearch = async (value) => {
    const trimmed = value.trim()
    if (!trimmed) return
    setLoading(true)
    try {
      const res = await searchPapers({
        query: trimmed,
        top_k: 20,
        use_semantic: mode === 'semantic' || mode === 'hybrid',
        use_keyword: mode === 'keyword' || mode === 'hybrid',
      })
      setResults(res.data.results || [])
    } finally {
      setLoading(false)
    }
  }

  const sourceLabel = {
    semantic: '语义',
    keyword: '关键词',
    hybrid: '混合',
  }

  return (
    <div>
      <Card style={{ ...componentStyles.card, marginBottom: 16 }}>
        <Space direction="vertical" style={{ width: '100%' }}>
          <Radio.Group value={mode} onChange={(e) => setMode(e.target.value)}>
            <Radio.Button value="hybrid">混合检索</Radio.Button>
            <Radio.Button value="semantic">语义检索</Radio.Button>
            <Radio.Button value="keyword">关键词检索</Radio.Button>
          </Radio.Group>
          <Search
            placeholder="输入关键词、短语或研究问题..."
            enterButton={
              <>
                <SearchOutlined /> 检索
              </>
            }
            size="large"
            loading={loading}
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            onSearch={handleSearch}
          />
        </Space>
      </Card>

      <List
        dataSource={results}
        renderItem={(item) => (
          <Card
            style={{ ...componentStyles.card, marginBottom: 12 }}
            bodyStyle={{ padding: '16px 20px' }}
          >
            <div
              style={{
                display: 'flex',
                justifyContent: 'space-between',
                alignItems: 'flex-start',
                marginBottom: 4,
              }}
            >
              <Text strong style={{ fontSize: 16, color: colors.textPrimary }}>
                {highlight(item.title, query)}
              </Text>
              <Tag color={item.source === 'hybrid' ? 'purple' : item.source === 'semantic' ? 'blue' : 'green'}>
                {sourceLabel[item.source] || item.source}
              </Tag>
            </div>
            <Text type="secondary" style={{ fontSize: 13 }}>
              {highlight(item.authors, query) || '未知作者'} | {item.year || '-'}
            </Text>
            <div
              style={{
                marginTop: 10,
                color: colors.textPrimary,
                lineHeight: 1.6,
                fontSize: 14,
              }}
            >
              {highlight(item.content, query)}
            </div>
            <Space style={{ marginTop: 8 }}>
              <Button
                type="link"
                style={{ paddingLeft: 0 }}
                onClick={() => onSelectPaper(item.paper_id)}
              >
                查看文献
              </Button>
              {item.page_number && (
                <Button
                  type="link"
                  style={{ paddingLeft: 0 }}
                  onClick={() =>
                    onSelectPaper(item.paper_id, { initialPage: item.page_number })
                  }
                >
                  跳转第 {item.page_number} 页
                </Button>
              )}
            </Space>
          </Card>
        )}
        locale={{
          emptyText: (
            <Empty
              description={
                query ? '未找到相关文献' : '输入关键词开始检索文献库'
              }
            />
          ),
        }}
      />
    </div>
  )
}

export default SearchPage
