# Batch 24 实施计划

1. **T1（24A）**：E2E 基线——electron/test/release-flow.test.js，真启后端子进程断言关键流程闭环；RED（流程断言对不存在的服务失败）→ GREEN（真启通过）→ 泄漏/超时硬化
2. **T2（24B）**：可访问性契约——frontend vitest，先写破坏性感知 RED（缺 aria-label 必 fail）再补组件契约 GREEN
3. **T3（24C）**：包体预算——scripts/check_artifact_budget.py + npm script 接入；RED（假制品超预算/夹带必 fail）→ GREEN
4. **T4（24D）**：升级回滚——electron/test/data-dir-migration.test.js，模拟旧数据目录三断言
5. **TRACE**：三端全量回归 + 台账 + test-report + tasks 勾选 + push

并行策略：T1+T4（Electron 侧，agent 1）与 T2+T3（前端+脚本侧，agent 2）文件隔离并行；T5 主代理收尾。
