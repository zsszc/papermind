# Batch 22G 测试报告：兼容 Weighted-RRF 与确定性 HNSW Harness

## 1. 结论

Batch 22G 完成了只改变词法分支分子的 `weighted-rrf-compat-v1`。等权纯函数在旧 RRF
可接受的空值、重复、非 canonical ID、source fallback、负切片与浮点 k 等边界上保持深度相等；
真实 train 在确定性向量快照上达到 24/24 top-5 顺序 parity。

冻结的 1.25/1.5/2.0 三组词法权重全部回退，没有 train winner，因此未运行 dev、holdout
或真实论文 Kimi 生成，生产 `hybrid` 配置保持不变。

## 2. SDD/TDD 与多 Agent 审查

- 三路只读 Agent 分别审查旧 RRF 异常域、eval 接线与 train/dev 制品 Gate。
- RED 先锁定兼容公式、90 组等权边界矩阵、profile 隔离、失败关闭、CLI/公式哈希、选择器
  错配拒绝与 dev 身份绑定；GREEN 分三次提交。
- 选择器不再只信报告内部自洽哈希：会根据代码冻结公式重算 formula/configuration SHA，拒绝
  空 Git SHA，并把 train 输入报告 SHA、快照身份与 winner 配置绑定到 dev。

## 3. Harness 缺陷与修复

初次 production/compat 跨进程比较只有 21/24 一致。随后 production/production 复跑也正好
只有同三题不一致，证明问题不在兼容公式。CPU device、PyTorch/BLAS 单线程均不能消除抖动；
最终定位到 Chroma 0.4.24 HNSW 的低 `search_ef=10` 近邻边界。

新增 `python -m eval.deterministic_vector_snapshot`：只读复制原快照，经唯一 stage 修改副本的
`hnsw:num_threads=1` 与 `hnsw:search_ef=vector_count` 后原子激活。原 464-vector 内容 SHA
保持 `b8480199...fe29`，HNSW 配置 SHA 为 `b2d3e13f...a7387bd`。确定性 production 双跑
达到 24/24，随后 production/compat 等权也达到 24/24。

## 4. 真实 train 网格

| 词法权重 | Recall@5 | factoid Recall | MRR | NDCG@5 | P95 | Gate |
|---:|---:|---:|---:|---:|---:|---|
| 1.00 | 0.667 | 0.500 | 0.424 | 0.485 | 326.9 ms | parity baseline |
| 1.25 | 0.625 | 0.333 | 0.373 | 0.436 | 323.9 ms | FAIL |
| 1.50 | 0.625 | 0.333 | 0.373 | 0.436 | 321.4 ms | FAIL |
| 2.00 | 0.625 | 0.333 | 0.369 | 0.433 | 315.8 ms | FAIL |

所有候选都同时违反 Recall、any-hit、factoid、MRR 与 NDCG Gate；只有 P95 < 1 秒通过。
选择制品 `train-selection.json` 明确输出 `passed=false`、`winner=null`，因此没有 dev 运行资格。

## 5. 全量 Harness

- 后端：`820 passed, 1300 warnings`，15.07 秒。
- 前端：12 个文件、39 项测试通过；ESLint 零警告；Vite 生产构建通过。
- Electron：26 项测试通过。
- 公开冻结 BM25：Recall@5/MRR/NDCG@5 = `0.900/0.783/0.813`，Gate 通过。
- `pip check`：无依赖冲突。
- 实际 Uvicorn 启动成功，`GET /api/health` 返回 `status=ok`、`llm_ready=true`。
- 启动仍报告已知的 4 条 `paper_tags` 历史孤儿；本批没有覆盖或修复用户主库。

## 6. 后续决策

Batch 22H 不继续盲目扩大词法权重。优先把 `search_ef=464` 的确定性 HNSW 副本作为独立、
可回滚的生产候选：以当前真实 `vector_db/` 构建 stage，验证向量内容不变、train/dev 质量与
P95 Gate 后，才允许经备份原子激活。该方向已在 train 消除 0.625/0.667 波动并稳定为
0.667，价值高于继续在失败权重方向调参。
