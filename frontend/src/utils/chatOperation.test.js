import { describe, expect, it } from 'vitest'

import {
  beginChatOperation,
  finishChatOperation,
  updateMessageByIdentity,
} from './chatOperation'


describe('chat operation gate', () => {
  it('同步拒绝第二个在途操作，且旧 operation 不能清空新 controller', () => {
    const ref = { current: null }
    const first = new AbortController()
    const second = new AbortController()

    expect(beginChatOperation(ref, first)).toBe(true)
    expect(beginChatOperation(ref, second)).toBe(false)
    expect(finishChatOperation(ref, second)).toBe(false)
    expect(ref.current).toBe(first)
    expect(finishChatOperation(ref, first)).toBe(true)
    expect(ref.current).toBe(null)
  })
})


describe('updateMessageByIdentity', () => {
  it('按 tempId 更新目标消息，不会污染后来追加的 assistant', () => {
    const messages = [
      { role: 'assistant', tempId: 'target', content: '' },
      { role: 'assistant', tempId: 'other', content: '其他会话' },
    ]

    const next = updateMessageByIdentity(messages, { tempId: 'target' }, { content: '目标增量' })

    expect(next[0].content).toBe('目标增量')
    expect(next[1].content).toBe('其他会话')
  })

  it('重新生成按持久化 message id 更新，不依赖数组 index', () => {
    const messages = [
      { id: 9, role: 'assistant', content: '插入项' },
      { id: 4, role: 'assistant', content: '旧答案' },
    ]

    const next = updateMessageByIdentity(messages, { id: 4 }, { content: '新答案' })

    expect(next[0].content).toBe('插入项')
    expect(next[1].content).toBe('新答案')
  })
})
