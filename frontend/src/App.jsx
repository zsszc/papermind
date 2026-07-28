import { useState, useEffect, memo, Suspense, lazy } from 'react'
import { Layout, Menu, Typography, Button, Upload, message, Spin } from 'antd'
import {
  FileTextOutlined,
  SearchOutlined,
  MessageOutlined,
  UploadOutlined,
  BookOutlined,
  EditOutlined,
  ExportOutlined,
  BarChartOutlined,
  MenuFoldOutlined,
  MenuUnfoldOutlined,
  SettingOutlined,
} from '@ant-design/icons'
import ChatPanel from './components/ChatPanel'
import SettingsModal from './components/SettingsModal'
import ErrorBoundary from './components/ErrorBoundary'
import { importPapers } from './api'
import { colors, componentStyles } from './theme'

const { Header, Sider, Content } = Layout
const { Title } = Typography

// 懒加载非首屏页面
const PaperList = lazy(() => import('./pages/PaperList'))
const PaperDetail = lazy(() => import('./pages/PaperDetail'))
const SearchPage = lazy(() => import('./pages/SearchPage'))
const ThesisList = lazy(() => import('./pages/ThesisList'))
const ThesisDetail = lazy(() => import('./pages/ThesisDetail'))
const WritingDesk = lazy(() => import('./pages/WritingDesk'))
const DataExport = lazy(() => import('./pages/DataExport'))
const StatsPage = lazy(() => import('./pages/StatsPage'))

const SIDER_WIDTH = 200
const SIDER_COLLAPSED_WIDTH = 80
const COLLAPSE_STORAGE_KEY = 'papermind-sider-collapsed'

const menuItems = [
  { key: 'papers', icon: <FileTextOutlined />, label: '文献' },
  { key: 'search', icon: <SearchOutlined />, label: '检索' },
  { key: 'thesis', icon: <BookOutlined />, label: '论文' },
  { key: 'writing', icon: <EditOutlined />, label: '写作' },
  { key: 'stats', icon: <BarChartOutlined />, label: '统计' },
  { key: 'chat', icon: <MessageOutlined />, label: '对话' },
  { key: 'export', icon: <ExportOutlined />, label: '导出' },
]

const AppHeader = memo(function AppHeader({ onUploadSuccess, onOpenSettings }) {
  const handleUpload = async ({ fileList }) => {
    const files = fileList.map((f) => f.originFileObj).filter(Boolean)
    if (!files.length) return
    try {
      const res = await importPapers(files)
      message.success(`成功导入 ${res.data.total} 篇文献，后台正在自动处理...`)
      onUploadSuccess()
    } catch (err) {
      message.error(err.response?.data?.detail || '导入失败')
    }
  }

  return (
    <Header
      style={{
        ...componentStyles.glassHeader,
        position: 'sticky',
        top: 0,
        zIndex: 100,
        padding: '0 24px',
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'space-between',
        height: 64,
      }}
    >
      <Title level={4} style={{ margin: 0, color: colors.textPrimary, fontWeight: 700 }}>
        PaperMind
      </Title>
      <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
        <Button
          type="text"
          icon={<SettingOutlined />}
          onClick={onOpenSettings}
          style={{ color: colors.textSecondary }}
        >
          设置
        </Button>
        <Upload
          multiple
          accept=".pdf"
          beforeUpload={() => false}
          onChange={handleUpload}
          fileList={[]}
        >
          <Button
            type="primary"
            icon={<UploadOutlined />}
            style={componentStyles.buttonPrimary}
          >
            导入 PDF
          </Button>
        </Upload>
      </div>
    </Header>
  )
})

const PageSkeleton = () => (
  <div style={{ display: 'flex', justifyContent: 'center', alignItems: 'center', height: '60vh' }}>
    <Spin size="large" tip="加载中..." />
  </div>
)

