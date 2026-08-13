# Batch 19 实施计划

## 1. 范围

- `frontend/src/components/ChatPanel.jsx`、`utils/sse.js`、新增 operation/timeout 工具及 RTL 测试。
- `frontend/src/pages/WritingDesk.jsx`、`PaperDetail.jsx`、`api.js` 及组件测试。
- `backend/app/routers/thesis.py`、`papers.py`、`schemas.py` 与路由/文件原子写测试。
- `backend/eval/run.py`、`services/llm.py` 与生成预算、有效性、生产语义 profile 测试。
- `docs/test-reports/`、开发计划台账与 `AGENTS.md`。

## 2. TDD 微循环

1. 先写 SSE body/EOF/终态/取消 RED，完成协议层 GREEN。
2. 写 Chat single-flight、signal、卸载、目标消息/会话隔离 RED，再修组件与 API helper。
3. 写 thesis JSON schema/长度/不回显正文 RED，再迁移前后端契约。
4. 写草稿恢复、按论文隔离、取消/乱序 RED，再修 WritingDesk。
5. 写笔记 404/上限/原子失败保旧内容 RED，再实现服务端；随后写保存队列/flush 组件 RED/GREEN。
6. 写生成样本白名单、预算、health、错误有效性和 `semantic-production` RED/GREEN。
7. 只用 private dev 做生产语义基线与固定 4 QA 生成烟测；不触碰 holdout。
8. 每个稳定 GREEN 节点独立提交；最后跑后端、前端、Electron、公开 RAG、npm audit 和真实健康 Gate。

## 3. 风险控制

- 私有答案和论文正文不得写入 Git；生成报告只进 `backend/eval/private/`。
- 真实 LLM 调用固定 QA 白名单与硬预算；任何重试仍由 `llm_service` 内部处理，CLI 不额外重放题目。
- 不在本批切换 holdout 或生产默认 reranker；所有检索提升先作为 dev profile 记录。
- 笔记兼容已有 form body，迁移期间不把正文放到 URL；服务端先保证旧客户端安全。
