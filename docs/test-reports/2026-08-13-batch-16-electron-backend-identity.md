# Batch 16 测试报告：Electron 后端身份边界（2026-08-13）

## 1. 结论

Batch 16 已消除生产桌面端固定连接 `127.0.0.1:8000`、仅凭任意 health 200 复用本机服务的身份混淆风险。每次 Electron 完整启动现在生成随机非特权回环端口、256-bit 能力令牌和 UUID 实例 ID；后端重启沿用本次身份，退出重启时轮换。令牌只经子进程环境与受限 IPC 传递，不进入 URL、命令行、日志或磁盘。

后端在存在 `PAPERMIND_API_TOKEN` 时统一保护 API、静态文件、文档与 MCP，仅 CORS `OPTIONS` 预检豁免。Electron readiness 同时验证令牌、JSON `status=ok` 与实例 ID；错误 JSON、错误实例、错误令牌和未知 200 服务均不能通过。前端 axios、SSE、图片分析、论文建议、React-PDF 与下载全部统一携带能力头；开发模式仍保持 8000 和无令牌兼容。

真实回环 smoke 已实际拉起 Uvicorn 并通过。Batch 16 工程 Gate 为 **PASS**。Batch 15 的新安装包 Gate 仍受官方可移植 Python 资产下载问题影响，不能据此宣称新桌面制品已完成发布验收。

## 2. Harness / SDD / TDD 证据

| 环节 | RED | GREEN |
|---|---|---|
| 后端能力边界 | 3 fail / 1 pass：health 无实例 ID，缺失/错误令牌仍为 200，static 未保护 | 纯 ASGI 中间件；正确令牌通过，错误/缺失令牌 401，OPTIONS 兼容，static/docs/MCP 同边界 |
| 拒绝路径兼容 | Electron `Origin: null` 的 401 缺少 CORS 响应头 | 调整中间件顺序，未授权响应仍可被渲染器读取；非 ASCII 原始 header 安全拒绝 |
| Electron 身份 | 缺少随机身份模块、严格 probe 与 URL 构造；旧 health 只看状态码 | 随机端口、32-byte token、UUID、严格 JSON/实例握手、环境净化与精确入口 IPC |
| 生命周期审查 | 发现 `backendProcess.pid` 未定义会导致生产启动崩溃；共享主动退出布尔值存在旧/新进程竞态 | 按具体子进程跟踪；启动 promise 合并；readiness 失败回收进程；旧进程迟到退出不清除新进程 |
| 前端运行配置 | 新增 4 项测试全部失败：无异步配置、能力头、PDF 资源对象与非法配置拒绝 | 统一 runtime config、axios/raw fetch/PDF/download 适配；file 模式配置非法时 fail-close |
| 真实回环 | 沙箱内监听 `127.0.0.1` 首次为 `EPERM` | 授权后 PASS：无/错令牌 401、正确身份 200、伪实例失败、退出后端口释放 |

规格、计划与任务位于 `specs/phases/batch-16-electron-backend-identity/`。本批按可回退行为保留 6 个实现前提交：`d991874`、`a20532b`、`e4f3879`、`eddd2ff`、`37eded8`、`3bf29f5`。

## 3. 最终自动化 Gate

| 门禁 | 结果 |
|---|---|
| 后端 pytest | **511 passed**，932 warnings，13.74s |
| 后端 `pip check` | No broken requirements found |
| 后端 Ruff | **未运行：当前 venv 未安装 ruff**；CI 现状也尚无 Ruff 步骤，列入后续 Harness 债务 |
| 前端 Vitest | **11 passed / 4 files** |
| 前端 lint | 通过，零 warning |
| 前端 build | 通过；保留既有 ui/StatsPage 大 chunk warning |
| 前端官方 npm audit | **0 vulnerabilities** |
| Electron node:test | **26 passed** |
| Electron 主进程/生命周期/安全/smoke/制品脚本语法 | 全部通过 |
| Electron 官方 npm audit | **0 vulnerabilities** |
| 真实 Uvicorn identity smoke | **PASS** |

ErrorBoundary 测试会故意输出 React 错误栈，测试本身通过。两端 npm 默认镜像不实现 audit，改用 `https://registry.npmjs.org` 后均为 0。

## 4. RAG 指标回归

本批没有修改检索、分块、排序、提示词或生成策略，因此只复跑 Batch 13 公开冻结集，不宣称模型质量提升。

| Profile | Recall@5 | MRR | NDCG@5 | P95 | 相对 Batch 13 |
|---|---:|---:|---:|---:|---|
| count | **0.900** | 0.775 | 0.806 | 0.3ms | 无回退 |
| BM25 | **0.900** | 0.783 | 0.813 | 0.5ms | 指标无回退；延迟绝对差异不足 1ms |

公开集只有 3 篇原创合成论文、12 条 QA，作用是可复现回归，不代表用户真实文献库质量。真实 RAG 提升仍按 Batch 20–23 的单变量消融路线推进。

## 5. 已知限制与下一批

- 随机端口存在探测 socket 关闭到 Uvicorn bind 的固有竞争窗口；能力令牌与实例 ID保证不会误认抢占者，当前策略为失败关闭，不降级复用未知服务。
- WebSocket 当前无业务路由；能力中间件保护 HTTP/SSE。若未来新增 WebSocket，必须在规格中增加握手令牌边界。
- Batch 15 的实际 unpacked/安装包验证仍待官方可移植 Python 运行时成功下载后重跑。
- 下一批 Batch 17：先对复制数据库建立 integrity dry-run/backup/repair Harness，再加入 PDF/DOCX 魔数与解压资源上限；未经用户确认不修改真实数据库。
