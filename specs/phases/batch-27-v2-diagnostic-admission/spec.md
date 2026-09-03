# Batch 27 规格：历史 train 报告诊断准入与单候选选择

## 1. 背景

Batch 26 默认要求 `git_tracked_clean=true`。现存 v2 train 报告的 split、top-k、resolver、
运行时和 5893/5893 向量快照均完整，绑定提交也是当前分支祖先，但报告生成时 tracked 工作区
非 clean。该报告不能作为候选晋级证据，但其冻结逐题排序仍可用于一次候选方向选择。

## 2. 目标

增加显式历史准入模式，在不修改源报告的前提下生成去标识化归因；模式仅允许候选选择，
绝不放宽正式 train Gate。取得主导失败后，只预注册一个候选方向。

## 3. 历史准入硬约束

- 默认行为不变：dirty 报告继续 fail closed。
- 只有 CLI 显式传 `--allow-historical-dirty-report` 才进入历史准入。
- 报告 `git_sha` 必须是当前 `HEAD` 的真实祖先提交。
- dataset/qrels/corpus/database/page/vector/HNSW 指纹必须为 64 位 SHA；数据库逻辑指纹必须
  等于 corpus 指纹。
- 向量快照必须满足 SQLite/Chroma 数量一致、无 missing/extra、1024 维、cosine、单线程、
  `search_ef == vector_count`，且快照指纹与 benchmark 一致。
- 输出必须标记 `usage=candidate-selection-only`、`promotion_eligible=false`、
  `requires_fresh_clean_baseline=true`；源报告 SHA 仍按原始字节语义计算，不得把 dirty 改成 true。

## 4. Gate

- 公开合成测试覆盖默认拒绝、未验证祖先拒绝、指纹/快照异常拒绝、显式历史准入成功。
- 历史模式输出仍不得包含 qa_id/chunk_id/正文/路径。
- 实际聚合只读取 train 报告；dev/holdout 继续拒绝。
- 得到主导失败后再创建下一候选 SDD；正式质量比较必须使用新生成的 clean 配对 train。

## 5. 非目标

- 不把历史 dirty 报告重新解释为正式基线或晋级证据。
- 不修改、覆盖或重新签名任何私有报告。
- 不运行 dev/holdout，不调用 Kimi。
