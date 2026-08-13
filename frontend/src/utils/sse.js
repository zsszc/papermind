/** SSE 响应没有可读 body 或违反协议。 */
export class SSEProtocolError extends Error {
  constructor(message) {
    super(message)
    this.name = 'SSEProtocolError'
  }
}


/** 流在收到 finished/error 终态前结束。 */
export class IncompleteSSEError extends Error {
  constructor(message = 'SSE 连接在完成事件前中断') {
    super(message)
    this.name = 'IncompleteSSEError'
  }
}

/** SSE 首事件、空闲或总时长超过预算。 */
export class SSETimeoutError extends Error {
  constructor(kind) {
    super(`SSE ${kind}超时`)
    this.name = 'SSETimeoutError'
    this.kind = kind
  }
}

function readWithTimeout(reader, timeoutMs, kind) {
  if (!Number.isFinite(timeoutMs) || timeoutMs <= 0) return reader.read()
  let timer
  return Promise.race([
    reader.read(),
    new Promise((_, reject) => {
      timer = setTimeout(() => reject(new SSETimeoutError(kind)), timeoutMs)
    }),
  ]).finally(() => clearTimeout(timer))
}


/** 读取 PaperMind SSE 响应，支持跨 chunk、CRLF、多行 data 与主动取消。 */
export async function readSSEStream(
  response,
  onDelta,
  onFinish,
  onError,
  {
    warning = console.warn,
    firstEventTimeoutMs = 60_000,
    idleTimeoutMs = 180_000,
    totalTimeoutMs = 600_000,
  } = {}
) {
  if (!response?.body || typeof response.body.getReader !== 'function') {
    throw new SSEProtocolError('SSE 响应体不可读')
  }

  const reader = response.body.getReader()
  const decoder = new TextDecoder('utf-8')
  let buffer = ''
  let terminal = null
  let lastCitations = []
  let receivedChunk = false
  const deadline = Date.now() + totalTimeoutMs

  const handleEvent = (eventText) => {
    const dataLines = []
    for (const rawLine of eventText.split('\n')) {
      const line = rawLine.replace(/\r$/, '')
      if (line.startsWith('data:')) {
        dataLines.push(line.startsWith('data: ') ? line.slice(6) : line.slice(5))
      }
    }
    if (!dataLines.length) return
    const payload = dataLines.join('\n')
    try {
      const data = JSON.parse(payload)
      // error 是独立失败终态，与 finished 同帧时不得再触发成功回调。
      if (data.error) {
        terminal = 'error'
        onError?.(data.error)
        return
      }
      if (data.delta) onDelta(data.delta)
      if (data.finished) {
        terminal = 'finished'
        lastCitations = data.citations || []
      }
    } catch (error) {
      warning('[SSE] JSON 解析失败，已跳过该事件：', error, payload)
    }
  }

  try {
    while (!terminal) {
      const remainingTotal = deadline - Date.now()
      if (remainingTotal <= 0) throw new SSETimeoutError('总时长')
      const eventBudget = receivedChunk ? idleTimeoutMs : firstEventTimeoutMs
      const timeoutKind = remainingTotal <= eventBudget
        ? '总时长'
        : receivedChunk ? '空闲' : '首事件'
      const { done, value } = await readWithTimeout(
        reader,
        Math.min(eventBudget, remainingTotal),
        timeoutKind
      )
      if (done) break
      receivedChunk = true
      buffer += decoder.decode(value, { stream: true })
      const events = buffer.split(/\r?\n\r?\n/)
      buffer = events.pop()
      for (const event of events) {
        handleEvent(event)
        if (terminal) break
      }
    }
    if (!terminal && buffer.trim()) handleEvent(buffer)
    if (!terminal) throw new IncompleteSSEError()
    if (terminal === 'finished') onFinish(lastCitations)
  } finally {
    // 已读到应用终态时主动停止底层流；Abort/网络异常也走同一释放路径。
    try {
      await reader.cancel()
    } catch {
      // reader 可能已关闭，释放失败不覆盖主错误。
    }
    try {
      reader.releaseLock?.()
    } catch {
      // 某些测试/运行时 reader 不实现显式锁释放。
    }
  }
}
