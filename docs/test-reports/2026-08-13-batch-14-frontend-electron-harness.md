# Batch 14 测试报告：前端与 Electron Harness（2026-08-13）

## 1. 结论

Batch 14 将前端自动测试从 0 提升到 5 个、Electron 从 0 提升到 6 个，并纳入主 CI。ChatPanel 的 SSE 解析已抽为纯模块；Electron health/wait/restart/kill 与环境净化已抽为不依赖 GUI 的纯生命周期模块。本批保持生产端口、启动参数和 UI 行为不变，为 Batch 15 的 Electron 大版本安全升级建立回归护栏。

## 2. 提交与行为

| 提交 | 内容 |
|---|---|
| `b71023d` | Batch 14 SDD spec/plan/tasks |
| `f9e0b34` | Vitest/RTL/MSW Harness、SSE parser、ErrorBoundary 测试 |
| `e97d71b` | Electron `node:test` 与 lifecycle 模块，main.js 复用 |
| `2aa0c24` | CI 加入前端与 Electron 测试 |
| `b9e5288` | Vitest 4.0.18 → 4.1.10，修复 critical advisory |

## 3. TDD / 安全 RED

### 前端 RED

首次 `npm test`：SSE 测试因 `src/utils/sse.js` 不存在而失败；ErrorBoundary JSX 测试也因运行约定失败。GREEN 后覆盖：

- SSE 跨 TCP chunk；
- CRLF 与 `data:` 可选空格；
- finished/citations 与 error；
- 非法 JSON 后继续；
- AbortError 取消 reader 并重抛；
- React 子树异常显示降级 UI。

### Electron RED

首次 `npm test`：`Cannot find module '../backend-lifecycle'`。GREEN 后覆盖：

- health 200/503、网络错误与请求超时；
- 立即成功、假时钟重试成功、等待超时；
- 主动退出、窗口销毁与 30 秒崩溃循环不重启；
- 进程存活判断；
- `PYTHONPATH/PYTHONHOME` 净化和数据目录注入。

### 依赖安全 RED

首次安装 Vitest 4.0.18 后，npm 官方审计发现 1 个 direct critical（Vitest UI server 任意文件读取/执行）。立即升级至 4.1.10，重跑 test/lint/build，官方 audit 恢复为 0。

## 4. 最终 Gate

| 门禁 | 结果 |
|---|---|
| 前端 Vitest | **5 passed / 2 files** |
| 前端 lint | 通过，零 warning |
| 前端 build | 通过；ui/StatsPage 大 chunk warning 保留 |
| 前端官方 npm audit | **0 vulnerabilities** |
| Electron node:test | **6 passed** |
| Electron main/lifecycle `node --check` | 通过 |
| 后端 pytest | **505 passed**，927 warnings |
| Python pip check | No broken requirements found |

ErrorBoundary 测试会故意抛出并由 React 输出两段错误栈，测试本身通过；这是预期噪声，后续可用测试渲染器的错误回调收敛输出。

## 5. 遗留风险与下一步

- Electron 官方 audit 仍为 **13 项：1 critical / 12 high**，本批未升级运行时依赖。
- 健康接口仍只判断固定 8000 端口的 HTTP 200，无法识别本机伪服务。
- 尚无 packaged GUI smoke；当前 Harness 只覆盖可在 CI 确定运行的纯生命周期边界。
- 下一批 Batch 15：Electron 43 + electron-builder 26.15.3、安全策略（CSP/导航/window.open/权限/single-instance）和安装包资源扫描；随机端口与能力令牌留在 Batch 16。
