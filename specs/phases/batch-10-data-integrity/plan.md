# Batch 10：文献处理与数据库一致性技术计划

## 1. 目标

实现规格 AC1–AC6，保证删除、重处理和检索缓存之间状态一致。

## 2. 技术方案

在现有 SQLite connect 事件中增加 `foreign_keys=ON`，测试引擎复用同一初始化函数。删除路由在主记录删除前执行参数化 ORM 清理：引用边删除、记忆来源置空。

处理器缺文件直接抛出 `FileNotFoundError`。手动端点复用模块级 paper lock，冲突返回 409，并防御性校验处理结果；锁的释放与注册表移除在注册表锁内完成，消除释放后被复用再误删注册项的竞态。

为 `SimpleCache` 增加按前缀清理方法，`VectorStore.add_chunks` 和 `delete_by_paper_id` 在数据变化边界调用。删除失败也在 `finally` 中失效缓存。

## 3. 涉及文件

- 修改：`backend/app/database.py`。
- 修改：`backend/app/routers/papers.py`、`backend/app/routers/chat.py`。
- 修改：`backend/app/services/processor.py`、`cache.py`、`retrieval.py`。
- 修改：`backend/tests/conftest.py`。
- 新增/修改测试：数据库完整性、处理状态、缓存失效。
- 同步相应 `specs/backend/` 规格。

## 4. 数据模型 / 接口变更

- 无新增字段，不需要 `ensure_schema()` 列迁移。
- 手动处理并发冲突新增 HTTP 409 行为；错误响应仍为通用文案。

## 5. 依赖变更

无。

## 6. 风险与权衡

| 风险 | 影响 | 缓解 |
|------|------|------|
| 开启外键暴露历史孤儿数据 | 某些写入/删除失败 | 路由补显式清理；后续增加离线完整性检查工具 |
| 清除全部语义缓存降低短时命中率 | 增删后下一次查询需重算 | 正确性优先，缓存 TTL 仅 60 秒且单用户规模小 |
| 手动处理由 200 变为 409 | 前端需展示错误 | Axios 已统一展示后端错误；不改变成功路径 |

## 7. 验证方式

- 定向测试 RED → GREEN。
- 后端全量 pytest。
- 前端 lint/build（接口成功形状未变，验证无回归）。
- `git diff --check`。
