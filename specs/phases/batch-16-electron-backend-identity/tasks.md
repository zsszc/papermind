# Batch 16 TDD 任务

- [x] H0：确认生产端固定 8000 且任意 health 200 会被接受。
- [x] T1 RED：后端正确/错误/缺失令牌、OPTIONS、static 与 instance health 测试失败。
- [x] T2 GREEN：实现纯 ASGI 能力中间件与 health 身份。
- [x] T3 RED：Electron 随机端口、身份生成、严格 probe 与环境注入测试失败。
- [x] T4 GREEN：实现 runtime identity/lifecycle/main IPC 接线。
- [x] T5 RED：前端异步运行配置与统一能力头测试失败。
- [x] T6 GREEN：适配 axios、SSE、图片、论文建议、PDF 加载/下载与 CSP。
- [ ] T7：真实回环 identity smoke、全量回归与 audit。
- [ ] T8：测试报告、计划台账、提交并推送。
