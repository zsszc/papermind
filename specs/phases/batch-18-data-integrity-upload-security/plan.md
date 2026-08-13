# Batch 18 实施计划

## 1. 范围

- `backend/app/services/backup.py` / `routers/export.py`：SQLite 在线快照与统一备份。
- `backend/app/services/data_integrity.py` / `database.py` / `main.py`：完整性 audit、启动 fail-close 与副本 repair。
- `backend/app/services/vector_rebuild.py` / `retrieval.py` / `eval/run.py`：隔离向量库重建、校验和评测边界。
- `backend/app/services/upload_validation.py` / `routers/papers.py` / `routers/thesis.py`：文件内容与解压资源门禁。
- `backend/eval/run.py`：只在 dev 中增加数值/实体检索单变量。
- `backend/tests/`、`docs/test-reports/`、开发台账。

## 2. 微循环顺序

1. 先写一致快照、WAL、include_db 与手动导出 RED，再统一备份 service。
2. 写损坏库/迁移 fail-close 和副本 repair RED，仅在副本验证白名单修复。
3. 写 PDF/DOCX 内容门禁及异常清理 RED/GREEN。
4. 写 Chroma 临时新库、完整性 Gate 与换入回滚 RED/GREEN；单元测试使用小向量，真库执行前先生成完整备份。
5. 只在 private dev 运行一个数值/实体单变量；不读取 holdout 报告做选择。
6. 每个绿色节点独立提交；最后运行后端、前端、Electron、公开 RAG 与安全审计 Gate。

## 3. 风险控制

- 真实 `papers.db` 与 `vector_db/` 默认只读；任何换入前必须有可验证备份和显式换入阶段。
- 不删除 17 个重复 PDF，不修改私有 QA/holdout，不把任何论文文本写入 Git。
- 不原地“修补”已失配 HNSW；失败注入测试要证明旧库仍可恢复。
- 本批不调用外部 LLM，词法/向量实验完全本地运行。
