# Batch 15 实施计划

## 1. 文件范围

- `electron/security-policy.js`、`electron/main.js`：窗口、导航、权限与单实例策略。
- `frontend/index.html`：开发与 Electron 生产共同适用的 CSP。
- `electron/test/`：纯策略和制品扫描 TDD。
- `electron/scripts/verify-artifact.js`：发布资源与敏感信息扫描。
- `electron/package.json` / lock、`electron-builder.yml`：依赖升级、入口与资源白名单。
- `.github/workflows/ci.yml`：桌面审计和可重复的制品扫描 Gate。

## 2. 顺序

1. 记录 13 项漏洞、6 个 Electron 测试的基线，先提交 SDD。
2. RED：为窗口参数、导航/弹窗/权限、单实例行为写失败测试。
3. GREEN：实现纯策略模块，由 `main.js` 薄接线并加入 CSP。
4. RED：为制品必需文件、禁止路径和密钥特征写失败测试。
5. GREEN：实现扫描器并收紧 electron-builder 资源规则。
6. 升级 Electron/builder，运行 audit、Harness 与实际 unpacked 构建扫描。
7. 运行跨端和后端全量门禁，生成报告、更新台账、分批提交并推送。

## 3. 风险与控制

- Electron 大版本升级：先用 Batch 14 纯生命周期 Harness 锁定后端行为，再升级依赖。
- CSP 误伤 Vite/PDF：显式保留本机 API、HMR、blob worker 和 data/blob 图片，frontend build/test 作为门禁。
- 资源过滤误删运行依赖：扫描器同时检查前后端入口与四个 Electron 模块，实际 unpacked 包再验证一次。
- 密钥误报：只扫描发布制品中的配置/环境类小文本文件，源码中的字段名不作为泄露证据。

