# Batch 9：配置持久化与本机部署边界技术计划

## 1. 目标

实现规格 AC1–AC5，消除示例模板泄密、非原子写入、失败状态漂移和宿主机暴露风险。

## 2. 技术方案

`Config.save()` 先把 YAML 写入目标目录中的临时文件，刷新后用 `os.replace()` 原子替换目标，并显式设置 `0600`。若当前读取路径以 `.example` 结尾，目标切换为去掉 `.example` 的私有配置文件。异常路径清理临时文件并保留原目标。

设置路由在修改前深拷贝内存配置；`save()` 抛错时恢复快照。部署侧只调整宿主机入口：直接启动用 `127.0.0.1`，Compose 端口使用 `127.0.0.1:宿主端口:容器端口`；容器内部仍监听 `0.0.0.0`。

## 3. 涉及文件

- 修改：`backend/app/core/config.py` — 安全原子保存。
- 修改：`backend/app/routers/settings.py` — 保存失败回滚。
- 测试：`backend/tests/test_config_save.py` — 配置持久化契约。
- 测试：`backend/tests/test_settings_put.py` — 内存回滚。
- 测试：`backend/tests/test_local_bindings.py` — 仓库部署边界。
- 修改：`scripts/start-demo.sh`、`docker-compose.yml`、`README.md`、`docs/DEPLOY.md`。
- 同步：`specs/backend/core/config.md`、`specs/backend/routers/settings.md`。

## 4. 数据模型 / 接口变更

无。HTTP 接口形状不变。

## 5. 依赖变更

无。

## 6. 风险与权衡

| 风险 | 影响 | 缓解 |
|------|------|------|
| Windows 临时文件权限语义不同 | 权限断言不稳定 | 权限测试仅在 POSIX 执行 |
| Docker 内部误改为回环 | 宿主机无法访问容器 | 只限制 Compose 的宿主机映射 |
| 保存失败回滚覆盖并发修改 | 理论上的状态覆盖 | 单用户、单进程设置写入，接受此边界 |

## 7. 验证方式

- 定向测试先 RED 后 GREEN。
- `env -u PYTHONPATH venv/bin/python -m pytest tests/ -q` 全绿。
- `git diff --check`。
