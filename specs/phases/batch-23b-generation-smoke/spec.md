# Batch 23B 规格：生产生成闭环 smoke（真实内容出站）

> 台账：「固定私有四题 smoke、生产链路引用忠实度与拒答；一次固定配置运行；不用于调参」。
> **用户已于 2026-08-31 明确授权真实内容出站**（与 Batch 22L QA 生成同一授权）。
> 目的：验证生产聊天链路（检索 → 组装 → Kimi 生成 → Guardrails）在真实 LLM 下的端到端行为。

## 1. 范围

- 4 道固定私有题：3 道正例（选自 v1 train，语料内可答）+ 1 道负例探针（语料外问题，期望拒答或明确声明无依据）
- 走生产 `POST /api/chat` SSE 全链路（真实 Kimi，kimi-k2.6，生产默认检索配置）
- 断言引用忠实度（`[^n^]` 均在检索证据范围内、无越界/伪造）与负例行为
- **一次固定配置运行**，结果归档私有目录；不因结果调整任何参数/算法/prompt

## 2. 硬约束

- 授权范围仅限本批 4 题与 22L QA 生成；禁止扩散到其他真实内容出站用途
- 运行配置冻结为生产默认（shared hybrid、rerank off、graph_expand off）
- 报告去标识化（私有目录 gitignore）；公库只提交 spec/plan/tasks
- 失败（网络/额度）如实记录为 UNAVAILABLE，不伪造结果

## 3. 任务契约

### T1：选题与固定

- 从 `eval/private/qa_private_v1.jsonl` train 分区固定选 3 道正例（factoid/method_detail/summary 各一，qa_id 记录进报告）
- 负例：固定一句语料外问题（「人工智能在火星探测中的最新进展是什么？」）
- 选题清单排他写入 `eval/private/batch23b_smoke_questions.json`（0600）

### T2：生产链路实跑

- 后端以生产配置运行；逐题 POST /api/chat 收集完整 SSE（delta 拼接 == 正文、finished 帧 citations）
- Langfuse trace 关联（host 已配置则记录 trace id 入报告）

### T3：断言与报告

- 正例：citations 全部落在当次检索证据内（编号不越界）；正文非空；无 Guardrail 剔除告警则为满分引用忠实度
- 负例：正文含拒答/无依据声明，且 citations 为空
- 报告 `docs/test-reports/2026-09-01-batch-23b-generation-smoke.md`（指标+trace id+限制），台账一行，tasks 勾选

## 4. 验收标准

- [ ] AC1：4 题全部完成实跑（或如实 UNAVAILABLE）
- [ ] AC2：正例引用零越界；负例行为符合拒答契约
- [ ] AC3：报告+台账+push；私有制品不泄原文

## 5. 非目标

- 不调参、不迭代、不扩展题量；生成质量评分（人审）不在本批
