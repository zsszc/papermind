import { Component } from 'react'
import { Result, Button } from 'antd'

// 全局错误边界：捕获子树渲染/生命周期异常，避免整页白屏。
// 出错时展示 Ant Design Result（500 风格）+ 刷新按钮，错误详情输出到控制台便于排查。
class ErrorBoundary extends Component {
  constructor(props) {
    super(props)
    this.state = { hasError: false }
  }

  static getDerivedStateFromError() {
    // 更新 state 触发兜底 UI 渲染
    return { hasError: true }
  }

  componentDidCatch(error, info) {
    // 错误详情输出到控制台，便于排查
    console.error('[ErrorBoundary] 捕获到渲染异常：', error, info)
  }

  handleReload = () => {
    window.location.reload()
  }

  render() {
    if (this.state.hasError) {
      return (
        <div
          style={{
            display: 'flex',
            justifyContent: 'center',
            alignItems: 'center',
            minHeight: '100vh',
          }}
        >
          <Result
            status="500"
            title="页面出现异常"
            subTitle="应用遇到未预期的错误，请尝试刷新页面。"
            extra={
              <Button type="primary" onClick={this.handleReload}>
                刷新页面
              </Button>
            }
          />
        </div>
      )
    }
    return this.props.children
  }
}

export default ErrorBoundary
