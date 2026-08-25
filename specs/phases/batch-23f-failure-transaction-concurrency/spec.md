# Batch 23F 规格：失败事务并发矩阵 v2

## 1. 目标

在 Batch 23E 的独立进程、文件 SQLite/WAL 与真实 chat router Harness 上，补齐
regenerate 的真实双请求、外部数据库变更和取消后重试证明。v1 fixture 保持不变，新增
v2 fixture/report，避免静默改写已发布基准。

## 2. 新增场景

1. `regenerate-active-second-request`：第一请求已 claim 且进入 LLM 后阻塞；第二个独立
   TestClient 请求必须因 active-set 返回精确 409，且不增加 retrieval/LLM 调用；释放后
   第一请求唯一 finished、revision=1。
2. `regenerate-external-revision-conflict`：第一请求终态提交前，独立 Session 把目标更新为
   revision=1 并由第三连接读回；第一请求必须返回 `regenerate_conflict`，不得覆盖外部状态。
3. `regenerate-external-delete`：第一请求终态提交前，第二个独立 TestClient 调真实 DELETE
   端点并得到 204；第一请求必须返回 `regenerate_target_missing`，不得复活目标，计数为 1。
4. `regenerate-cancel-release-retry`：首次请求至少产生一个 delta 后取消，无终态且目标不变；
   不做 Harness 清理即以相同 revision 重试，必须进入 LLM 并成功 revision=1。

## 3. 并发与事务证据

- 每个并发场景使用 per-scenario controller、`threading.Event` 和有限超时，不以 sleep 竞争。
- 第一请求 worker 与第二请求使用不同 TestClient；worker 异常、join 超时或存活线程均使 Gate 失败。
- active 409 必须校验固定 detail，并证明第二请求前后 fake 调用数不变。
- 外部 update 必须 rowcount=1、commit 后由新连接读回，再释放第一请求。
- delete 必须经真实路由完成，并在第一请求恢复前由新连接确认目标缺失和计数一致。
- Harness 的失败清理只能在所有行为断言之后进行，不能伪造 active-set 释放。

## 4. v2 报告与硬 Gate

- 所有场景输出同一固定键集合；不适用的 peer/retry 状态使用 null。
- 报告只含固定枚举、状态码、计数、布尔值和 SHA256，不含 ID、正文、引用、线程名、路径、
  异常正文、时间戳或耗时。
- v2 新增 coordination timeout、worker exception/live worker、active 409 原因、secondary fake
  调用、外部 commit、外部状态覆盖、目标复活、取消释放和重试失败违规计数，全部必须为 0。
- fake 调用、请求数和 offline proof 汇总必须与场景逐项求和一致；报告双跑字节一致。

## 5. 非目标

- 不修改生产路由行为，不涉及前端/UI。
- 不访问 `papers/`、`eval/private/`、`config.yaml`、真实数据库、向量库或网络。
- 不调用 Kimi/Embedding，不宣称提升 RAG/生成质量指标。