function App() {
  const [view, setView] = useState('papers')
  const [selectedPaperId, setSelectedPaperId] = useState(null)
  const [selectedPaperOptions, setSelectedPaperOptions] = useState(null)
  const [selectedThesisId, setSelectedThesisId] = useState(null)
  const [refreshKey, setRefreshKey] = useState(0)
  const [settingsOpen, setSettingsOpen] = useState(false)
  const [collapsed, setCollapsed] = useState(() => {
    try {
      return JSON.parse(localStorage.getItem(COLLAPSE_STORAGE_KEY) || 'false')
    } catch {
      return false
    }
  })

  useEffect(() => {
    const handleResize = () => {
      if (window.innerWidth < 768) {
        setCollapsed(true)
      }
    }
    handleResize()
    window.addEventListener('resize', handleResize)
    return () => window.removeEventListener('resize', handleResize)
  }, [])

  useEffect(() => {
    localStorage.setItem(COLLAPSE_STORAGE_KEY, JSON.stringify(collapsed))
  }, [collapsed])

  const handleViewChange = ({ key }) => {
    setView(key)
    if (key === 'papers') setSelectedPaperId(null)
    if (key === 'thesis') setSelectedThesisId(null)
  }

  const handleRefresh = () => setRefreshKey((k) => k + 1)

  const siderWidth = collapsed ? SIDER_COLLAPSED_WIDTH : SIDER_WIDTH

  return (
    // 错误边界包在最外层，不破坏内部 React.lazy 的 Suspense 结构
    <ErrorBoundary>
      <Layout style={{ minHeight: '100vh', background: colors.pageBg }}>
      <AppHeader onUploadSuccess={handleRefresh} onOpenSettings={() => setSettingsOpen(true)} />
      <Layout style={{ background: colors.pageBg }}>
        <Sider
          width={SIDER_WIDTH}
          collapsedWidth={SIDER_COLLAPSED_WIDTH}
          collapsed={collapsed}
          collapsible
          trigger={null}
          onCollapse={setCollapsed}
          style={{
            ...componentStyles.sider,
            position: 'fixed',
            left: 0,
            top: 64,
            bottom: 0,
            zIndex: 99,
            overflow: 'auto',
          }}
        >
          <div style={{ padding: '12px 12px 0', display: 'flex', justifyContent: 'flex-end' }}>
            <Button
              type="text"
              size="small"
              icon={collapsed ? <MenuUnfoldOutlined /> : <MenuFoldOutlined />}
              onClick={() => setCollapsed((c) => !c)}
              style={{ color: colors.textSecondary }}
            />
          </div>
          <Menu
            mode="inline"
            selectedKeys={[view]}
            onClick={handleViewChange}
            inlineCollapsed={collapsed}
            items={menuItems.map((item) => ({
              key: item.key,
              icon: item.icon,
              label: <span style={{ fontSize: 14 }}>{item.label}</span>,
            }))}
            style={{
              borderRight: 'none',
              background: 'transparent',
              paddingTop: 8,
            }}
            theme="light"
          />
        </Sider>
        <Content
          style={{
            marginLeft: siderWidth,
            padding: '24px 32px 24px 24px',
            overflow: 'auto',
            minHeight: 'calc(100vh - 64px)',
            transition: 'margin-left 0.2s',
          }}
        >
          <Suspense fallback={<PageSkeleton />}>
            {view === 'papers' && !selectedPaperId && (
              <PaperList
                key={refreshKey}
                onSelectPaper={setSelectedPaperId}
                onRefresh={handleRefresh}
              />
            )}
            {view === 'papers' && selectedPaperId && (
              <PaperDetail
                paperId={selectedPaperId}
                initialPage={selectedPaperOptions?.initialPage}
                onBack={() => {
                  setSelectedPaperId(null)
                  setSelectedPaperOptions(null)
                }}
              />
            )}
            {view === 'search' && (
              <SearchPage
                onSelectPaper={(id, options = null) => {
                  setSelectedPaperId(id)
                  setSelectedPaperOptions(options)
                  setView('papers')
                }}
              />
            )}
            {view === 'thesis' && !selectedThesisId && (
              <ThesisList onSelect={setSelectedThesisId} />
            )}
            {view === 'thesis' && selectedThesisId && (
              <ThesisDetail
                thesisId={selectedThesisId}
                onBack={() => setSelectedThesisId(null)}
                onSelectPaper={(id, options = null) => {
                  setSelectedPaperId(id)
                  setSelectedPaperOptions(options)
                  setView('papers')
                }}
              />
            )}
            {view === 'writing' && (
              <WritingDesk
                onSelectPaper={(id, options = null) => {
                  setSelectedPaperId(id)
                  setSelectedPaperOptions(options)
                  setView('papers')
                }}
              />
            )}
            {view === 'export' && <DataExport />}
            {view === 'stats' && <StatsPage />}
            {view === 'chat' && (
              <div style={{ ...componentStyles.card, height: 'calc(100vh - 112px)', padding: 0, overflow: 'hidden' }}>
                <ChatPanel
                  fullHeight
                  onSelectPaper={(id, options = null) => {
                    setSelectedPaperId(id)
                    setSelectedPaperOptions(options)
                    setView('papers')
                  }}
                />
              </div>
            )}
          </Suspense>
        </Content>
      </Layout>
      {view !== 'chat' && (
        <ChatPanel
          onSelectPaper={(id, options = null) => {
            setSelectedPaperId(id)
            setSelectedPaperOptions(options)
            setView('papers')
          }}
        />
      )}
      <SettingsModal open={settingsOpen} onClose={() => setSettingsOpen(false)} />
      </Layout>
    </ErrorBoundary>
  )
}

export default App
