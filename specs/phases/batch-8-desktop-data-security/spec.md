# Batch 8：桌面端数据隔离与运行时路径 规格说明书

## 1. 背景与目标

Electron 生产包设置 `PAPERMIND_DATA_DIR` 后，SQLite 配置路径已部分重定向，但 PDF、笔记、概括、论文、向量库、日志、备份与静态文件仍锚定安装包 resources。与此同时，electron-builder 会把真实 `config.yaml` 和本地数据目录直接打进分发包。

本批次目标是保证：分发包不携带密钥/个人数据；所有可变运行时文件在桌面生产环境统一落到应用数据目录；开发模式行为保持不变。

## 2. 范围

- 新增统一运行时根目录契约。
- PDF、笔记、概括、论文、向量库、日志、备份、静态文件与处理流水线统一使用运行时根目录。
- electron-builder 仅打包配置模板，不打包真实配置与任何个人数据目录。
- 修复两个仍会向客户端透传异常原文的 500 响应。

不包含：迁移旧桌面数据、改变 SQLite 当前 `PAPERMIND_DATA_DIR/papers.db` 位置、UI 迁移。

## 3. 行为契约

1. 未设置 `PAPERMIND_DATA_DIR` 时，运行时根目录为项目根，现有相对路径不变。
2. 设置 `PAPERMIND_DATA_DIR` 时，运行时根目录为该目录，并自动创建。
3. 数据文件的数据库存储值继续使用 `papers/<name>`、`my-thesis/<name>` 等可移植相对路径；解析时相对于运行时根目录定位。
4. `/static` 仍只允许四个白名单目录，并在当前运行时根目录内执行 `resolve()` 防穿越。
5. 备份源、备份输出、日志与 ChromaDB 均使用运行时根目录。
6. 桌面构建配置不得包含 `config.yaml`、`data/`、`papers/`、`notes/`、`summaries/`、`my-thesis/`、`vector_db/`、`logs/`、`backups/`；只允许包含 `config.yaml.example`。
7. 生产首次启动时仅复制配置模板，不从安装包复制真实密钥。
8. 路由层 500 响应不得包含底层异常原文。

## 4. 验收标准

- [x] AC1：开发/生产两种环境的运行时根目录测试通过。
- [x] AC2：上传目录、静态文件、处理流水线、向量库、日志与备份路径均由统一根目录派生。
- [x] AC3：electron-builder 配置不再打包真实配置与个人数据。
- [x] AC4：生产配置初始化只复制 `config.yaml.example`，已有用户配置不覆盖。
- [x] AC5：两个已知异常透传端点返回通用文案。
- [x] AC6：后端全套件、前端 lint/build 全绿。
