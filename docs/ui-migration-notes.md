# PaperMind UI Redesign（Figma Make 导出包）盘点笔记

> 侦察日期：2026-07-29 ｜ 来源：`UI Redesign for PaperMind.zip`（Figma Make 导出）
> 原始设计稿：https://www.figma.com/design/FQWUXNVxUGYuMxyiWHzmm4/UI-Redesign-for-PaperMind
> 结论一句话：**这是一套高保真静态原型**——页面结构、布局、视觉体系完整可用，但没有任何数据层，所有内容均为写死的 mock，核心业务组件（PDF 预览、SSE 对话）只有"样子"没有"里子"。

---

## 1. 技术栈与关键依赖（package.json）

| 类别 | 实际使用 | 说明 |
|---|---|---|
| 框架 | React 18.3（peerDep）+ TypeScript + Vite 6.3.5 | ESM，`"type": "module"` |
| 样式 | Tailwind CSS **4.1**（`@tailwindcss/vite`，CSS-first 配置，无 tailwind.config.js） | ⚠️ 但业务页面 95% 是 **inline style 硬编码 hex**，Tailwind 工具类只用了一点点 |
| 组件库 | shadcn/ui 全套 **44 个组件**（`src/app/components/ui/`，Radix 底层） | ⚠️ **业务页面一个都没 import，全部是死代码**（Figma Make 模板自带） |
| 图标 | lucide-react 0.487 | 唯一被业务页面大量使用的库 |
| 图表 | **Recharts 2.15.2**（StatsPage 直接用） | 旧前端用 ECharts 6 —— 技术栈不一致 |
| 面板分割 | react-resizable-panels 2.1.7 | PaperDetailPage 三栏直接使用 |
| 路由 | react-router 7.13 已装但 **完全未用** | App.tsx 用 `useState<Page>` 切换视图 —— 与旧前端模式一致，迁移友好 |
| 主题 | next-themes 0.4.6 已装未用 | theme.css 有 `.dark` token，但页面硬编码颜色，暗色模式**实际不可用** |
| 模板残留（可裁剪） | @mui/material + @emotion、react-slick、react-dnd、embla-carousel、vaul、motion、canvas-confetti、react-responsive-masonry、react-popper、cmdk、input-otp、react-day-picker 等 | Figma Make 模板全家桶，业务代码零引用 |

**与旧前端的关键差异**：JSX→TSX、AntD→（名义 shadcn/实际手写）、ECharts→Recharts、无 axios/fetch 任何数据层。

---

## 2. 页面映射表（新 UI ↔ 现有 8 页）

| 新 UI 页面 | 对应现有页面 | 对应关系 | 说明 |
|---|---|---|---|
| `LibraryPage` | PaperList（文献库） | ✅ 直接对应 | 网格/列表双视图、状态筛选、排序、空态、标签 chip（auto/manual 区分）；「导入文献」按钮是 no-op |
| `PaperDetailPage` | PaperDetail（三栏） | ✅ 结构对应 | react-resizable-panels 三栏（PDF 45% / 笔记 30% / AI 25%，AI 栏可关闭），与旧布局思路一致；内容全假 |
| `SearchPage` | SearchPage（语义检索） | ✅ 直接对应 | 有 **语义/关键词/混合三模式切换**（正好对应后端 RRF 混合检索）、搜索历史、相关度分数、snippet 高亮、模型加载态 |
| `ThesisPage` | ThesisList **+** ThesisDetail | 🔀 **二合一**（IA 变更） | 单页内：版本列表 + 章节字数条 + 引用检测报告（匹配/未匹配）。迁移时把旧两页逻辑合并进一页 |
| `WritingDeskPage` | WritingDesk（6 Skill） | ✅ 直接对应 | 章节侧栏 + 编辑器 + 6 张技能卡（中英翻译/学术润色/方法对比/大纲生成/数据分析/写作助手，与后端 6 Skill 一一对应）+ AI 输出面板（替换/插入操作）；输出是 setTimeout 假数据 |
| `StatsPage` | StatsPage（ECharts 统计） | ⚠️ 对应但图表栈不同 | 4 张汇总卡 + 4 图（年度趋势/期刊分布/阅读状态饼图/热门标签条形图），全部 Recharts |
| `ExportPage` | DataExport | ✅ 对应 + 新增 | Excel/CSV/备份包三种导出 + 导出记录 + **自动备份时间线**（新增 UI，旧版没有，正好对接后端每日 3 点备份） |
| `SettingsModal` | SettingsModal | ✅ 直接对应 | 四个分区：LLM 配置 / 检索配置 / 存储路径 / 关于。⚠️ mock 默认值写的是 Anthropic/Claude（`api.anthropic.com`、`claude-sonnet-4-6`），需改回 Kimi 配置 |
| `Sidebar`/`TopBar` | App.jsx 主布局 | ✅ | 可折叠侧栏、LLM 连接状态指示（写死 true）、全局搜索框、导入按钮（均 mock） |

