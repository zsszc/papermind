# Batch 22C 实施计划

1. RED：冻结超长单段硬切、有界重叠、页码与连续索引契约。
2. GREEN：只修改 `TextChunker`，保持现有短段落行为与 chunk schema。
3. HARNESS：实现候选 SQLite 复制/reprocess 的显式 stage 命令与失败清理测试。
4. AUDIT：在候选副本上验证 DB、坐标、页面、长度、qrel 唯一解析和源库零变化。
5. VECTOR：从候选 SQLite 构建并验证隔离 Chroma，不使用 `--activate`。
6. TRAIN：只跑 private train；按冻结 Gate 决定是否运行一次 dev。
7. DEV：仅 train 通过时执行一次，失败候选不晋级；永不查看 holdout。
8. TRACE：全量 Harness、测试报告、路线图、分批提交与 push；生产换入单独等待授权。

