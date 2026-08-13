/** 创建串行 latest-wins 保存队列：在途请求完成后只保存最新待写值。 */
export function createLatestSaveQueue(saveValue) {
  let activeValue
  let pendingValue
  let lastSavedValue
  let runningPromise = null
  let dirty = false

  const drain = async () => {
    try {
      while (pendingValue !== undefined) {
        activeValue = pendingValue
        pendingValue = undefined
        await saveValue(activeValue)
        lastSavedValue = activeValue
        activeValue = undefined
      }
      dirty = false
    } catch (error) {
      activeValue = undefined
      dirty = true
      throw error
    } finally {
      runningPromise = null
    }
  }

  const save = (value) => {
    dirty = value !== lastSavedValue
    if (!dirty && !runningPromise) return Promise.resolve()
    if (runningPromise && value === activeValue && pendingValue === undefined) {
      return runningPromise
    }
    pendingValue = value
    if (!runningPromise) runningPromise = drain()
    return runningPromise
  }

  return {
    save,
    flush(value) {
      return save(value)
    },
    isDirty() {
      return dirty
    },
    markSaved(value) {
      lastSavedValue = value
      dirty = pendingValue !== undefined || activeValue !== undefined
    },
    getLastSavedValue() {
      return lastSavedValue
    },
  }
}
