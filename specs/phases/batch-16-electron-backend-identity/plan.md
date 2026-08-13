# Batch 16 实施计划

## 1. 文件范围

- `backend/app/core/capability.py`、`backend/app/main.py`：能力校验中间件与 health 实例 ID。
- `backend/tests/test_capability.py`：开发兼容、401、OPTIONS、static/health 边界。
- `electron/runtime-identity.js`、`backend-lifecycle.js`、`main.js`、`preload.js`：身份生成、端口选择、probe、spawn 与 IPC。
- `electron/test/`：纯 Node 端口/身份/IPC/lifecycle 测试。
- `frontend/src/utils/apiUrl.js`、`api.js`、原生 fetch/PDF 消费方、`main.jsx`：异步配置和统一能力头。
- `frontend/index.html`：随机回环端口 CSP。
- `electron/scripts/smoke-backend-identity.js`：真实回环子进程集成 smoke。

## 2. 顺序

1. 记录固定 8000 + 任意 200 的攻击基线，提交 SDD。
2. RED/GREEN：后端能力 ASGI 中间件、OPTIONS 豁免与身份 health。
3. RED/GREEN：Electron 身份生成、随机端口与严格 JSON health probe。
4. RED/GREEN：preload IPC 与前端异步配置、axios/raw fetch/PDF 统一能力头。
5. 建立无需 LLM 的真实回环 smoke，验证正确/错误令牌、实例匹配和端口释放。
6. 全量回归、audit、报告、台账、分批提交并推送。

## 3. 设计约束

- 令牌只存在内存和子进程环境；禁止 CLI 参数、query/hash、localStorage、日志与磁盘持久化。
- 中间件使用纯 ASGI 包装，避免 BaseHTTPMiddleware 对 SSE 流式响应的缓冲/取消语义。
- 后端读取环境变量在请求时完成，使 pytest 可通过 monkeypatch 覆盖且无需重载全局 app。
- 随机端口存在“探测后到 spawn 前”的竞争窗口，实例 ID 与令牌确保竞争者不会被误认；启动失败只能重试或报错，不能降级复用未知服务。
- CSP 端口通配仅限 `http://127.0.0.1:*`，不允许 `http:*` 或外部域名。

