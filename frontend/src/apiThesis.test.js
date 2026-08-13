import { describe, expect, it, vi } from 'vitest'

import api, { suggestCitations } from './api'

describe('suggestCitations', () => {
  it('正文只进入 JSON body，URL 不含段落', async () => {
    const post = vi.spyOn(api, 'post').mockResolvedValue({ data: {} })
    const paragraph = '中文 & ? # \n长段落'
    const signal = new AbortController().signal

    await suggestCitations(7, paragraph, { signal })

    expect(post).toHaveBeenCalledWith(
      '/thesis/7/suggest-citations',
      { paragraph },
      { signal }
    )
    expect(post.mock.calls[0][0]).not.toContain(encodeURIComponent(paragraph))
  })
})
