# Phase C：Guardrails 防幻觉护栏 规格说明书

> 来源：Phase 2 计划 Phase C 章节 + specs 反推实证（agent_graph.md / chat.md / eval/metrics.md）。
> 目标：AI 回答的引用必须可溯源；检索不足时明确拒答而非编造。

## 1. 背景与目标

现状（规格实证）：`/api/chat` 的引用完全依赖 LLM 自觉——prompt 里给了 chunk 列表并要求 `[^n^]` 标注，但**无任何后置校验**：LLM 编造不存在的引用编号、或在零检索结果下仍输出 `[^1^]`，系统照单全收并落库。eval 侧 `citation_coverage` 指标已实现（metrics.md）但未接入 `--with-llm` 流程。

## 2. 范围

### 2.1 包含

- C1：**引用忠实度校验**。答案流式生成完成后、落库前，校验每个 `[^n^]` 标记：n 必须在本次检索返回的 chunk 编号范围内；无检索结果时答案中不得出现任何 `[^n^]`。违规处理：剔除无效标记 + citations 标注 `verified: false`（**不阻塞返回，先观测**）
- C2：**检索不足拒答强化**。system prompt 增加硬约束：未检索到 chunk 时必须声明「文献库中没有相关内容」、禁止编造引用
- C3：**引用忠实度进评测**。`citation_coverage` 接入 eval `--with-llm` 流程，作为生成侧正式指标入报告

### 2.2 非目标

- 不改 SSE 帧格式（delta/finished/error 三类帧保持）
- 不做语义级引用忠实度（答案语句与 chunk 内容的蕴含关系）——属 NLI 范畴，后续 Phase 评估
- 不处理 regenerate 路径的校验（其无请求体开关，共享同一校验函数即可自然受益）
- LLM 在线验证（拒答率实测）依赖 Moonshot 解冻，本轮只交付离线可测部分

## 3. 行为契约

### 3.1 C1：verify_citations 校验

- **位置**：`services/agent_graph.py` 新增纯函数 `verify_citations(answer_text, retrieved_chunks) -> (cleaned_text, report)`；`routers/chat.py` 在流式完成后、落库前调用（generate 之后的唯一落库点）
- **输入**：答案全文、本次检索返回的 chunk 列表（编号 1-based 与 prompt 中一致）
- **规则**：
  - `[^n^]` 且 1 ≤ n ≤ len(retrieved) → 保留，计入有效引用
  - `[^n^]` 越界或 retrieved 为空 → 从文本剔除该标记（保留语句本身）
- **输出**：`(清洗后文本, {"total": n, "valid": m, "removed": k, "verified": bool})`
- **落库**：citations 字段附 `verified`（全部有效或无引用时为 true；有剔除为 false）与 `removed` 计数
- **日志**：有剔除时记 `[guardrails]` warning（qa 脱敏：不记答案全文，记编号列表）
- **幂等纯函数**：无 DB/网络/LLM 调用，可直接单测

### 3.2 C2：拒答强化

- **触发**：检索结果为空（retrieved == []）
- **行为**：组装 system prompt 时追加硬约束段：「未检索到相关文献片段。必须明确回答『文献库中没有相关内容』，禁止编造任何引用标记。」
- **不回归**：有检索结果时 prompt 与现状一致

### 3.3 C3：citation_coverage 接入评测

- `eval/run.py --with-llm` 路径：对每条有答案 QA 计算 `citation_coverage`（既有函数，签名见 metrics.md），汇总均值写入报告 `overall.citation_coverage`
- 报告 schema 增量字段；trend.py 对缺字段旧报告不崩（延续 B4 兼容模式）

## 4. 边界条件与错误处理

| 场景 | 期望行为 |
|------|----------|
| 答案无 `[^n^]` 且有检索 | verified=true（引用缺失是行为问题但不篡改） |
| `[^0^]` 或负数 | 剔除（编号 1-based） |
| retrieved=0 且答案含 `[^1^]` | 剔除 + verified=false |
| 答案流式中断（无落库） | 不调用校验（沿用现状） |
| `--with-llm` LLM 返回错误串 | citation_coverage 按 0 计或跳过该条（取 metrics 现有契约） |

## 5. 依赖

- specs/backend/services/agent_graph.md（三节点线性拓扑、prompt 组装契约）
- specs/backend/routers/chat.md 3.8（落库时序七步）
- specs/backend/eval/metrics.md（citation_coverage 公式与边界）
- 宪法第 5 条（TDD）、第 8 条（LLM 唯一入口——校验函数不调 LLM，天然合规）

## 6. 验收标准（可测试）

- [ ] AC1：伪造答案文本单测全覆盖（有效/越界/零检索/无引用/多引用混合），verify_citations 行为符合 3.1
- [ ] AC2：chat 路由集成测试：mock LLM 输出含越界引用 → SSE 完成后落库 citations 带 verified=false、文本标记已剔除
- [ ] AC3：检索为空时发往 LLM 的 system prompt 含拒答硬约束段；非空时不含
- [ ] AC4：`eval.run --with-llm`（mock LLM）报告含 citation_coverage 字段；trend.py 兼容旧报告
- [ ] AC5：全套件全绿

## 7. 现有测试覆盖与盲区

- agent_graph 现有测试覆盖编排节点（test_agent_graph.py），无生成后校验概念
- chat 路由 SSE 落库测试由 Batch 7/7b 建立（test_chat.py），本轮在同文件追加
- citation_coverage 单测已存在（metrics 32 用例之一），接入 run.py 属新路径需新测

## 8. 关键设计决策

- **校验放路由层而非 LangGraph 图内**：agent_graph 现状是 generate 前的编排图（记忆→检索→组装），LLM 流式生成在路由层；把纯函数校验插在落库前是最小侵入，不强行扩图（若 Phase F Deep Agents 重排图结构再议）
- **违规不阻塞、先观测**：剔除+标记而非 422/重试——护栏第一版先收集违规率数据，再决定是否需要重生成回路
- **拒答用 prompt 硬约束而非输出拦截**：成本为零且可观测（eval 负例拒答率）；输出拦截属更重方案留作后备
