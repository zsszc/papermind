# Batch 11：前端依赖安全升级技术计划

## 1. 目标

把 npm audit 的 12 项已知漏洞收敛为 0，并保持 PDF Viewer 与生产构建可用。

## 2. 技术方案

按审计给出的安全主版本升级 `react-pdf` 与 Vite；同步升级 `@vitejs/plugin-react` 以满足 peer 约束。PDF worker 改为 `new URL('pdfjs-dist/build/pdf.worker.min.mjs', import.meta.url)`，样式改用新版公开路径。删除未使用的 router 依赖和 Vite 手工分包项。

## 3. 涉及文件

- 修改：`frontend/package.json`、`frontend/package-lock.json`。
- 修改：`frontend/src/components/PdfViewer.jsx`、`frontend/vite.config.js`。
- 删除：`frontend/public/pdf.worker.min.js`。

## 4. 数据模型 / 接口变更

无。

## 5. 依赖变更

- `react-pdf` 7 → 10。
- `vite` 5 → 8；`@vitejs/plugin-react` 升级到相容版本。
- 移除 `react-router-dom`。

## 6. 风险与权衡

| 风险 | 影响 | 缓解 |
|------|------|------|
| react-pdf 主版本 API/CSS 变化 | PDF 页面不可用 | 按官方公开路径改造并 production build |
| Vite 8 Node 要求提高 | 旧 Node 无法开发 | 当前 Harness Node 22；README 标注 Node 20.19+ / 22.12+ |
| 无组件自动化测试 | 交互回归可能漏检 | 后续路线 P1 首项补前端测试与 PDF 冒烟 |

## 7. 验证方式

- 升级前 npm audit 为 RED。
- `npm run lint`、`npm run build`。
- npm 官方 registry `npm audit --json` 为 0。
