import { describe, expect, it, vi } from 'vitest'

import { createLatestSaveQueue } from './latestSaveQueue'

function deferred() {
  let resolve
  let reject
  const promise = new Promise((res, rej) => {
    resolve = res
    reject = rej
  })
  return { promise, resolve, reject }
}

describe('createLatestSaveQueue', () => {
  it('在旧保存进行中合并中间值并保证最新值最后落盘', async () => {
    const first = deferred()
    const save = vi.fn((value) => (value === 'A' ? first.promise : Promise.resolve()))
    const queue = createLatestSaveQueue(save)

    const a = queue.save('A')
    const b = queue.save('B')
    const c = queue.save('C')
    expect(save).toHaveBeenCalledTimes(1)

    first.resolve()
    await Promise.all([a, b, c])

    expect(save.mock.calls.map(([value]) => value)).toEqual(['A', 'C'])
    expect(queue.isDirty()).toBe(false)
  })

  it('失败会拒绝对应等待者，但仍允许更新内容重试', async () => {
    const save = vi.fn()
      .mockRejectedValueOnce(new Error('offline'))
      .mockResolvedValueOnce(undefined)
    const queue = createLatestSaveQueue(save)

    await expect(queue.save('A')).rejects.toThrow('offline')
    expect(queue.isDirty()).toBe(true)
    await queue.save('B')

    expect(save.mock.calls.map(([value]) => value)).toEqual(['A', 'B'])
    expect(queue.isDirty()).toBe(false)
  })

  it('flush 可等待当前队列完成且不重复保存同一内容', async () => {
    const pending = deferred()
    const save = vi.fn(() => pending.promise)
    const queue = createLatestSaveQueue(save)

    const saving = queue.save('最终内容')
    const flushing = queue.flush('最终内容')
    pending.resolve()
    await Promise.all([saving, flushing])

    expect(save).toHaveBeenCalledOnce()
  })

  it('markSaved 建立初始快照，避免未编辑内容被重复写入', async () => {
    const save = vi.fn().mockResolvedValue(undefined)
    const queue = createLatestSaveQueue(save)
    queue.markSaved('服务端内容')

    await queue.flush('服务端内容')

    expect(save).not.toHaveBeenCalled()
    expect(queue.getLastSavedValue()).toBe('服务端内容')
  })
})
