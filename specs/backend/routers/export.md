# routers/export.py（数据导出：CSV / Excel / 引用 / 备份）规格说明书

> 本文件描述 `backend/app/routers/export.py` 的**行为契约**（做什么），不描述实现细节。
> 依据源码实际内容反向工程整理（2026-08-04）。端点签名照抄代码。
> 备份的服务层契约（`auto_backup` / `cleanup_old_backups` / 每日调度）详见 `specs/backend/services/backup.md`；本规格聚焦四个导出端点的 HTTP 行为。

## 1. 背景与目标

PaperMind 是本地优先的单用户应用，用户需要把数据带出去做他用：文献清单移交 Excel 管理、写论文时贴参考文献列表、整机迁移前打全量包。本路由提供三类元数据导出（CSV / Excel / 格式化引用，均为**流式下载、不落服务器盘**）和两类备份触发（流式下载 zip / 服务端落盘 zip），对应前端「数据导出」页的全部功能。

## 2. 范围

### 2.1 包含

- `GET /api/export/papers/csv`：文献元数据 CSV 下载
- `GET /api/export/papers/excel`：文献元数据 Excel 下载
- `GET /api/export/papers/bib`：格式化引用列表 TXT 下载（GB/T 7714 / APA / MLA）
- `POST /api/export/backup`：全量数据 zip 流式下载（不落盘、不含 `config.yaml`）
- `POST /api/export/backup/auto`：服务端立即备份落盘 + 清理旧份
- 导出列定义（`EXPORT_COLUMNS`）与引用格式化规则（`_format_citation`）

### 2.2 非目标

- 恢复 / 还原备份、增量备份、备份加密（全项目无此能力，见 backup 服务规格第 2.2 节）
- 每日凌晨 3 点定时调度（归 `main.py` 与 backup 服务规格）
- 导出内容的筛选 / 子集导出：所有端点恒导出**全表**，无查询过滤参数
- 笔记 / 概括的单独导出（只能经全量备份 zip 间接获得）

## 3. 行为契约

路由注册：`app.include_router(export.router, prefix="/api/export", tags=["export"])`（`main.py`）。

### 3.0 模块常量与辅助函数

- `EXPORT_COLUMNS`：10 列 `(字段名, 中文表头)`——`id/ID`、`title/标题`、`authors/作者`、`year/年份`、`journal/期刊/会议`、`doi/DOI`、`status/阅读状态`、`tags/标签`、`filename/文件名`、`created_at/导入时间`。CSV 与 Excel 共用同一份列定义与行数据（`_paper_to_row`）。
- `_paper_to_row(paper: Paper) -> dict`：空值一律转 `""`；`tags` 为 `", "` 连接的标签名（无标签为 `""`）；`created_at` 格式化为 `%Y-%m-%d %H:%M:%S`（为 `None` 时为 `""`）。
- `_format_citation(paper: Paper, fmt: str) -> str`：
  - 缺省占位：`authors` 空 → `"匿名"`，`title` 空 → `"未命名"`，`year` 空 → `"n.d."`，`journal` 空 → `""`；
  - `APA`：`{authors} ({year}). {title}. {journal}`；
  - `MLA`：`{authors}. "{title}." {journal}, {year}.`；
  - 其余（含默认 GB/T 7714）：`{authors}. {title}[J]. {journal}, {year}.`；
  - `fmt` 精确匹配 `"APA"` / `"MLA"`，**大小写敏感**（路由层已做归一化，见 3.3）。

### 3.1 `GET /papers/csv` → `export_papers_csv(db: Session = Depends(get_db))`

- **输入**：无参数
- **输出**：`StreamingResponse`，`media_type="text/csv; charset=utf-8"`，`Content-Disposition: attachment; filename="papermind_papers_%Y%m%d_%H%M%S.csv"`
- **行为**：全表按 `created_at` 降序导出；首行为中文表头；内容编码 **utf-8-sig**（带 BOM，兼容 Excel 直接打开）
- **副作用**：DB 只读；整个 CSV 在内存构建后一次性放入响应
- **异常**：无显式处理；DB 异常 → 全局 500 通用文案

### 3.2 `GET /papers/excel` → `export_papers_excel(db: Session = Depends(get_db))`

- **输入**：无参数
- **输出**：`StreamingResponse`，`media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"`，文件名 `papermind_papers_%Y%m%d_%H%M%S.xlsx`
- **行为**：工作表名固定 `"文献列表"`；表头与行数据同 CSV；A–J 列宽硬编码（8/50/30/10/30/25/12/20/25/20）
- **异常**：`openpyxl` 缺失（函数内 `import`，`ImportError` 时）→ `HTTPException(500, detail="缺少 openpyxl，无法导出 Excel")`——detail 原文返给客户端
- **副作用**：DB 只读；整个工作簿在内存构建

