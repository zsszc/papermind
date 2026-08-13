# Batch 14 实施计划

## 1. 文件范围

- `frontend/package.json` / lock：Vitest、RTL、jest-dom、jsdom、MSW。
- `frontend/src/utils/sse.js`：从 ChatPanel 抽取流解析。
- `frontend/src/test/` 与 `*.test.jsx`：统一测试 setup 与 React/SSE 契约。
- `electron/backend-lifecycle.js`：健康探测、等待与重启决策。
- `electron/test/`、`electron/package.json`：Node test Harness。
- `.github/workflows/ci.yml`：前端和 Electron 测试 Gate。

## 2. 顺序

1. 记录 Node/npm 与现有零测试基线，提交 SDD。
2. RED：SSE 跨 chunk/abort 与 ErrorBoundary 测试。
3. GREEN：引入前端 Harness、抽取 SSE，不改 UI 行为。
4. RED：Electron health/wait/restart/kill 判断测试。
5. GREEN：抽取依赖可注入模块并让 main.js 复用。
6. 全量回归、安全审计、测试报告、提交推送。

## 3. 风险

- 测试依赖升级污染运行依赖：全部放 devDependencies，production bundle 不引入。
- SSE 重构改变顺序：用现有行为契约和边界测试锁定。
- Electron import 触发 GUI/日志：纯模块不得 require electron，不直接 import main.js。