**新增（旧版没有）**：备份时间线 UI、检索混合模式切换、阅读状态三态徽标体系、PDF 划词浮动工具条（高亮/备注/发给 AI）。
**缺失（旧版有、新 UI 无对应）**：见第 5 节。

---

## 3. 组件清单

### 3.1 自绘业务组件（全部页面内联，未抽公共组件）

| 组件 | 所在 | 可复用性评估 |
|---|---|---|
| `PaperCard` / `StatusBadge` / `TagChip` | LibraryPage | 视觉好，需抽成独立组件并接真实数据 |
| `PDFViewer`（假） | PaperDetailPage | **只有外壳**：工具栏（缩放/翻页，页码硬编码 `/15`）+ 划词浮动条 + 硬编码高亮 mark。内容是一篇写死的 Attention 论文纯文本，**无 react-pdf/pdfjs** |
| `AIChatPanel` + `CitationChip` | PaperDetailPage | 气泡、引用 chip（hover tooltip、点击跳转占位）、三点 streaming 动画都有；但回复是 `setTimeout(1500)` 假数据 |
| `NotesPanel` | PaperDetailPage | textarea 编辑 + 预览切换；⚠️ 预览用**正则替换 + `dangerouslySetInnerHTML`** 渲染 Markdown，迁移时必须换成 react-markdown |
| `HighlightedText`（snippet 高亮） | SearchPage | 可按后端返回的偏移量直接复用思路 |
| `ChapterWordBar`、引用检测表 | ThesisPage | 直接可用 |
| Skill 卡片 + AI 输出面板 | WritingDeskPage | 结构可用，接 `/api/chat/skills` |
| `Toggle` 等表单件 | SettingsModal | 手写，可换成 shadcn Switch |

### 3.2 shadcn/ui 组件
44 个全部存在（button/card/dialog/tabs/select/table/sidebar/chart/sonner…），**业务代码零引用**。迁移策略：留用 button/dialog/select/tabs/switch/sonner/chart 等常用的，其余连同 @mui 等残留依赖一起删除以瘦身。

---

## 4. 数据层

- **完全没有 API 调用代码**：全仓库 grep 不到 `fetch`/`axios`/`EventSource`/`ReadableStream`，没有 baseURL 概念，后端地址无处可写。
- 唯一数据源：`src/app/data/mockData.ts`（12 篇 mock 文献 + 4 组图表数据）。
- ⚠️ **mock 类型与后端 schema 不一致**，需做字段映射：
  - `id: string`（后端 int）；`authors: string[]`（后端单字符串）；`tags: {id,label,type}[]`（后端标签关联表）；`status` 三态命名一致（unread/reading/read ✅）；`pages`/`citationCount`/`importedAt` 后端无对应字段。

## 5. 样式体系

