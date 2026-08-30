# Batch 23F 测试报告：失败事务并发矩阵 v2

## 1. 结论

Batch 23F 在 Batch 23E 的独立进程、文件 SQLite/WAL 与真实 chat router Harness 之上，
补齐了 regenerate 的真实并发/外部变更证明：双 TestClient 第二请求 409、外部会话
revision 冲突、真实 DELETE 端点外部删除、取消后无清理重试。四个 v2 场景全部通过，
既有 7 个场景零回归（共 11/11 PASS，gate=PASS）。

本批次由前序代理完成 T1–T3（审查/RED/GREEN），T4 加固改动（错误码枚举、场景元数据
冻结校验、fake 调用汇总一致性、终态/计数推导校验）在磁盘上以未提交状态被发现，
本次验收确认其实现完整、证据达标后按 T5 流程收尾提交。

## 2. SDD / TDD 轨迹

- 审查 `spec.md`：v1 fixture 冻结不变，v2 新增四场景与违规计数字段（全部必须为 0）。
- RED `6a07ea1`：v2 fixture/schema/subprocess 契约先行。
- GREEN `4ef18eb`：per-scenario controller + threading.Event 同步（不以 sleep 竞争），
  实现四场景。
- HARDEN（本次提交）：worker join/异常、第二请求调用增量、精确 409/错误原因、
  报告双跑字节一致性与汇总一致性 Gate——`failure_transactions.py` 校验器新增
  场景元数据冻结比对、terminal/error_code 枚举白名单、fake 调用三方求和一致、
  按冻结契约推导期望终态计数。

## 3. 并发事务 Gate（`python -m eval.failure_transactions`）

| Gate | 结果 |
|---|---:|
| 注册场景 / 通过场景 | 11 / 11 |
| regenerate-active-second-request：第二请求 409 / 调用增量 | 精确 detail / 0 |
| 外部 revision 冲突：regenerate_conflict / 状态未被覆盖 | PASS / PASS |
| 外部删除：真实 DELETE 204 / regenerate_target_missing / 目标未复活 | PASS / PASS / PASS |
| 取消-释放-重试：首请求无终态 / 同 revision 重试成功 revision=1 | PASS / PASS |
| coordination timeout / worker exception / live worker 违规 | 0 / 0 / 0 |
| 报告双跑 | 字节一致 |

## 4. 回归与计数

- 全套件：**912 passed / 0 failed**（`env -u PYTHONPATH venv/bin/python -m pytest tests/ -q`，22.93s）；
  较 Batch 23E（911）净增 1（v2 并发场景校验用例）。
- 定向：`tests/test_failure_transactions_offline.py` **5 passed**。
- 前端 55 / Electron 26 未涉及（本批次纯后端）。
- 公开检索/生成基准无回退（本批次不改检索与生成路径）。

## 5. 已知限制

- v2 并发证据限于 TestClient + 文件 SQLite/WAL；真实多 worker 部署形态不在范围（单 worker 架构既定）。
- Moonshot 账户冻结状态不影响本批次（全程 fake，零真实服务调用）。

## 6. 提交

- T4 代码与校验器加固：本次提交（`test: harden failure transaction proof` 后续）。
- 文档：本报告 + 进度台账 + tasks.md 勾选 + AGENTS.md 计数同步（894/911 → 912）。
