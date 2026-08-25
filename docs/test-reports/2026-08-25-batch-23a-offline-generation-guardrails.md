# Batch 23A 测试报告：生成 Guardrail 离线 Harness

## 1. 结论

Batch 23A 已完成公开可复现的生成引用/拒答 Gate，并修复生产链路漂移：
主聊天 stream/non-stream 与 regenerate 现在共用同一纯 Guardrail，finished 携带
清洗后全文，citations 只包含答案实际合法引用的 chunk。前端已把 delta 视为
provisional，成功时原子替换，失败/取消/提前 EOF 不保留半条回答。

公开生成 Gate 的 citation precision/recall/F1 与负例拒答率均为 1.000，越界、
畸形、重复和负例引用均为 0，Gate PASS。

## 2. SDD / TDD 轨迹

- RED `ae8dd74`：后端在收集阶段因纯 Guardrail/离线 evaluator 缺失而失败；
  前端 4 项契约失败，证明 finished 未传递清洗全文、畸形 citations 未拒绝、
  UI 保留越界 marker 与 error 后半条回答。
- GREEN `51bf764`：实现共享纯解析/清洗、实际引用选择、公开 fixture/CLI/CI、
  生产三路径 parity 与前端原子终态。
- 三个只读代理并行审计了 Harness 数学、SSE 状态机和隐私/离线边界，据此
  采用 occurrence-aware precision、重复 chunk 身份 fail closed、报告字段白名单和执行阶段 audit hook。

## 3. 指标口径

- 生产唯一引用协议为 `[^n^]`；旧式 `[pN_cN]`、畸形、越界或歧义身份均失败关闭。
- Precision 分母是所有 citation claim；只有首次、合法、相关的唯一 chunk 进正确分子。
- Recall 按唯一相关证据覆盖；F1 逐正例计算后宏平均，不由宏 P/R 二次推导。
- 安全负例必须明确拒答、零 citation claim、零 retrieved 且无生成错误。

## 4. 公开 Gate 结果

| 指标 | 结果 | 门槛 | 状态 |
|---|---:|---:|---|
| Citation precision | 1.000 | ≥ 0.90 | PASS |
| Citation recall | 1.000 | ≥ 0.90 | PASS |
| Citation F1 | 1.000 | ≥ 0.90 | PASS |
| 负例拒答率 | 1.000（2/2） | ≥ 0.90 | PASS |
| 越界 / 畸形 / 重复引用 | 0 / 0 / 0 | 全部 = 0 | PASS |
| 负例引用 | 0 | = 0 | PASS |

报告绑定 fixture、Guardrail 公式与共享生产消息契约 SHA-256，只写 case ID、计数、指标与 Gate，
不写答案、prompt、证据正文、绝对路径或配置。清洁解释器执行证明：network /
subprocess / private-path attempts 均为 0，禁止模块加载列表为空。

## 5. 全量 Harness

| Harness | 结果 |
|---|---|
| 后端 pytest | **887 passed** |
| 前端 Vitest | **50 passed / 14 files** |
| 前端 ESLint | 0 warnings |
| 前端生产构建 | PASS（保留既有大 chunk 提示） |
| Electron node:test | **26 passed** |
| 公开 count Gate | Recall@5/MRR/NDCG@5 = **0.900/0.775/0.806**，PASS |
| 公开 BM25 Gate | Recall@5/MRR/NDCG@5 = **0.900/0.783/0.813**，PASS |
| 生成 Guardrail Gate | P/R/F1/refusal = **1.000/1.000/1.000/1.000**，PASS |
| Python 依赖一致性 | `pip check` 无冲突 |

环境：macOS，Python 3.12.2，Node 22.23.1，npm 10.9.8。主要复现命令：

```bash
cd backend && env -u PYTHONPATH venv/bin/python -m pytest tests/ -q
cd frontend && npm run lint && npm test && npm run build
cd electron && npm test
cd backend && env -u PYTHONPATH venv/bin/python -m eval.run --fixture eval/fixtures/rag_public_v1.json --dataset eval/dataset/qa_public_v1.jsonl --keyword-only --lexical-profile count --threshold 0.85 --report-dir eval/reports/public-count
cd backend && env -u PYTHONPATH venv/bin/python -m eval.run --fixture eval/fixtures/rag_public_v1.json --dataset eval/dataset/qa_public_v1.jsonl --keyword-only --lexical-profile bm25 --threshold 0.85 --report-dir eval/reports/public-bm25
cd backend && env -u PYTHONPATH -u OPENAI_API_KEY -u KIMI_API_KEY -u MOONSHOT_API_KEY -u PAPERMIND_DATA_DIR venv/bin/python -m eval.generation_guardrails --report-dir eval/reports/public-generation
cd backend && venv/bin/python -m pip check
```

## 6. 隐私边界与限制

- 本批未读取 `papers/`、`eval/private/`、真实 QA/holdout 或 `config.yaml`，未调用
  Kimi、Embedding、联网搜索或其他网络服务。
- “CI 离线”指生成 Harness 执行阶段；checkout、依赖安装与 artifact 上传依然是 CI 基础设施网络。
- deep-review 已在 finished 提供清洗全文，但为保持现有综述引用聚合契约，本批未改为
  “仅实际 marker 子集”；后续需独立 SDD/TDD。
- 真实论文四题 Kimi smoke 仍会把问题和 top-5 证据发送到外部，充值不等于
  内容出站授权；Batch 23B 继续等待用户明确授权。
