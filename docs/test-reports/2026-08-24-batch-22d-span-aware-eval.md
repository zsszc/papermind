# Batch 22D 测试报告：跨块证据 Benchmark v2

## 1. 结论

Batch 22D 已完成原定的跨块证据评测修复，并在真实论文 train 上正式拒绝 512/50
细粒度分块候选。候选没有进入 dev、holdout 或 Kimi 生成评测，也没有换入生产数据库或
向量库。

评测前的独立审查指出“命中 chunk 数 / 相关 chunk 数”会随分块粒度改变分母，因此在查看
train 排名结果前将主指标修订为证据字符区间的并集覆盖率；该决策以独立 SDD 提交保留。

## 2. 实现与 TDD 痕迹

- `Chunk` 新增可空 `page_start/page_end`，正文采用相对原始解析页文本的 0-based 半开区间。
- TextChunker 从原始页文本切分阶段携带坐标，覆盖硬切、overlap 与段落分隔符边界。
- `page-span-v2` 先在原始页唯一定位 quote，再映射同页相交 chunk；跨页、多处命中、空坐标、
  越界或覆盖空洞全部 fail-close。
- 同时报告 `any_hit@5` 与字符 `span_coverage@5`，旧 `chunk-v1` 行为和公开基准保持不变。
- 新增旧粗分块坐标回填候选工具、WAL 快照回归及旧/新报告自动配对 Gate。
- RED、GREEN、真实故障修复和 Gate 均分别提交；私有 QA、quote、逐题结果未进入 Git。

## 3. 隔离数据与完整性

| 项目 | 旧基线候选 | 512/50 候选 |
|---|---:|---:|
| SQLite chunks | 464 | 2,904 |
| 正文 chunks | 445 | 2,885 |
| 有效正文坐标 | 445/445 | 2,885/2,885 |
| train evidence 唯一解析 | 24/24 | 24/24 |
| train evidence 对应 chunk | 24 | 39 |
| Chroma IDs | 464 | 2,904 |
| Embedding 维度 | 1,024 | 1,024 |
| SQLite quick_check | ok | ok |
| 外键违规 | 0 | 0 |

两套候选的原始页文本指纹一致。旧基线只在副本回填 offset，chunk ID、内容、页码与索引均
保持不变；候选向量使用本地 BGE-M3 缓存离线构建并通过 ID 全等、维度与 query smoke。
生产源库仍为 464 chunks，并保留历史 4 条 `paper_tags` 孤儿，未被本批修改。

## 4. 真实 train 配对结果

检索配置固定为 hybrid + `bm25-bilingual` + top-5，无 reranker；24 条 train QA，零运行时降级。

| 指标 | 旧基线 | 512/50 候选 | 差值 |
|---|---:|---:|---:|
| span_coverage@5 | 0.667 | 0.453 | -0.213 |
| any_hit@5 | 0.667 | 0.500 | -0.167 |
| factoid span coverage | 0.500 | 0.393 | -0.107 |
| MRR | 0.422 | 0.344 | -0.078 |
| NDCG@5 | 0.483 | 0.316 | -0.167 |
| P95 | 353.2 ms | 403.7 ms | +50.5 ms |

冻结 Gate 要求 coverage 至少提升 1/24、any-hit/factoid 不回退、MRR/NDCG 回退不超过
0.02、P95 < 1 秒且零降级。候选仅通过延迟与运行稳定性，其他五项失败，因此拒绝并跳过
dev。512 字符上限能减少过长输入风险，但“字符”不等于 tokenizer token，报告不宣称已彻底
消除 BGE 截断。

## 5. 全量 Harness

- 后端：`678 passed, 1253 warnings`，12.06 秒。
- 公开冻结 BM25：Recall@5/MRR/NDCG@5 = `0.900/0.783/0.813`，0.85 Gate 通过。
- 前端：12 个文件、39 项测试通过；ErrorBoundary 预期 stderr。
- Electron：26 项测试通过。
- 前端 lint 零警告，生产 build 通过；保留既有大 chunk 提示。
- `pip check`：无依赖冲突。
- 隔离数据目录实际启动成功，`GET /api/health` 返回 200/status=ok；因隔离目录仅有公开配置
  模板，`llm_ready=false`，本批未读取或发送真实论文内容到 Kimi。

## 6. 异常与修复

首次真实旧库回填暴露 WAL 快照强制切换 `journal_mode=DELETE` 的锁冲突。未发布的候选自动
清理；随后移除非必要模式切换、增加 5 秒 busy timeout 与 WAL 回归测试，真实回填通过。

首次向量构建因 HuggingFace 客户端尝试在线 HEAD 而产生 DNS 重试，进程安全中止并删除明确
的不完整 stage；随后使用 `HF_HUB_OFFLINE=1` / `TRANSFORMERS_OFFLINE=1` 从本地缓存完整
重建。Chroma telemetry 警告仍为已知无害噪声。

## 7. 后续决策

不激活纯 512/50 分块。Batch 22E 将验证 parent-child 检索：细粒度 child 用于召回，旧粗块
或连续大窗口作为 parent 恢复上下文，并继续使用同一 page-span-v2 train Gate；只有 train
通过才允许一次 dev。
