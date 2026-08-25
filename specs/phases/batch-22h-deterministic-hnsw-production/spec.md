# Batch 22H 规格：确定性 HNSW 生产候选

## 1. 目标与单一变量

Batch 22G 证明当前 464-vector HNSW 在独立进程间会产生 21/24 的 top-5 抖动；将评测副本
冻结为 `num_threads=1/search_ef=464` 后，production 双跑与 compat 等权均为 24/24，且
train Recall/MRR/NDCG 稳定为 0.667/0.424/0.485，P95 仍低于 1 秒。

本批只改变 HNSW 查询参数，不重算 embedding、不改 RRF/BM25/chunk/QA/qrel。目标是验证该
确定性配置能否安全晋级真实生产 `vector_db/`。

## 2. 数据与激活契约

- 源必须是当前真实 `vector_db/`，先校验 SQLite/Chroma ID 全等、464 条、1024 维及 embedding SHA。
- 后端/Electron 和所有 Chroma client 必须停止后才能复制或激活；不支持在线热切换。
- 使用与生产 `vector_db/` 同父目录的唯一 stage 复制，不原地修改；冻结 `hnsw:num_threads=1`、
  `hnsw:search_ef=vector_count`，修改前后 embedding SHA 必须相同。
- 激活沿用 `activate_staged_vector_store()` 的备份/原子 rename/回滚语义；激活前必须再次审计。
- 生产配置、模型、语料和检索 profile 均不改变。

## 3. 评测协议与 Gate

1. 在 train 上独立运行候选两次，要求 24/24 top-5 相等、四项质量逐值相等、零降级。
2. Train Recall/factoid/MRR/NDCG 使用未舍入阈值，分别不低于
   `0.6666666666666666/0.5/0.4236111111111111/0.4852888182323138`，P95 < 1 秒。
3. 历史 dev 报告缺少现行 page-span/vector/HNSW 指纹，不能充当自动 Gate 基线。train 通过后
   只运行一次进程内配对 dev：同一次遍历同时查询当前生产快照与候选快照，只允许 HNSW 配置
   不同；候选 Recall、factoid、MRR、NDCG 均不回退且至少一项严格提升，P95 < 1 秒。
4. dev 通过才允许备份并原子激活；激活后做 vector audit、query smoke、后端 health 与聊天检索 smoke。
5. holdout 保持封存；不发送真实 QA/证据给 Kimi。

## 4. 验收标准

1. stage 构建、源只读、目标已存在拒绝、collection/segment 双层配置、embedding/HNSW 文件
   SHA 与激活后验证失败回滚均有测试。
2. 报告/选择制品绑定 Git、数据库、语料、page text、vector 与 HNSW 配置 SHA。
3. 任一重复性、质量、延迟、降级或完整性 Gate 失败即停止，不触碰生产向量目录。
4. 完成全量后端、前端、Electron、公开评测、依赖与实际启动 Harness，并提交中文测试报告。
