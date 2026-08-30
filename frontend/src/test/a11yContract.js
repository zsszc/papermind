import { within } from '@testing-library/react'
import { expect } from 'vitest'

// Batch 24 / T2 可访问性契约断言组：新交互组件上架前必须过同一断言组。
// 可访问名称由 Testing Library 按 WAI-ARIA accName 规则计算，与 getByRole 行为一致。
const NON_EMPTY_NAME = /\S/

function hasExplicitTextLabel(root, el) {
  // placeholder 不算合规名称（输入后即消失，WCAG 不认可为标签）；
  // 必须有 aria-label / aria-labelledby / <label> 关联之一。
  if (el.getAttribute('aria-label')?.trim()) return true
  const labelledby = el.getAttribute('aria-labelledby')
  if (
    labelledby
      ?.split(/\s+/)
      .some((id) => root.ownerDocument.getElementById(id)?.textContent?.trim())
  ) {
    return true
  }
  const labels = root.ownerDocument.querySelectorAll('label[for]')
  for (const label of labels) {
    if (el.id && label.getAttribute('for') === el.id) return true
  }
  if (el.closest('label')) return true
  return false
}

/**
 * 收集容器内的可访问性违规：
 * - unnamedButtons：缺少可访问名称的按钮（含 icon-only 按钮）
 * - unfocusableButtons：tabIndex < 0 导致无法键盘聚焦的按钮（disabled 除外）
 * - unlabeledTextboxes：仅有 placeholder、无显式标签关联的文本输入框
 */
export function collectA11yViolations(root) {
  const scope = within(root)
  const buttons = scope.queryAllByRole('button')
  const namedButtons = new Set(scope.queryAllByRole('button', { name: NON_EMPTY_NAME }))
  const unnamedButtons = buttons.filter((el) => !namedButtons.has(el))
  const unfocusableButtons = buttons.filter((el) => !el.disabled && el.tabIndex < 0)
  const textboxes = [...scope.queryAllByRole('textbox'), ...scope.queryAllByRole('searchbox')]
  const unlabeledTextboxes = textboxes.filter((el) => !hasExplicitTextLabel(root, el))
  return { unnamedButtons, unfocusableButtons, unlabeledTextboxes }
}

function htmlOf(elements) {
  return elements.map((el) => el.outerHTML.slice(0, 160)).join('\n')
}

/** 断言容器内零可访问性违规；违规时输出 offending 元素便于定位。 */
export function expectA11yContract(root) {
  const violations = collectA11yViolations(root)
  expect(
    violations.unnamedButtons,
    `缺少可访问名称的按钮:\n${htmlOf(violations.unnamedButtons)}`
  ).toHaveLength(0)
  expect(
    violations.unfocusableButtons,
    `无法键盘聚焦的按钮（tabIndex < 0）:\n${htmlOf(violations.unfocusableButtons)}`
  ).toHaveLength(0)
  expect(
    violations.unlabeledTextboxes,
    `缺少显式标签关联的输入框（placeholder 不算合规名称）:\n${htmlOf(violations.unlabeledTextboxes)}`
  ).toHaveLength(0)
}
