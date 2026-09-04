# Batch 33 规格：前端 chunk 与统计页性能收尾

## 1. 背景

Batch 32 最终回归显示 `ui` 与 `StatsPage` 两个 JS chunk 分别约 1.12MB/1.14MB，超过 Vite
600KB 警戒线。当前 `manualChunks` 把所有 Ant Design 依赖强制聚合到一个文件，削弱了页面懒加载；
统计页则通过完整 ECharts 入口加载未使用图表。

## 2. 冻结目标

1. 除独立 PDF worker 外，每个生产 JS chunk 原始大小不得超过 600KiB；PDF worker 保持 1100KiB 上限。
2. StatsPage 只注册实际使用的 Bar/Pie/Graph、Title/Tooltip/Grid 与 CanvasRenderer。
3. 移除全量 Ant Design 人工聚合，让构建器按动态页面边界共享依赖；不得仅调高警告阈值。
4. 统计页 WAIT/PASS/错误/空库隐私行为与关键页面懒加载保持不变。
5. 新增可在 CI 复用的确定性 chunk budget Gate，缺少构建产物或超限均失败。

## 3. 验收标准

- [x] AC1：旧构建被 chunk Gate 明确拒绝，记录超限文件与大小。
- [x] AC2：新构建所有普通 JS ≤600KiB，PDF worker ≤1100KiB。
- [x] AC3：统计页现有测试与新增模块化契约通过，未扩大初始页面同步依赖。
- [x] AC4：前端测试/lint/build、后端、Electron、发布 E2E 与公开 Gate 全绿。
- [x] AC5：测试报告、进度台账、分段提交与 push 完成。

## 4. 非目标

- 不重做视觉设计、不修改 API/数据库/RAG，不触碰用户个人文件。
- 不以 gzip 大小替代实际传输前原始 chunk 预算，不通过提高 Vite 阈值掩盖问题。
