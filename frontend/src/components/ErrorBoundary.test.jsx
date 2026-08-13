import React from 'react'
import { describe, expect, it, vi } from 'vitest'
import { render, screen } from '@testing-library/react'

import ErrorBoundary from './ErrorBoundary'

function BrokenChild() {
  throw new Error('测试渲染失败')
}

describe('ErrorBoundary', () => {
  it('子组件抛错时展示降级 UI', () => {
    const consoleError = vi.spyOn(console, 'error').mockImplementation(() => {})

    render(
      <ErrorBoundary>
        <BrokenChild />
      </ErrorBoundary>
    )

    expect(screen.getByText('页面出现异常')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '刷新页面' })).toBeEnabled()
    consoleError.mockRestore()
  })
})
