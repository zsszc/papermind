/**
 * 对话操作的同步单飞门禁。
 * React state 的更新不是同步锁，因此发送/重生成必须在任何 await 前占用 ref。
 */
export function beginChatOperation(operationRef, controller) {
  if (operationRef.current) return false
  operationRef.current = controller
  return true
}


/** 只允许当前操作释放自己的槽位，避免旧 finally 清空新 controller。 */
export function finishChatOperation(operationRef, controller) {
  if (operationRef.current !== controller) return false
  operationRef.current = null
  return true
}


/** 按稳定 id/tempId 更新消息，不依赖“最后一条”或可漂移的数组 index。 */
export function updateMessageByIdentity(messages, identity, patch) {
  const index = messages.findIndex((item) => (
    identity.id != null ? item.id === identity.id : item.tempId === identity.tempId
  ))
  if (index < 0) return messages
  const next = [...messages]
  next[index] = { ...next[index], ...patch }
  return next
}
