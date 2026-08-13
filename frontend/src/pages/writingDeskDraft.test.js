import { describe, expect, it } from 'vitest'

import { readWritingDeskState, writeWritingDeskState } from './writingDeskDraft'

describe('WritingDesk 草稿存储', () => {
  it('同步恢复选中论文及按论文隔离的草稿', () => {
    const storage = {
      getItem: () => JSON.stringify({
        selectedThesis: 2,
        drafts: { 1: '论文一草稿', 2: '论文二草稿' },
      }),
    }

    expect(readWritingDeskState(storage)).toEqual({
      selectedThesis: 2,
      drafts: { 1: '论文一草稿', 2: '论文二草稿' },
    })
  })

  it('兼容旧版单段落存储', () => {
    const storage = {
      getItem: () => JSON.stringify({ selectedThesis: 7, paragraph: '旧草稿' }),
    }

    expect(readWritingDeskState(storage)).toEqual({
      selectedThesis: 7,
      drafts: { 7: '旧草稿' },
    })
  })

  it('写入时保留每篇论文的草稿', () => {
    const calls = []
    const storage = { setItem: (...args) => calls.push(args) }

    writeWritingDeskState(storage, {
      selectedThesis: 2,
      drafts: { 1: 'A', 2: 'B' },
    })

    expect(JSON.parse(calls[0][1])).toEqual({
      selectedThesis: 2,
      drafts: { 1: 'A', 2: 'B' },
    })
  })
})
