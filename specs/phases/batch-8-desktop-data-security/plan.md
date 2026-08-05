# Batch 8：桌面端数据隔离与运行时路径 技术计划

## 1. 技术方案

- 在 `core/config.py` 增加 `runtime_root`，集中表达项目根/应用数据根切换。
- 各业务模块只通过 `config.runtime_root` 拼接可变目录；数据库位置保持既有兼容契约。
- 数据库中的文件路径保持相对运行时根目录，不写绝对路径。
- electron-builder 删除真实配置与数据目录的 `extraResources` 项。
- 先新增失败测试，确认现状因正确原因失败，再做最小实现。

## 2. 风险与回退

- 风险：测试曾 monkeypatch 模块级 `PROJECT_ROOT`。静态路由保留可注入根变量或改为可 monkeypatch 的 helper。
- 风险：开发模式已有数据必须零迁移。未设置环境变量时根目录保持项目根。
- 回退：删除 `PAPERMIND_DATA_DIR` 即回到开发路径行为。

## 3. 验证

1. 专项测试 RED → GREEN。
2. `env -u PYTHONPATH venv/bin/python -m pytest tests/ -q`。
3. `npm run lint && npm run build`。
4. `git diff --check` 与敏感文件打包配置人工复核。

