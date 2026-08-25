import { describe, expect, it, vi } from 'vitest'

import api, { getBenchmarkV2Readiness } from './api'


describe('getBenchmarkV2Readiness', () => {
  it('使用固定只读端点并隔离全局错误提示', async () => {
    const get = vi.spyOn(api, 'get').mockResolvedValue({ data: {} })

    await getBenchmarkV2Readiness()

    expect(get).toHaveBeenCalledWith(
      '/readiness/benchmark-v2',
      { skipGlobalError: true },
    )
  })
})