- **设计 token**（theme.css）：暖纸学术风。`--background #FAF8F4`（米纸）、`--primary #2B4C7E`（墨蓝）、辅助色 sage `#5F8D6E` / amber `#B98A2F` / red `#C8433C` / purple `#7B6EA8`；圆角 0.5rem。
- **字体**：Inter（西文）+ Noto Serif SC（标题/正文衬线）+ Noto Sans SC + Cascadia Code（代码）。⚠️ `index.css` 里 `@import url('https://fonts.googleapis.com/...')` **外链 Google Fonts——Electron 离线环境会失效，必须字体本地化**。
- **暗色模式**：theme.css 有完整 `.dark` token + `@custom-variant dark`，next-themes 已装；但页面全部 inline style 硬编码 hex（不走 CSS 变量），**暗色模式实际不可用**，要支持需把页面颜色改为 token 引用（工作量大，建议降级为"后续可选"）。

---

## 6. Top 迁移风险与缺失项

### Top 5 风险
1. **PDF 预览完全缺失**：无 react-pdf/pdfjs，正文是写死的假文本、页码硬编码 15、高亮是写死 mark。需把旧 `PdfViewer`（react-pdf 7）整体移植进新三栏，并接通 CitationChip 的"跳页"和划词标注持久化（`paper_annotations`）。
2. **SSE 流式对话无实现**：仅 setTimeout 假回复；Markdown 靠正则 + `dangerouslySetInnerHTML`（有 XSS 隐患）。需移植旧 `ChatPanel` 的手写 fetch ReadableStream SSE 解析 + react-markdown，并补会话 CRUD UI（新 UI 没有会话列表/历史管理）。
3. **零数据层 + 类型漂移**：需新建 api 层（可整体搬旧 `api.js` axios 封装 + `apiUrl.js`），并写 mock 类型 ↔ 后端 schema 的映射适配；dev 代理（/api、/static → :8000）也要在 vite.config.ts 里补。
4. **图表栈切换（ECharts→Recharts）**：旧 StatsPage 图表逻辑不能平移，要么用 Recharts 重写（跟随新 UI，推荐），要么把 ECharts 装回来；后端统计数据形状需对齐。
5. **样式与运行环境隐患**：(a) inline 硬编码色使 dark token 形同虚设；(b) Google Fonts 外链在 Electron 离线包失效；(c) SettingsModal 默认值是 Anthropic/Claude 而非 Kimi——接配置接口时必须纠正。

### 缺失功能清单（需从旧前端移植逻辑，新 UI 连壳都没有）
- 文献**上传/导入流程**（按钮全是 no-op，无进度、无处理状态轮询）
- **会话历史管理**（对话列表、删除、重新生成）与**图片分析**入口（/analyze-image 多模态）
- 文献**元数据编辑**、标签管理增删
- PDF **标注的读取/持久化**（浮动条只是 mock）
- 后端**健康状态**（/api/health 的 llm_ready 在 Sidebar 只写死 true）
- 无 react-window 之类的虚拟列表（文献量大时需补）

---

## 7. 建议迁移顺序

1. **骨架与地基**：App 壳（Sidebar/TopBar/视图状态切换）→ 移植 api.js/apiUrl.js → vite.config.ts 补 /api、/static 代理 → 字体本地化 → 裁剪无用依赖与 shadcn 死组件。
2. **LibraryPage**：接 papers 接口 + 字段映射 + 导入流程（风险小，先打通数据链路）。
3. **PaperDetailPage**（ hardest first 的核心页）：移植 PdfViewer(react-pdf) → 移植 ChatPanel(SSE+react-markdown) → NotesPanel 换 react-markdown 并接笔记接口。
4. **SearchPage**：三模式切换对接后端检索开关（语义/关键词/RRF 混合）。
5. **SettingsModal**：接 /api/settings，默认值改 Kimi，接 LLM 健康检查。
6. **ThesisPage**：合并旧 ThesisList/ThesisDetail 两页逻辑进单页。
7. **WritingDeskPage**：6 Skill 卡片接 /api/chat/skills，输出面板接流式。
8. **StatsPage**：Recharts 重写图表，接统计接口。
9. **ExportPage**：接导出/备份接口，备份时间线接备份记录。
10. **收尾**：暗色模式（可选）、虚拟列表、Electron 打包验证。
