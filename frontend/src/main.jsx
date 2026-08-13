import React from 'react'
import ReactDOM from 'react-dom/client'
import { ConfigProvider } from 'antd'
import zhCN from 'antd/locale/zh_CN'
import App from './App.jsx'
import './index.css'
import { themeTokens } from './theme'
import { initializeRuntimeConfig } from './utils/apiUrl'

async function bootstrap() {
  const root = ReactDOM.createRoot(document.getElementById('root'))
  try {
    await initializeRuntimeConfig()
    root.render(
      <React.StrictMode>
        <ConfigProvider locale={zhCN} theme={{ token: themeTokens }}>
          <App />
        </ConfigProvider>
      </React.StrictMode>,
    )
  } catch (error) {
    console.error('[startup] 运行配置初始化失败', error)
    root.render(<div style={{ padding: 32 }}>PaperMind 安全启动失败，请重新启动应用。</div>)
  }
}

void bootstrap()
