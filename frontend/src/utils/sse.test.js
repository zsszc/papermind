import { describe, expect, it, vi } from 'vitest'

import { readSSEStream } from './sse'

function responseFromChunks(chunks) {
  const encoder = new TextEncoder()
  let index = 0
  const cancel = vi.fn()
  return {
    body: {
      getReader: () => ({
        read: vi.fn(async () => {
          if (index >= chunks.length) return { done: true, value: undefined }
          return { done: false, value: encoder.encode(chunks[index++]) }
        }),
        cancel,
      }),
    },
    cancel,
  }
}

describe('readSSEStream', () => {
  it('解析跨 chunk、CRLF 与 finished 引用', async () => {
    const response = responseFromChunks([
      'data: {"del',
      'ta":"你"}\r\n\r\ndata:{"delta":"好"}\n\n',
      'data: {"finished":true,"citations":[{"paper_id":1}]}\n\n',
    ])
    const onDelta = vi.fn()
    const onFinish = vi.fn()

    await readSSEStream(response, onDelta, onFinish)

    expect(onDelta.mock.calls.flat()).toEqual(['你', '好'])
    expect(onFinish).toHaveBeenCalledTimes(1)
    expect(onFinish).toHaveBeenCalledWith([{ paper_id: 1 }])
  })

  it('错误事件结束流并调用错误回调', async () => {
    const response = responseFromChunks([
      'data: {"error":"服务繁忙"}\n\ndata: {"delta":"不应读取"}\n\n',
    ])
    const onDelta = vi.fn()
    const onFinish = vi.fn()
    const onError = vi.fn()

    await readSSEStream(response, onDelta, onFinish, onError)

    expect(onError).toHaveBeenCalledWith('服务繁忙')
    expect(onDelta).not.toHaveBeenCalled()
    expect(onFinish).toHaveBeenCalledOnce()
  })

  it('非法 JSON 只 warning，继续处理下一事件', async () => {
    const response = responseFromChunks([
      'data: not-json\n\ndata: {"delta":"继续"}\n\n',
    ])
    const warning = vi.fn()
    const onDelta = vi.fn()

    await readSSEStream(response, onDelta, vi.fn(), undefined, { warning })

    expect(warning).toHaveBeenCalledOnce()
    expect(onDelta).toHaveBeenCalledWith('继续')
  })

  it('AbortError 时取消 reader 并重新抛出', async () => {
    const abortError = new DOMException('aborted', 'AbortError')
    const cancel = vi.fn()
    const response = {
      body: {
        getReader: () => ({
          read: vi.fn(async () => { throw abortError }),
          cancel,
        }),
      },
    }

    await expect(readSSEStream(response, vi.fn(), vi.fn())).rejects.toBe(abortError)
    expect(cancel).toHaveBeenCalledOnce()
  })
})
