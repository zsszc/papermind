# Batch 7 缺陷修复 规格说明书

> 对应模块规格：specs/backend/services/retrieval.md、llm.md、backup.md、image_analyzer.md、embedding.md、cache.md
> 本规格只描述"修复后应该是什么行为"（行为契约），实现细节见同目录 plan.md，任务分解见 tasks.md。

## 1. 背景与目标

SDD 规格反推（Batch 1-4）实证发现 14 项缺陷，其中 4 项高严重度、3 项中严重度适合立即修复。本规格定义这 7 项修复的行为契约与验收标准。目标：消灭 1 个用户可见 500、1 个误导性错误文案、2 个安全隐患、3 个契约/配置缺陷，每项修复必须先有失败测试（宪法第 5 条）。

## 2. 范围

### 2.1 包含

- F1：`retrieval._build_where` 组合过滤崩溃修复
- F2：LLM 429 配额/冻结类错误的专属文案
- F3：备份包中 `config.yaml` 的 API Key 剥离
- F4：`/api/chat/analyze-image` 上传大小限制 + 异常脱敏
- F5：`embedding.embed()` 的 `batch_size` 形参透传
- F6：`chunk_size`/`chunk_overlap` 配置接入 TextChunker（消灭死配置）
- F7：`cache` 注释纠偏（LRU → 最早过期驱逐；纯注释，无行为变更）

### 2.2 非目标

- 不实现 abstract/journal 提取逻辑重构（属 Phase B 数据质量议题，需单独 spec）
- 不删除 web_search 死代码（需单独决策：删除或接线）
- 不改 `{error}` SSE 帧语义（需与前端协同设计，属 Phase C）
- 不补 characterization 测试（属 Batch 7b）

## 3. 行为契约

### 3.1 F1：组合过滤不再崩溃

- **输入**：`/api/search` 带 `year_gte` + `year_lte`（或任意多顶层键 where）
- **输出**：正常返回过滤后的检索结果，HTTP 200
- **后置条件**：ChromaDB 收到的 where 子句符合其语法（多条件经 `$and` 组合）
- **异常**：任何 where 构造异常不得冒泡为 500——检索层降级为无过滤检索并在响应/日志中标注

### 3.2 F2：配额类错误专属文案

- **输入**：Kimi 返回 429 且 `error.type` 为 `exceeded_current_quota_error`（或消息含 insufficient balance / suspended）
- **输出**：`llm.py` 格式化文案明确告知"账户额度不足或已冻结，请检查 Moonshot 控制台"
- **不回归**：`engine_overloaded_error` 仍报"负载过高"；401 仍报认证失败；timeout 仍报超时

### 3.3 F3：备份包不含明文密钥

- **输入**：`create_backup()`（自动与手动两路径）
- **输出**：备份包内 `config.yaml` 的 `llm.api_key` 值替换为 `[REDACTED]`（其余配置原样）
- **后置条件**：磁盘上的真实 `config.yaml` 不受影响；备份注释与实际行为一致
- **验收**：解包备份，grep 不到真实 key 前缀

### 3.4 F4：图片上传安全

- **输入**：`POST /api/chat/analyze-image` 上传图片
- **输出**：超过大小上限（10MB）→ HTTP 413；分析过程任何异常 → 通用错误文案 + error_code，不透传异常原文
- **边界**：合法图片（<10MB、白名单 MIME）正常分析

### 3.5 F5：batch_size 透传

- **输入**：`embed(texts, batch_size=N)`
- **输出**：worker 以 N 调用底层 encode（默认保持现状 8）
- **不回归**：默认调用路径行为不变

### 3.6 F6：chunk 配置生效

- **输入**：`config.yaml` 的 `embedding.chunk_size` / `chunk_overlap`
- **输出**：`TextChunker` 初始化默认读取这两个配置项；显式传参优先于配置
- **不回归**：配置缺失时使用现有硬编码默认值（512/50）

### 3.7 F7：注释纠偏

- cache.py 的驱逐策略注释改为与实际行为一致（最早过期驱逐）；零行为变更

## 4. 边界条件与错误处理

| 场景 | 期望行为 |
|------|----------|
| F1：仅单个过滤键 | 保持现有单键 where 形式（不包 $and） |
| F1：无过滤条件 | 不传 where |
| F2：429 但非配额类（如 engine_overloaded） | 维持原"负载过高"文案 |
| F3：config.yaml 本身缺失 | 备份跳过该文件，不中断 |
| F4：恰好 10MB | 边界值放行或拒绝需在测试中钉死（建议放行 ≤10MB） |
| F6：配置值为非法类型（字符串等） | 回退默认值，不崩溃 |

## 5. 依赖

- retrieval/llm/backup/image_analyzer/embedding/cache 现有模块规格
- 宪法第 5 条（TDD）、第 13/14 条（安全纪律）

## 6. 验收标准（可测试）

- [ ] AC1：组合过滤测试通过（修复前必现 500/ValueError，修复后 200）
- [ ] AC2：配额错误文案测试通过（mock 429 quota → 文案含"额度"或"冻结"）
- [ ] AC3：备份包内 config.yaml 无明文 key（测试断言包内文件内容）
- [ ] AC4：超 10MB 图片 → 413；分析异常 → 无原文透传
- [ ] AC5：batch_size 透传测试通过（mock encode 收到指定值）
- [ ] AC6：chunk_size/chunk_overlap 从配置读取的测试通过
- [ ] AC7：全套件 `pytest tests/ -q` 全绿无回归

## 7. 现有测试覆盖与盲区

7 项修复全部先写失败测试（RED），测试文件归位：`test_search.py`（F1）、`test_llm.py`（F2，新建）、`test_backup.py`（F3，新建）、`test_chat_image.py`（F4，新建或并入现有）、`test_embedding.py`（F5/F6，新建）。

## 8. 关键设计决策

- F1 选"规范化 where 为 $and 形式"而非"路由层 catch 500"：治本且保留过滤能力
- F3 选"备份时写入脱敏副本"而非"排除 config.yaml"：备份仍含完整配置结构，恢复可用
- F4 上限定 10MB：病理截图/图表足够，与 PDF 50MB 上限区分
