# Batch 23D 规格：生成并发与失败清理 Harness

## 1. 目标

补齐 Batch 23C 未覆盖的跨阶段事务边界：deep-review 在规划、汇总、空结果、
Guardrail 和数据库失败时不得遗留新建空会话；regenerate 在生成期间目标被修改或删除时
不得覆盖较新的状态或发送假成功。

## 2. 范围

- 新 deep-review 延迟到成功终态事务才创建会话；plan/delta 的 `conversation_id` 为 null，finished 返回真实 ID。
- deep-review 只有非空、Guardrail 后仍非空的综述才能提交 user+assistant，并按真实消息行更新计数。
- deep-review 消息、引用与计数同一事务提交；提交前完成成功终帧序列化。
- messages 增加单调递增的 `revision`；历史接口返回该字段，regenerate 请求必须携带
  `expected_revision`。
- regenerate 入口 revision 不匹配时 HTTP 409，且不得进入检索或 LLM；同进程内同一目标
  的第二个在途请求同样 HTTP 409，避免重复模型费用。
- regenerate 终态使用 `id + conversation_id + role + revision` 条件更新，并把 revision 原子加一；
  目标被并发修改或删除时分别发送固定 `regenerate_conflict` / `regenerate_target_missing`
  error，不覆盖外部变更、不发送 finished。
- 客户端失败、冲突、断流或取消后以历史接口对账；会话 epoch 防止迟到结果覆盖已切换会话。
- 测试全部使用内存 SQLite、fake LLM / retrieval；不访问真实论文、私有评测、配置、网络或模型。

## 3. 硬 Gate

- 新 deep-review 在 plan / synthesize / empty / Guardrail-empty / finalization 失败后天然无会话写入：
  `orphan_conversation_count == 0`，消息数为 0，响应无 finished，错误文案脱敏。
- 指定已有会话时，失败不得删除或修改该会话及原消息。
- deep-review 成功时 user+assistant 与计数一次提交，`message_count == COUNT(messages)`。
- regenerate 冲突时外部最新 `content + citations` 原样保留，响应仅以 error 结束。
- regenerate 成功时正文、引用、revision 一次原子更新，finished 携带新 revision；所有终态都释放
  同目标 active-set。
- 所有失败响应、日志、数据库均不得包含异常 canary。
- 公开检索与生成 Gate 不回退，三端全量 Harness 全绿。

## 4. 非目标

- 不引入多用户锁、任务队列或分布式事务；`revision` 仅用于单消息乐观并发控制。
- 不承诺提交成功后客户端断连能回滚；数据库仍是权威状态源。
- 不执行 Kimi 或真实论文生成 smoke；Batch 23B 仍需明确内容出站授权。
