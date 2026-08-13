# Batch 18 规格：数据一致性与上传安全

## 1. 背景与问题

Batch 17 用 18 篇真实论文建立了 72 条已审 QA 基准，但也暴露出评测和恢复信心所依赖的数据层风险：

- SQLite 使用 WAL，自动备份与手动导出却直接复制正在使用的 `papers.db` / `-wal` / `-shm`，不能保证同一时点一致；Electron 运行时的手动导出还可能漏掉根目录数据库。
- 真实 SQLite `quick_check=ok`，但 `paper_tags` 有 4 条历史孤儿关联；不应在原库上自动修复。
- 当前 Chroma metadata 为 464 个当前 ID，持久 HNSW 快照却保留 1000 个旧 ID，隔离读取可触发 `IndexError`；现有 hybrid 指标不可信。
- PDF/DOCX 上传只检查扩展名；DOCX 没有 ZIP 条目数、解压总量、压缩比与必要成员门禁，解析失败时也可留下文件。

## 2. 行为规格

### S1：SQLite 一致快照

- 在线备份必须使用 Python `sqlite3.Connection.backup()` 生成单文件快照，禁止将活跃主库与 WAL/SHM 分别拷贝。
- 快照入包前必须通过 `PRAGMA quick_check`；报告 `foreign_key_check` 数量但不输出用户数据。
- ZIP 遍历必须排除 `papers.db` / `papers.db-wal` / `papers.db-shm`；`include_db=False` 时三者均不得进包。
- 手动导出与自动备份共用同一 service，Electron 根目录数据库必须以 `data/papers.db` 入包。
- 自动备份先写同目录临时文件，flush/fsync 后 `os.replace`；失败不留半包。

### S2：数据库完整性与副本修复

- 已存在数据库在任何 create/migrate 写入前执行 quick check；主库损坏必须 fail-close，不得继续启动 LLM 检查或备份线程。
- Schema/FTS 迁移异常必须传播，只有全部成功后才记录启动完成。
- audit 只输出 quick-check 状态、外键违反总数与白名单孤儿计数。
- repair 只允许作用于一致快照副本；白名单仅删除引用不存在 paper 的 `paper_tags` 行，保留所有合法关联，二次执行幂等。
- Batch 18 不提供“解压并覆盖真实数据”的一键恢复。

### S3：Chroma 隔离重建与换入门禁

- 从 SQLite chunks 在真实目录的同父目录临时新库重建，不原地 delete/rebuild。
- 换入前必须校验 SQLite 期望 ID 集合与 Chroma 实际 ID 集合完全相等、count 相等、embedding 维度正确，并通过 query smoke。
- 先将旧库原子 rename 为带时间戳备份，再换入新库；任一校验/换入失败保留旧库并清理临时库。
- 重建是显式管理动作，不得在启动、评测或普通检索中自动执行。
- hybrid 评测使用显式隔离快照/collection；不存在时 fail-close，不得在真实数据目录调用 `get_or_create_collection`。

### S4：PDF/DOCX 内容门禁与清理

- PDF 必须同时满足 `.pdf` 扩展名与 `%PDF-` 文件头；伪装扩展名返回 400，不建库、不留文件/笔记。
- DOCX 必须是合法 ZIP，包含 `[Content_Types].xml` 和 `word/document.xml`，且成员路径不得绝对路径或穿越。
- DOCX 默认上限：ZIP 成员数 2048、单成员解压 32MiB、总解压 128MiB、单成员压缩比 100；超限返回 400。
- 校验在解析前完成；上传、校验、解析、DB commit 任一失败都回滚并清理该文件及关联孤儿。

### S5：指标提升纪律

- 只使用 private dev 24 条选择单变量；Batch 17 已查看的 holdout 不再用于调参或本批验收。
- 优先修复 factoid 的数值/实体锨点，不得为单题加特例问题或答案。
- dev Recall@5 不得回退，主判 NDCG@5，MRR 辅助；报告同时记录分题型指标和 P95。

## 3. 验收标准

1. WAL 未 checkpoint 的最新提交能从 ZIP 单文件快照读取，包内无 WAL/SHM；手动与自动备份共用实现。
2. 损坏库与迁移错误 fail-close；副本 repair 仅删除孤儿 `paper_tags`，真实源库哈希/行数不变。
3. Chroma 重建失败不替换旧库；成功时 SQLite/Chroma ID 集合一致并保留可恢复旧库备份。
4. 伪装 PDF、缺成员 DOCX、路径穿越与 ZIP bomb 用例均被拒绝，解析失败无文件/DB 孤儿。
5. 指标实验只使用 dev，公开冻结基准与全量工程 Gate 无回退，生成去标识化测试报告。
