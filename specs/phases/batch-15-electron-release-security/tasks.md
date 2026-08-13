# Batch 15 TDD 任务

- [x] H0：确认 Electron 6 个测试基线和 13 项 high+ 漏洞基线。
- [x] T1 RED：窗口、导航/弹窗/权限与单实例策略测试失败。
- [x] T2 GREEN：实现安全策略、CSP 与主进程接线。
- [x] T3 RED：制品必需文件、禁止路径和密钥模式测试失败。
- [x] T4 GREEN：实现制品扫描器并收紧 builder 资源规则。
- [x] T5：升级 Electron 43 / builder 26，清零官方 audit。
- [ ] T6：实际构建 unpacked 应用并通过制品扫描（运行时官方资产下载受当前网络阻断；安全 Gate 正确拒绝摘要不匹配文件）。
- [x] T7：全量回归、测试报告、计划台账、提交并推送。