### 3.3 `GET /papers/bib` → `export_papers_bib(format: str = "GB/T 7714", db: Session = Depends(get_db))`

- **输入**：查询参数 `format`（默认 `"GB/T 7714"`）
- **格式归一化**（按代码顺序）：
  1. `fmt = format.upper()`——`"apa"` / `"mla"` 等小写输入可用；
  2. `fmt` 不在 `{"GB/T 7714", "APA", "MLA"}` → 回退 `config.get("export.citation_format", "GB/T 7714")`（**注意：配置值不再二次校验、不做 upper 归一**，配置写成小写 `"apa"` 时 `_format_citation` 精确匹配失败，实际按 GB/T 7714 输出）；
  3. 别名归并：`"GB" in fmt or "7714" in fmt` → 统一为 `"GB/T 7714"`。
- **输出**：`StreamingResponse`，`media_type="text/plain; charset=utf-8"`，UTF-8 编码（**无 BOM**），文件名 `papermind_citations_{fmt 的 '/' 替换为 '_'}_%Y%m%d_%H%M%S.txt`（如 `papermind_citations_GB_T 7714_....txt`）
- **行为**：全表按 `created_at` 降序，逐条编号 `[1] [2] ...`，每行一条 `_format_citation` 结果，`\n` 连接；空库时导出空文件
- **副作用**：DB 只读
- **异常**：无显式处理；DB 异常 → 全局 500

### 3.4 `POST /backup` → `export_backup()`

- **输入**：无参数（前端 `api.post('/export/backup', {}, { responseType: 'blob', timeout: 120000 })`）
- **输出**：`StreamingResponse`，`media_type="application/zip"`，文件名 `papermind_backup_%Y%m%d_%H%M%S.zip`，8192 字节分块下发
- **打包规则**（路由内联逻辑，**不走 `services/backup.create_backup`**）：
  - 目录清单固定 `["data", "papers", "notes", "my-thesis", "vector_db", "skills", "logs"]`（恒含 `vector_db`；**不含 `summaries/`**）；
  - 不存在的目录静默跳过；递归收集全部文件，zip 内路径为相对项目根路径（如 `data/papers.db`）；
  - **不含 `config.yaml`**——与服务端落盘备份的关键差异：本端点不会泄露 API Key（宪法第 14 条）；
  - zip **不落地服务器磁盘**，但先在内存完整构建再分块 yield（大 `vector_db` 时有内存峰值）。
- **副作用**：读全量数据文件；内存峰值 ≈ 压缩后 zip 体积
- **异常**：打包中任何异常（磁盘读错、文件被占用等）→ 全局 500 通用文案

### 3.5 `POST /backup/auto` → `trigger_auto_backup()`

- **输入**：无参数
- **输出**：`{"status": "ok", "path": "<备份文件绝对路径>"}`——path 为服务器本地路径，供用户到 `backups/` 目录取件
- **行为**：顺序调用 `auto_backup()` → `cleanup_old_backups()`（默认 keep=10），等价立即执行一次每日定时任务；产物为 `backups/papermind_auto_backup_%Y%m%d_%H%M%S.zip`
- **与服务端备份契约的关系**：`auto_backup` 走 `create_backup` 默认参数，目录清单与本端点 3.4 相同（七个目录，含 `vector_db`），但额外把 **`config.yaml` 明文（含 API Key）打入 zip 根**（backup 服务规格 3.2）——两个备份端点内容物不同，调用方不得视为等价
- **异常**：`auto_backup` 失败 re-raise → 全局 500；此时 `cleanup_old_backups` 不执行，可能残留半个 zip（backup 服务规格 3.6）
- **副作用**：磁盘写入一个 zip；可能删除若干旧备份

## 4. 边界条件与错误处理

