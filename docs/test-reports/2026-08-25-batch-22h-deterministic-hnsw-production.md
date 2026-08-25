# Batch 22H 测试报告：确定性 HNSW 生产候选

## 1. 结论

Batch 22H 完成了生产候选所需的离线 stage、原始 SQLite/HNSW 审计、embedding 内容指纹、
train 独立进程重复性 Gate、同一次问题遍历的 dev 配对 Harness，以及激活后二次校验失败自动
回滚。候选在 train 消除了既有跨进程排序抖动，但在 dev 没有带来任何质量提升；严格 Gate
判定失败，因此没有激活，生产 `vector_db/` 未被替换。

## 2. SDD/TDD 与审查修复

- 三路只读 Agent 分别审查生产库切换安全、评测 Gate 和 Chroma 工具实现。
- 修复 P0：`VectorStore` 不再用 `get_or_create_collection(... metadata=...)` 打开已有库，避免
  Chroma 0.4.24 抹掉已冻结的 `num_threads/search_ef`。
- 修复 collection 作用域错误：向量数只统计 `papers` 的唯一 METADATA segment，不再误计
  其他 collection；同时要求唯一 VECTOR/METADATA segment。
- stage 构建绑定 464 个 ID、1024 维、embedding SHA、collection/segment 双层 HNSW 元数据、
  query smoke 与打开前文件指纹；激活后复检失败会隔离失败候选并恢复旧库。
- 报告新增数据库逻辑、HNSW 配置/文件、tracked Git clean 指纹；Gate 制品绑定输入报告 SHA。
- RED 提交：`ecb1c0a`、`8f4cf82`、`c3ccd57`、`06f71c4`、`1e3dc91`、`81fcfe0`。

## 3. Chroma 运行时文件发现

真实 stage 首次构建按预期 fail-close。隔离复现实验证明 Chroma 0.4.24 仅打开 collection 就会
重写含运行时状态的 `length.bin`；默认 HNSW 首次查询还可能重写 `data_level0.bin` 的运行时
区域。embedding 语义内容 SHA 始终为
`b8480199082caa0750da021cacbc8f0a079e1d817f755bfba93c2a7ac049fe29`。

最终协议在任何 Chroma client 打开前冻结原始文件指纹，打开后以 ID、维度、embedding SHA、
双层元数据和 query smoke 验证语义完整性。构建前后生产 `length.bin` SHA 相同，证明正式
stage 构建没有初始化或改写生产 Chroma；失败的配对基线副本被保留为隔离诊断目录。

## 4. 真实论文评测

评测使用私有 Benchmark v1 的 train/dev 各 24 条、真实 PDF 页文本和与生产 464 chunks 内容
一致但补齐页坐标的只读 Batch 22D `baseline.db`。holdout 未读取，未调用 Kimi 生成。

### Train 独立进程重复性

| 指标 | Run A | Run B | Gate |
|---|---:|---:|---|
| top-5 完全相同 | 24/24 | 24/24 | PASS |
| Recall@5 | 0.6666666667 | 0.6666666667 | PASS |
| factoid Recall | 0.5000000000 | 0.5000000000 | PASS |
| MRR | 0.4236111111 | 0.4236111111 | PASS |
| NDCG@5 | 0.4852888182 | 0.4852888182 | PASS |
| P95 | 344.0 ms | 365.5 ms | PASS |

两份报告绑定 Git `231dc60f69c95c8f16b5cb43896410fe0b27586f`、数据库/语料/页文本、
向量/HNSW 与管线身份；自动 Gate `passed=true`。

### 同遍历配对 dev

| 指标 | 当前生产 HNSW 副本 | 确定性候选 | 差值 |
|---|---:|---:|---:|
| Recall@5 | 0.6250000000 | 0.6250000000 | 0 |
| factoid Recall | 0.3333333333 | 0.3333333333 | 0 |
| MRR | 0.3937500000 | 0.3937500000 | 0 |
| NDCG@5 | 0.4517186825 | 0.4517186825 | 0 |
| P95 | 248.5 ms | 244.6 ms | 候选通过延迟 Gate |

四项非回退和延迟检查通过，但“至少一项严格质量提升”失败，自动 Gate `passed=false`。
这说明 `search_ef=464/num_threads=1` 的价值是重复性，不是当前 dev 的质量提升；按冻结规格
不能把稳定性收益临时改写成质量晋级条件。

## 5. 全量 Harness

- 后端：`834 passed, 1300 warnings`，13.10 秒。
- 前端：12 个文件、39 项测试通过；ESLint 零警告；Vite 生产构建通过。
- Electron：26 项测试通过。
- 公开冻结 BM25：Recall@5/MRR/NDCG@5=`0.900/0.783/0.813`，Gate 通过。
- `pip check`：无依赖冲突。
- 实际 Uvicorn 启动成功；Kimi 健康检查 HTTP 200，`/api/health` 返回
  `status=ok`、`llm_ready=true`；随后正常关闭。
- 主 SQLite 仍报告已知 4 条 `paper_tags` 历史孤儿，本批未覆盖用户数据。

## 6. 决策与下一步

1. 不激活确定性 HNSW 候选，生产配置与向量目录保持原样。
2. 保留 stage、train/dev 报告和 Gate 制品在 gitignore 的本地评测目录，供后续审计。
3. Batch 22I 改为 train-first 的稀有实体/数值/单位锚点候选，硬目标是 factoid 至少新增
   1/6 命中，同时总体 Recall/MRR/NDCG 不回退、P95 < 1 秒；失败则不看 dev。
4. 私有生成评测仍需单独的真实内容出站授权；本批 Kimi 仅做最小健康检查。
