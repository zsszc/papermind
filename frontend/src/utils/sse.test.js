import { describe, expect, it, vi } from 'vitest'

import { IncompleteSSEError, SSEProtocolError, SSETimeoutError, readSSEStream } from './sse'

function responseFromChunks(chunks) {
  const encoder = new TextEncoder()
  let index = 0
  const cancel = vi.fn()
  const releaseLock = vi.fn()
  return {
    body: {
      getReader: () => ({
        read: vi.fn(async () => {
          if (index >= chunks.length) return { done: true, value: undefined }
          return { done: false, value: encoder.encode(chunks[index++]) }
        }),
        cancel,
        releaseLock,
      }),
    },
    cancel,
    releaseLock,
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
    expect(onFinish).toHaveBeenCalledWith(
      [{ paper_id: 1 }],
      expect.objectContaining({ finished: true }),
    )
    expect(response.cancel).toHaveBeenCalledOnce()
    expect(response.releaseLock).toHaveBeenCalledOnce()
  })

  it('finished 将后端清洗正文作为原子终态传给调用方', async () => {
    const response = responseFromChunks([
      'data: {"delta":"原始[^9^]"}\n\n',
      'data: {"finished":true,"content":"清洗后","citations":[],"verification":{"removed":1}}\n\n',
    ])
    const onFinish = vi.fn()

    await readSSEStream(response, vi.fn(), onFinish)

    expect(onFinish).toHaveBeenCalledWith(
      [],
      expect.objectContaining({ content: '清洗后', verification: { removed: 1 } }),
    )
  })

  it('finished citations 类型畸形时拒绝进入成功态', async () => {
    const response = responseFromChunks([
      'data: {"finished":true,"content":"答案","citations":"private-path"}\n\n',
    ])
    const onFinish = vi.fn()

    await expect(readSSEStream(response, vi.fn(), onFinish)).rejects.toBeInstanceOf(
      SSEProtocolError
    )
    expect(onFinish).not.toHaveBeenCalled()
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
    expect(onFinish).not.toHaveBeenCalled()
    expect(response.cancel).toHaveBeenCalledOnce()
    expect(response.releaseLock).toHaveBeenCalledOnce()
  })

  it('非法 JSON 只 warning，继续处理下一事件', async () => {
    const response = responseFromChunks([
      'data: not-json\n\ndata: {"delta":"继续"}\n\n',
    ])
    const warning = vi.fn()
    const onDelta = vi.fn()

    await expect(
      readSSEStream(response, onDelta, vi.fn(), undefined, { warning })
    ).rejects.toBeInstanceOf(IncompleteSSEError)

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
          releaseLock: vi.fn(),
        }),
      },
    }

    await expect(readSSEStream(response, vi.fn(), vi.fn())).rejects.toBe(abortError)
    expect(cancel).toHaveBeenCalledOnce()
  })

  it('响应体缺失时拒绝读取', async () => {
    await expect(readSSEStream({ body: null }, vi.fn(), vi.fn())).rejects.toBeInstanceOf(
      SSEProtocolError
    )
  })

  it('未收到 finished 就 EOF 时报不完整流，不调用 finish', async () => {
    const response = responseFromChunks(['data: {"delta":"半个答案"}\n\n'])
    const onFinish = vi.fn()

    await expect(readSSEStream(response, vi.fn(), onFinish)).rejects.toBeInstanceOf(
      IncompleteSSEError
    )

    expect(onFinish).not.toHaveBeenCalled()
    expect(response.releaseLock).toHaveBeenCalledOnce()
  })

  it('error 与 finished 同帧时 error 优先，两个回调互斥', async () => {
    const response = responseFromChunks([
      'data: {"error":"失败","finished":true,"citations":[{"paper_id":1}]}\n\n',
    ])
    const onFinish = vi.fn()
    const onError = vi.fn()

    await readSSEStream(response, vi.fn(), onFinish, onError)

    expect(onError).toHaveBeenCalledOnce()
    expect(onFinish).not.toHaveBeenCalled()
  })

  it('同一批次重复 finished 时仅接受首个终态', async () => {
    const response = responseFromChunks([
      'data: {"finished":true,"content":"first","citations":[]}\n\n' +
      'data: {"finished":true,"content":"second","citations":[{"paper_id":2}]}\n\n',
    ])
    const onFinish = vi.fn()

    await readSSEStream(response, vi.fn(), onFinish)

    expect(onFinish).toHaveBeenCalledOnce()
    expect(onFinish.mock.calls[0][1].content).toBe('first')
  })

  it('首事件超过预算时抛出可区分的超时并释放 reader', async () => {
    vi.useFakeTimers()
    const cancel = vi.fn()
    const releaseLock = vi.fn()
    const response = {
      body: { getReader: () => ({ read: vi.fn(() => new Promise(() => {})), cancel, releaseLock }) },
    }
    const pending = readSSEStream(response, vi.fn(), vi.fn(), vi.fn(), {
      firstEventTimeoutMs: 50,
      idleTimeoutMs: 100,
      totalTimeoutMs: 1000,
    })
    const rejection = expect(pending).rejects.toMatchObject({
      name: 'SSETimeoutError', kind: '首事件',
    })

    await vi.advanceTimersByTimeAsync(50)
    await rejection
    expect(cancel).toHaveBeenCalledOnce()
    expect(releaseLock).toHaveBeenCalledOnce()
    vi.useRealTimers()
  })

  it('持续收到数据会续租空闲预算，但仍受总时长限制', async () => {
    vi.useFakeTimers()
    const encoder = new TextEncoder()
    let reads = 0
    const response = {
      body: {
        getReader: () => ({
          read: vi.fn(() => {
            reads += 1
            if (reads <= 2) {
              return new Promise((resolve) => setTimeout(
                () => resolve({ done: false, value: encoder.encode(`data: {"delta":"${reads}"}\n\n`) }),
                40
              ))
            }
            return new Promise(() => {})
          }),
          cancel: vi.fn(),
          releaseLock: vi.fn(),
        }),
      },
    }
    const pending = readSSEStream(response, vi.fn(), vi.fn(), vi.fn(), {
      firstEventTimeoutMs: 50,
      idleTimeoutMs: 50,
      totalTimeoutMs: 110,
    })
    const rejection = expect(pending).rejects.toBeInstanceOf(SSETimeoutError)

    await vi.advanceTimersByTimeAsync(120)
    await rejection
    vi.useRealTimers()
  })
})
