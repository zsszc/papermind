# Batch 7b 路由层安全与数据完整性修复 规格说明书

> 缺陷来源：Batch 5 routers 规格反推实证清单（specs/backend/routers/*.md 第 7/8 节）。
> 对应宪法：第 13 条（异常脱敏）、第 14 条（密钥与数据纪律）。
> 计划与任务分解见同目录 plan.md / tasks.md。

## 1. 背景与目标

Batch 5 规格反推发现路由层 6 项缺陷，其中异常原文透传违反宪法第 13 条（安全纪律，同 Batch 7 F4 已修模式），孤儿文件/引用悬空/计数不回溯属数据完整性问题。目标：消灭 3 处异常透传 + 3 项数据完整性缺陷，每项先失败测试后实现。

## 2. 范围

### 2.1 包含

- F8：`papers.py` AI 概括 LLM 失败、`thesis.py` `/analyze` 与 `/suggest-citations` LLM 失败、`settings.py` PUT 保存失败——四处 `HTTPException(detail=f"...{e}")` 不再透传异常原文，改通用文案（原文仅入日志）
- F9：papers 批量导入中途失败时，清理本次已落盘的 PDF/笔记文件（不留孤儿）
- F10：删除 paper 时级联删除 `thesis_citations` 中 `paper_id` 关联行（不留悬空引用）
- F11：`delete_messages_from` 删除消息后回溯修正会话 `message_count`

### 2.2 非目标

- 不改 memory POST 查询参数契约（前端已匹配，记录即可）
- 不改 regenerate 落库绕过依赖注入的架构问题（需独立设计，属后续）
- 不改 chat 非流式分支联网开关（需与前端协同，属 Phase C）
- 不改记忆更新内联 await 的阻塞问题（性能议题，属 Phase D 可观测后评估）

## 3. 行为契约

### 3.1 F8：异常脱敏（四处）

- **触发**：LLM 调用/配置保存抛出任何异常
- **输出**：HTTP 5xx（沿用现有状态码），`detail` 为通用文案（如「AI 概括失败，请稍后再试」），不含异常类型名/消息文本/堆栈
- **副作用**：异常原文 + 堆栈写入 `logs/app.log`（`logger.error(..., exc_info=True)` 或等价）
- **不回归**：成功路径行为不变；前置校验类 4xx（如 400/404）detail 文案不变

### 3.2 F9：批量导入孤儿文件清理

- **触发**：批量导入循环中某篇处理失败（解析/入库异常）
- **输出**：该篇返回错误标记，继续后续篇目（现有语义不变）
- **后置条件**：本次为该失败篇目已落盘的 PDF 与笔记文件被删除；DB 无该篇记录（现有回滚语义保持）；其他成功篇目不受影响

### 3.3 F10：删除 paper 级联清理引用

- **触发**：`DELETE /api/papers/{id}`
- **后置条件**：`thesis_citations` 中所有 `paper_id == id` 的行被删除；其他表级联行为（chunks/annotations/FTS）不回归

### 3.4 F11：message_count 回溯

- **触发**：`DELETE /api/chat/conversations/{cid}/messages/{mid}`（自 mid 起截断删除）
- **后置条件**：会话 `message_count` 等于删除后实际剩余消息数

## 4. 边界条件与错误处理

| 场景 | 期望行为 |
|------|----------|
| F8：异常原文含敏感路径/key | 响应 detail 完全不含原文 |
| F9：文件已不存在（被外部删除） | 清理容错，不抛异常 |
| F9：全部篇目失败 | 所有落盘文件清理完毕 |
| F10：paper 无任何引用 | 正常删除，无影响 |
| F11：删除全部消息 | message_count 归 0 |
| F11：会话不存在 | 404（现有语义不变） |

## 5. 依赖

- specs/backend/routers/{papers,thesis,settings,chat}.md 第 3/7/8 节
- 宪法第 5 条（TDD）、第 13 条（异常脱敏）

## 6. 验收标准（可测试）

- [ ] AC1：mock LLM 抛异常，四处端点 detail 不含异常原文（特征串不出现）
- [ ] AC2：批量导入第二篇注入失败，第一篇成功保留、第二篇无文件残留、返回错误标记
- [ ] AC3：删除有引用的 paper 后 thesis_citations 无对应行
- [ ] AC4：截断删除后 message_count == 实际剩余消息数
- [ ] AC5：全套件 `pytest tests/ -q` 全绿无回归

## 7. 现有测试覆盖与盲区

- F8 四处当前零脱敏测试（Batch 5 实证）
- F9/F10/F11 当前零测试
- 新测试文件：`tests/test_routes_sanitize.py`（F8）、`tests/test_papers_integrity.py`（F9/F10）、`tests/test_chat.py` 追加（F11）

## 8. 关键设计决策

- F8 沿用 Batch 7 F4 的脱敏模式（通用文案 + 日志原文），保持全项目一致
- F9 选「失败即清理」而非「定期扫孤儿」：失败点上下文最全，清理最精确
- F10 选应用层级联（ORM delete 前显式删引用）而非 DB 外键级联：项目无 Alembic，不引入外键约束变更
