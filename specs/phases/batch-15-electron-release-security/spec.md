# Batch 15 规格：Electron 发布安全基线

## 1. 背景

Batch 14 已建立 Electron 无 GUI 的生命周期 Harness，但桌面发布仍被依赖审计和运行边界阻断：Electron 29 / electron-builder 24 的官方 npm audit 基线为 13 项（1 critical / 12 high），窗口缺少 CSP、导航与新窗口限制、权限默认拒绝和单实例约束；安装包也没有自动化资源与敏感信息检查。

本批只处理桌面壳与发布制品安全。固定端口和后端身份校验将在 Batch 16 通过随机端口与每次启动能力令牌解决，避免把两个高风险迁移混在一次变更中。

## 2. 行为规格

### S1：依赖安全

- Electron 升级到 43.x，electron-builder 升级到 26.x，并锁定实际安装版本。
- `npm audit --audit-level=high` 必须为 0；现有 Node Harness 与语法检查不得回退。
- 升级不得改变后端进程的固定端口、启动参数和数据目录语义。

### S2：渲染器最小权限

- BrowserWindow 保持 `contextIsolation=true`、`nodeIntegration=false`，并显式启用 sandbox 与 webSecurity、禁止不安全混合内容。
- 生产页与开发页使用一条可验证的 CSP：默认同源，仅额外允许本机 API、开发 HMR、内联样式、data/blob 图片与 PDF worker；禁止 object、frame 和任意外部脚本。
- preload 只暴露既有只读环境信息，不新增 Node、文件或进程能力。

### S3：导航、弹窗与权限

- 主窗口只允许当前受信入口导航：开发环境为 `localhost/127.0.0.1:5173`，生产环境为本地 `file:` 页面。
- `window.open` 一律拒绝；不把未验证 URL 交给系统浏览器。
- session 的权限检查、权限请求和设备权限默认拒绝，未知或新增权限同样拒绝。

### S4：单实例

- 启动时必须获取 single-instance lock；失败则立即退出且不得创建窗口/后端进程。
- 第二次启动时恢复、聚焦已有主窗口；已有窗口尚未创建时安全忽略。

### S5：发布制品最小化与扫描

- `app.asar` 必须包含 `main.js`、`preload.js`、`backend-lifecycle.js` 与本批安全策略模块。
- backend 资源排除测试、评测报告、缓存、日志、数据库和历史误生成文件；仅打包公开配置模板，不打包真实 `config.yaml`。
- 提供可在 CI/本地运行的制品扫描器：校验必需文件、拒绝用户数据/密钥路径，并对小型文本配置执行常见密钥模式检查。
- 实际 unpacked 应用构建完成后执行扫描；扫描失败时发布 Gate 失败。

## 3. 验收标准

1. 安全策略与制品扫描至少各经历一组 RED → GREEN。
2. Electron node:test、main/纯模块语法、frontend test/lint/build 全绿。
3. Electron 与 frontend 官方 npm audit 均为 0。
4. 构建至少一个本机 unpacked 应用并通过资源/密钥扫描。
5. 后端全量 pytest 不回退；生成测试报告并更新计划台账。

