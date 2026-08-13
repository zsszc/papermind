/** 读取 PaperMind SSE 响应，支持跨 chunk、CRLF、多行 data 与主动取消。 */
export async function readSSEStream(
  response,
  onDelta,
  onFinish,
  onError,
  { warning = console.warn } = {}
) {
  const reader = response.body.getReader()
  const decoder = new TextDecoder('utf-8')
  let buffer = ''
  let finished = false
  let lastCitations = []

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
      if (data.delta) onDelta(data.delta)
      if (data.error) {
        finished = true
        onError?.(data.error)
      }
      if (data.finished) {
        finished = true
        lastCitations = data.citations || []
      }
    } catch (error) {
      warning('[SSE] JSON 解析失败，已跳过该事件：', error, payload)
    }
  }

  try {
    while (!finished) {
      const { done, value } = await reader.read()
      if (done) break
      buffer += decoder.decode(value, { stream: true })
      const events = buffer.split(/\r?\n\r?\n/)
      buffer = events.pop()
      for (const event of events) {
        handleEvent(event)
        if (finished) break
      }
    }
    if (!finished && buffer.trim()) handleEvent(buffer)
    onFinish(lastCitations)
  } catch (error) {
    if (error.name === 'AbortError') {
      try {
        await reader.cancel()
      } catch {
        // reader 可能已经关闭，取消失败不覆盖原始 AbortError。
      }
    }
    throw error
  }
}
