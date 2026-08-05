# Batch 11：前端依赖安全升级规格说明书

## 1. 背景与目标

官方 npm 漏洞审计报告 12 项漏洞（1 critical、8 high、3 moderate）。PaperMind 会打开用户导入的外部 PDF，旧 `react-pdf` 间接依赖的 `pdfjs-dist 3.11.174` 存在恶意 PDF 执行脚本的高风险；旧 Vite 开发服务器也存在路径/源码泄露类问题。本批次在不改变产品行为的前提下升级受影响依赖。

## 2. 范围

### 2.1 包含

- 升级 `react-pdf`/`pdfjs-dist` 到审计建议的安全主版本。
- 升级 Vite 与 React 插件到相容安全版本。
- 移除源码未使用的 `react-router-dom`。
- 更新 PDF worker 与样式导入方式，删除旧 worker 静态副本。

### 2.2 非目标

- 不改页面布局、PDF 批注业务或路由架构。
- 不升级 React、Ant Design、Electron 等无关依赖。
- 不使用 `npm audit fix --force` 做不可控批量升级。

## 3. 行为契约

- PDF Viewer 继续加载同一后端 PDF URL、渲染文本层、禁用 PDF annotation layer，并保持翻页/缩放/批注能力。
- PDF worker 必须与当前 `pdfjs-dist` 版本同源打包，不再维护可能版本漂移的 `public/pdf.worker.min.js`。
- 前端生产构建和 ESLint 必须通过。
- `npm audit --registry=https://registry.npmjs.org` 的 critical/high/moderate 数量均为 0；若上游仍无修复版本，必须在报告中记录例外和可利用性分析。

## 4. 边界条件与错误处理

| 场景 | 期望行为 |
|------|----------|
| PDF 加载失败 | 继续显示错误与下载链接 |
| worker/API 版本不匹配 | 构建期同源引用避免该状态 |
| 主版本 API 变化 | 以 lint/build 失败阻断提交 |

## 5. 依赖

- React 18、Vite、react-pdf、pdfjs-dist。

## 6. 验收标准（可测试）

- [x] AC1：旧 PDF worker 静态副本不再被引用或打包。
- [x] AC2：PDF Viewer 使用当前 pdfjs worker 模块和新版 CSS 路径。
- [x] AC3：`npm run lint` 与 `npm run build` 通过。
- [x] AC4：npm 官方 registry 审计返回 0 漏洞。
- [x] AC5：源码与清单均不再包含未使用的 react-router 依赖。

## 7. 现有测试覆盖与盲区

- 前端目前没有组件测试；本批次以依赖审计、lint 和 production build 为 Harness。
- PDF 交互仍需后续补 Playwright/Electron 冒烟测试。

## 8. 关键设计决策

- 安全升级使用明确版本，不执行强制全树升级。
- worker 通过 `import.meta.url` 从依赖解析，消除手工复制与版本漂移。