| 场景 | 期望行为 |
|------|----------|
| 文献库为空 | CSV / Excel 仅表头；bib 为空文件；均正常 200 下载 |
| 文献字段为空（无作者/年份等） | 行内填空串 `""`；bib 用占位值（`匿名` / `未命名` / `n.d.`） |
| 文献无标签 | `tags` 列为 `""` |
| `format=bogus`（无法识别的引用格式） | 回退 `config.yaml` 的 `export.citation_format`（默认 `GB/T 7714`），不报错 |
| `format=apa`（小写） | upper 归一后按 APA 输出 |
| 配置 `export.citation_format` 写成小写 | `_format_citation` 匹配失败，实际输出 GB/T 7714 样式，但**文件名仍带配置原值** |
| `openpyxl` 未安装 | Excel 端点 500，detail 为「缺少 openpyxl，无法导出 Excel」 |
| 备份清单中目录不存在 | 静默跳过，不影响其余目录 |
| `vector_db` 体积大 | 两个备份端点都先把完整 zip 装进内存，存在内存峰值风险 |
| 同一秒内两次备份 | 文件名相同：下载流各自独立无冲突；`/backup/auto` 落盘时后写覆盖先写 |
| `summaries/` 目录 | 不在任何备份清单内，AI 概括数据不会被备份 |
| 备份期间数据文件正被写入 | 文件级拷贝，无快照一致性（单用户本地场景视为可接受） |

## 5. 依赖

- **上游依赖**：`app.database.get_db`；`app.models.Paper`（含 `tags` 关系）；`app.core.config.config`（`export.citation_format`）；`app.services.backup.auto_backup / cleanup_old_backups`；标准库 `csv / io / zipfile / datetime / pathlib`；`openpyxl`（函数内延迟导入）
- **下游消费者**：前端 `api.js` 的 `exportPapersCSV` / `exportPapersExcel` / `exportPapersBib` / `exportBackup` / `triggerAutoBackup`（`DataExport.jsx` 页面）

## 6. 验收标准（可测试）

- [ ] AC1：CSV 导出为 utf-8-sig、首行中文表头、10 列齐、按 `created_at` 降序、文件名带时间戳
- [ ] AC2：Excel 导出为合法 xlsx、工作表名「文献列表」、内容与 CSV 同源
- [ ] AC3：`format=APA/MLA/GB/T 7714`（含小写）分别产出对应样式；无法识别的 format 回退配置默认；每条引用带 `[序号]` 前缀
- [ ] AC4：`POST /api/export/backup` 返回 zip 流，内含 `data/papers/notes/my-thesis/vector_db/skills/logs` 下文件且**不含 `config.yaml`**，不存在目录被跳过
- [ ] AC5：`POST /api/export/backup/auto` 返回 `{"status":"ok","path":...}`，`backups/` 新增 `papermind_auto_backup_*.zip` 且总数 ≤10
- [ ] AC6：空库时三个元数据导出端点仍正常 200（表头/空文件）

## 7. 现有测试覆盖与盲区

- **已覆盖**：**零**——grep 确认 `backend/tests/` 全部 15 个测试文件中无任何对 `/api/export/*`、`EXPORT_COLUMNS`、`_format_citation`、两个备份端点的引用（backup 服务规格第 7 节同样确认服务层零覆盖）
- **盲区**：
  - 三个元数据导出端点全部行为（AC1–AC3、AC6）无测试：列顺序、BOM、表头、格式化、文件名模式均无固化（**中**，回归改列定义时无告警）
  - 两个备份端点无测试，且两者内容物差异（含/不含 `config.yaml`、含/不含 `vector_db`、清单差异）无任何断言固化（**高**，`/backup/auto` 的 API Key 明文入包风险与密钥纪律相关，见 backup 服务规格盲区）
  - 引用格式归一化链（upper → 集合校验 → 配置回退 → GB 别名）及其「配置小写值静默变 GB/T」的坑无测试（**中**）
  - zip 内存峰值、同秒文件名覆盖、备份期间文件写入一致性均无测试（**低**）

## 8. 关键设计决策

- **元数据导出内存构建 + StreamingResponse 外壳**：数据量小（文献清单级别），实现最简；三个端点统一 `Content-Disposition: attachment` 强制下载而非浏览器内预览
- **CSV 用 utf-8-sig**：带 BOM 让 Windows Excel 双击打开不乱码，面向非技术用户的取舍（bib 的 TXT 反而无 BOM，属不一致的现状记录）
- **备份双端点分工**：`/backup` 流式下载落客户端、刻意不含 `config.yaml`（密钥不随下载包扩散）；`/backup/auto` 落服务器盘、内容与每日定时备份同构（含明文 `config.yaml`，文件名前缀 `papermind_auto_backup_` 才受 10 份清理约束）——两者刻意不同构，不得合并（宪法第 14 条）
- **备份内联重复实现而非复用 `create_backup`**：路由层为剔除 `config.yaml` 与固定含 `vector_db` 而自行内联；代价是两套打包逻辑独立演进（目录清单已出现 `summaries/` 都不含的共性遗漏），改造时应抽公共函数并以参数控制 config 排除
- **`openpyxl` 函数内延迟导入**：避免未安装时拖垮整个路由模块加载；500 detail 直给中文原因，属有意的可操作错误信息
