# Batch 14 规格：前端与 Electron 自动化 Harness

## 1. 背景

后端已有 505 个 pytest 用例，但前端和 Electron 自动化测试为 0。Chat SSE 解析、API 请求与桌面后端进程生命周期只能靠人工验证，直接升级 Electron 29 → 43 风险过高。

## 2. 行为规格

### S1：前端测试运行器

- `npm test` 使用 Vitest + jsdom，支持 React Testing Library 与 jest-dom。
- MSW 作为网络契约测试工具安装，测试不得连接真实后端。
- CI 在 lint/build 前运行前端测试。

### S2：SSE 解析

- 解析器脱离 React 组件成为纯可测试模块。
- 支持跨 chunk、CRLF、`data:` 可选空格、多行 data、delta/finished/error。
- 非法 JSON 只报告 warning，不中断后续事件。
- AbortError 时取消 reader 并重新抛出；完成回调只调用一次。

### S3：React 错误边界

- 子组件抛错时显示稳定降级 UI，不让整页空白。
- 测试环境不得依赖 AntD 网络或真实 API。

### S4：Electron 生命周期 Harness

- 使用 Node 内置 `node:test`，不启动真实 Electron GUI。
- 健康探测可注入 request/timer/clock，覆盖 200、非 200、网络错误与超时。
- 后端等待覆盖立即成功、重试成功和超时；测试使用假时钟，不真实 sleep。
- 自动重启节流、主动退出和进程仍存活判断由纯函数表达并测试。
- `main.js` 复用已测试模块，不改变本批生产端口、启动参数或退出语义。

## 3. 验收标准

1. 前端与 Electron 至少各一组 RED 后 GREEN。
2. `npm test`、frontend lint/build/audit、electron test/node-check 全部执行。
3. 后端全量 pytest 不回退。
4. CI 纳入前端测试与 Electron node:test。
5. 生成测试报告并更新计划台账；Electron 漏洞仍由 Batch 15 修复。
