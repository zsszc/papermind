# Batch 23A 规格：生成 Guardrail 离线 Harness

## 1. 目标

在不发送真实论文、不依赖 Kimi 可用性的前提下，为生产生成链路建立公开可复现的引用
忠实度、证据边界、负例拒答与 SSE 安全基线，为后续一次性私有 smoke 提供可信 Gate。

## 2. 范围

- 使用原创 CC0 合成论文、问题、检索证据与固定生成响应构建公开 fixture。
- 复用生产聊天的消息组装、引用解析和 Guardrail 逻辑；评测代码不得另写宽松解析器。
- 评测 citation precision/recall/F1、越界引用数、负例拒答率和流式终止契约。
- 本批只运行确定性离线响应或 mock LLM，不调用 Kimi、联网搜索、Embedding 或私有语料。

## 3. 硬 Gate

- 指标数学以手算 fixture 锁定，重复/未知/越界 citation 必须计入相应惩罚。
- 引用不得超出检索允许的 paper/chunk 集；越界引用数必须为 0 才允许后续私有 smoke。
- 负例必须明确拒答且不得伪造引用，公开 fixture 的拒答率下限为 0.90。
- SSE 中错误、取消、提前 EOF、重复 finished 不得留下半条 assistant 消息或错误引用。
- 公开 CI 全离线、无密钥、无私人数据；评测报告绑定 fixture 与代码指纹。

## 4. 非目标

- 不根据公开 fixture 调整真实检索排序。
- 不执行真实论文固定四题 smoke，不将 QA 或 top-k 证据发送到外部服务。
- 不读取 Benchmark v1/v2 holdout，不修改生产模型或温度配置。
