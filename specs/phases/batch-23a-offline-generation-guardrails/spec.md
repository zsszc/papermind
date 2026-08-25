# Batch 23A 规格：生成 Guardrail 离线 Harness

## 1. 目标

在不发送真实论文、不依赖 Kimi 可用性的前提下，为生产生成链路建立公开可复现的引用
忠实度、证据边界、负例拒答与 SSE 安全基线，为后续一次性私有 smoke 提供可信 Gate。

## 2. 范围

- 使用原创 CC0 合成论文、问题、检索证据与固定生成响应构建公开 fixture。
- 复用生产聊天的消息组装、引用解析和 Guardrail 逻辑；评测代码不得另写宽松解析器。
- 评测 citation precision/recall/F1、越界引用数、负例拒答率和流式终止契约。
- 本批只运行确定性离线响应或 mock LLM，不调用 Kimi、联网搜索、Embedding 或私有语料。
- 生产 stream / non-stream / regenerate 共用同一纯 Guardrail；流式 delta 只是
  provisional，以 finished 的清洗后 `content` 为唯一成功终态。

## 3. 硬 Gate

- 指标数学以手算 fixture 锁定：每个 citation claim 都进 precision 分母，
  只有首次、合法且相关的 chunk 进正确分子；Recall 按唯一相关证据覆盖，
  F1 逐正例计算后宏平均。
- 引用不得超出检索允许的 paper/chunk 集；越界引用数必须为 0 才允许后续私有 smoke。
- 负例必须明确拒答且不得伪造引用，公开 fixture 的拒答率下限为 0.90。
- SSE 中 error / cancel / EOF 不得留下半条 assistant；重复 finished 采“首终态获胜”幂等契约。
- CI 的“生成 Harness 执行阶段”强制离线、无密钥、无私人数据；checkout /
  依赖安装 / artifact 上传属外层 CI 基础设施，不属该执行边界。

## 4. 非目标

- 不根据公开 fixture 调整真实检索排序。
- 不执行真实论文固定四题 smoke，不将 QA 或 top-k 证据发送到外部服务。
- 不读取 Benchmark v1/v2 holdout，不修改生产模型或温度配置。
