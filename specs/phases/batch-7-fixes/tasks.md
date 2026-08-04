# Batch 7 缺陷修复 TDD 任务分解

> 每个任务严格 RED → GREEN → REFACTOR → COMMIT。
> 实现前必须先读对应源码全文（代码块为契约示意，以源码为准）。
> 测试命令统一：`cd backend && env -u PYTHONPATH venv/bin/python -m pytest <目标> -v`

## 任务清单

- [ ] T1：F1 组合过滤 500 修复
- [ ] T2：F2 配额类错误专属文案
- [ ] T3：F3 备份包 API Key 剥离
- [ ] T4：F4 图片上传大小限制 + 异常脱敏
- [ ] T5：F5 batch_size 透传
- [ ] T6：F6 chunk_size/chunk_overlap 配置接入
- [ ] T7：F7 cache 注释纠偏 + 全套件回归 + 规格同步 + 复盘文档追加

---

### T1：F1 组合过滤 500 修复

**目标**：`year_gte`+`year_lte` 组合过滤不再 500。

**Step 1（RED）**：在 `tests/test_search.py` 新增用例——构造多顶层键 where 调用 `_build_where`（或对 `/api/search` 发组合过滤请求），断言不抛 ValueError / 返回 200。

**Step 2（验证 RED）**：预期 FAIL——当前实现必抛 `ValueError`（ChromaDB 0.4.24 不接受多顶层键 where）。

**Step 3（GREEN）**：`services/retrieval.py` 的 `_build_where` 在多键时返回 `{"$and": [{k: v}, ...]}`；调用处 catch 异常降级为无过滤检索 + 日志。

**Step 4（验证 GREEN）**：新用例 PASS；`pytest tests/test_search.py -q` 全绿。

**Step 5（REFACTOR）**：无（保持最小改动）。

**Step 6（COMMIT）**：`fix(retrieval): 组合过滤 where 包装为 $and 形式，修复 /api/search 500`

---

### T2：F2 配额类错误专属文案

**目标**：429 配额/冻结类错误返回明确文案，不再误报"负载过高"。

**Step 1（RED）**：新建 `tests/test_llm.py`——mock sync_client 抛出带 `exceeded_current_quota_error` 的 APIError，断言格式化文案含"额度"或"冻结"；另补两个不回归用例（engine_overloaded → "负载过高"；401 → 认证失败）。

**Step 2（验证 RED）**：预期 FAIL——当前实现把配额错误归入"负载过高"文案。

**Step 3（GREEN）**：`services/llm.py` 错误格式化函数新增配额类识别分支。

**Step 4（验证 GREEN）**：新文件全 PASS。

**Step 5（COMMIT）**：`fix(llm): 429 配额/冻结类错误返回专属文案，不再误报负载过高`

---

### T3：F3 备份包 API Key 剥离

**目标**：备份包内 config.yaml 的 `llm.api_key` 为 `[REDACTED]`。

**Step 1（RED）**：新建 `tests/test_backup.py`——临时目录构造含 api_key 的 config.yaml，调 `create_backup()`，解包断言包内 config.yaml 不含原 key、含 `[REDACTED]`，磁盘原文件不受影响。

**Step 2（验证 RED）**：预期 FAIL——当前实现原样入包。

**Step 3（GREEN）**：`services/backup.py` 写包前生成脱敏副本（yaml 加载→替换→序列化，失败则跳过该文件）。

**Step 4（验证 GREEN）**：新用例 PASS。

**Step 5（COMMIT）**：`fix(backup): 备份包内 config.yaml 剥离明文 API Key（宪法第14条）`

---

### T4：F4 图片上传大小限制 + 异常脱敏

**目标**：>10MB 图片 → 413；分析异常 → 通用文案不透传原文。

**Step 1（RED）**：新建 `tests/test_chat_image.py`——超大文件上传断言 413；mock analyzer 抛异常断言响应无异常原文。

**Step 2（验证 RED）**：预期 FAIL（当前无大小限制、异常原文透传）。

**Step 3（GREEN）**：`services/image_analyzer.py`（或路由层）加大小校验与异常转译。

**Step 4（验证 GREEN）**：新用例 PASS。

**Step 5（COMMIT）**：`fix(image): analyze-image 增加 10MB 上限与异常脱敏（宪法第13条）`

---

### T5：F5 batch_size 透传

**目标**：`embed(texts, batch_size=N)` 的 N 到达底层 encode。

**Step 1（RED）**：新建 `tests/test_embedding.py`——mock 模型 encode，调用 embed 传 batch_size=3，断言底层收到 3。

**Step 2（验证 RED）**：预期 FAIL（当前 worker 恒用 8）。

**Step 3（GREEN）**：worker 调用透传 batch_size。

**Step 4（验证 GREEN）**：PASS。

**Step 5（COMMIT）**：`fix(embedding): embed() 透传 batch_size 至 worker`

---

### T6：F6 chunk 配置接入

**目标**：TextChunker 默认读取 `embedding.chunk_size`/`chunk_overlap`，非法值回退 512/50。

**Step 1（RED）**：在 `tests/test_embedding.py` 追加——mock config 返回自定义值，断言 TextChunker 默认参数采用之；非法值用例断言回退。

**Step 2（验证 RED）**：预期 FAIL（当前不读配置）。

**Step 3（GREEN）**：TextChunker 初始化接入配置。

**Step 4（验证 GREEN）**：PASS。

**Step 5（COMMIT）**：`fix(embedding): TextChunker 接入 chunk_size/chunk_overlap 配置（消灭死配置）`

---

### T7：F7 注释纠偏 + 收官

**目标**：cache.py 注释改为"最早过期驱逐"；全套件回归；同步 6 份模块规格第 7 节；复盘文档追加 Batch 7 实录。

**步骤**：
1. 改注释（无测试，纯文档）
2. `pytest tests/ -q` 全绿
3. 更新 specs/backend/services/{retrieval,llm,backup,image_analyzer,embedding,cache}.md 的第 7 节（盲区标记为已修复）
4. 追加 PaperMind_构建与改进复盘.md 第三部分
5. COMMIT：`docs(specs): Batch 7 收官——规格同步与注释纠偏`

---

## 执行纪律

- 每个 T 的 Step 2 必须真实看到失败，失败原因必须是"功能缺失"而非测试写错
- 任何一步全套件变红立即停下修复
- 遇 API 403（用量超限）：静默挂定时器等重置；429：冷却 5 分钟重派
