# Batch 7 缺陷修复 技术计划

## 1. 目标

实现同目录 spec.md 的 F1-F7，每项严格 TDD（RED→GREEN→REFACTOR→COMMIT），全套件保持绿色。

## 2. 技术方案

| 修复 | 方案 | 涉及文件 |
|------|------|----------|
| F1 | `_build_where` 在多顶层键时包装为 `{"$and": [{k1: v1}, {k2: v2}]}`；检索调用处 catch ValueError 降级为无过滤检索并记日志 | `services/retrieval.py` |
| F2 | `llm.py` 错误格式化处新增配额类识别分支（type=exceeded_current_quota_error 或消息含 insufficient balance/suspended）→ 专属文案 | `services/llm.py` |
| F3 | `backup.py` 写包前将 config.yaml 内容读入内存、正则/yaml 替换 api_key 值为 [REDACTED]、以脱敏副本入包 | `services/backup.py` |
| F4 | image_analyzer 路由/服务加大小校验（>10MB → 413）；异常统一转通用文案 | `services/image_analyzer.py`（+`routers/chat.py` 如需） |
| F5 | embedding worker 调用透传 batch_size | `services/embedding.py` |
| F6 | TextChunker 默认参数从 config.get("embedding.chunk_size"/"chunk_overlap") 读取，非法值回退默认 | `services/embedding.py` |
| F7 | 注释修正 | `services/cache.py` |

## 3. 测试文件

- 修改：`backend/tests/test_search.py`（F1）
- 新建：`backend/tests/test_llm.py`（F2）、`test_backup.py`（F3）、`test_chat_image.py`（F4）、`test_embedding.py`（F5/F6）

## 4. 数据模型 / 接口变更

无 DB 变更。接口行为变更：`analyze-image` 新增 413 响应（向前兼容——前端已有通用错误处理）。

## 5. 依赖变更

无。

## 6. 风险与权衡

| 风险 | 缓解 |
|------|------|
| F1 改动影响现有单键过滤路径 | 测试覆盖单键/多键/无过滤三态 |
| F3 脱敏副本书写引入 IO 差异 | 测试直接断言包内内容；异常时跳过该文件不中断备份 |
| F6 配置接入改变现有分块行为 | 默认值保持 512/50，全套件回归 |

## 7. 验证方式

1. 每项修复的 RED 证据（先失败）与 GREEN 证据（后通过）记录在提交信息或复盘文档
2. `env -u PYTHONPATH venv/bin/python -m pytest tests/ -q` 全绿
3. 修复完成后同步更新对应模块规格第 7 节（宪法第 20 条）
