# services/backup.py（全量备份与每日自动备份）规格说明书

> 本文件描述 `backend/app/services/backup.py` 及其两个暴露面（`main.py` 定时调度、`routers/export.py` 手动端点）的**行为契约**（做什么），不描述实现细节。
> 依据源码实际内容反向工程整理（2026-08-04）。函数签名照抄代码。

## 1. 背景与目标

PaperMind 是本地优先应用（宪法第 1 条）：SQLite 数据库、PDF、笔记、向量库全部在用户本地磁盘，无云端副本。一旦磁盘损坏或误删即不可恢复。本模块提供两道防线：

1. **每日凌晨 3 点自动备份**：后台 daemon 线程把全量数据打成 zip 存入 `backups/`，只保留最近 10 份；
2. **手动备份**：用户可在「数据导出」页随时触发——或直接下载 zip 到本地（不落服务器盘），或让服务端立即做一次与自动备份同构的备份并清理旧份。

## 2. 范围

### 2.1 包含

- `services/backup.py` 三个函数：`create_backup` / `auto_backup` / `cleanup_old_backups`（及辅助 `get_project_root`）
- 备份内容清单与 zip 内路径布局
- `main.py::_schedule_daily_backup()` 的每日 3 点调度计算与失败容错
- 手动触发：`POST /api/export/backup`（流式下载）与 `POST /api/export/backup/auto`（服务端落盘 + 清理）
- 保留最近 10 份的清理策略

### 2.2 非目标

- **恢复 / 还原**：全项目无任何 restore 功能，备份 zip 只能靠用户手工解压回迁
- 增量 / 差异备份、备份加密、远端上传、备份完整性校验
- `vector_db` 之外的 ChromaDB 一致性处理（备份是文件级拷贝，不做数据库快照 quiesce）
- `routers/export.py` 中 CSV / Excel / 引用导出（归 export 路由规格）

## 3. 行为契约

### 3.1 `get_project_root() -> Path`

- **输出**：`Path(__file__).resolve().parents[3]`，即项目根目录（`backend/app/services/` 上溯三级）
- **注意**：与 `PAPERMIND_DATA_DIR` 重定向**无关**——Electron 生产包中数据目录被重定向到系统应用数据目录后，本函数仍指向安装包内路径，备份清单可能取不到真实数据（见第 8 节）

### 3.2 `create_backup(dirs: List[str] = None, include_db: bool = True, include_vector: bool = True) -> bytes`

- **输入**：
  - `dirs`：自定义目录清单；为 `None` 时用默认清单 `["data", "papers", "notes", "my-thesis", "skills", "logs"]`，且 `include_vector=True`（默认）时追加 `"vector_db"`。传了 `dirs` 则 `include_vector` 不再生效；
  - **`include_db` 是死参数**：函数体从不引用它——`data` 目录（含 SQLite 库）永远在默认清单内，**无法通过参数排除数据库**；
- **输出**：完整 zip 文件的 bytes（整个压缩包在内存中构建）
- **打包规则**：
  - 清单中不存在的目录**静默跳过**；
  - 递归收集各目录下全部文件（`rglob("*")` + `is_file()`），zip 内路径（arcname）为相对项目根的路径，如 `data/papers.db`；
  - **`config.yaml` 原样打入 zip 根**（若存在）。源码注释自称「去掉 API Key」，但**实际未做任何脱敏**——API Key 明文随包输出（以代码为准，安全含义见第 4、8 节）。
- **异常**：无任何 try/except——磁盘满、权限错误、文件读取中被占用等异常**原样抛出**给调用方
- **副作用**：读全量数据文件；峰值内存 ≈ 压缩后 zip 体积（`vector_db` 大时显著）

### 3.3 `auto_backup(backup_dir: Path = None) -> Path`

- **输入**：`backup_dir=None` 时默认 `<项目根>/backups`，不存在则 `mkdir(parents=True)` 创建
- **输出**：备份文件路径，命名 `papermind_auto_backup_%Y%m%d_%H%M%S.zip`
- **后置条件**：成功时写 `logger.info` 并返回路径；**同一秒内两次调用产生同名文件，后写覆盖先写**（`write_bytes`）
- **异常**：`create_backup` 或写盘失败 → `logger.error`（含堆栈）后**原样 re-raise**——容错责任上移到调用方（调度线程 / HTTP 层）
- **副作用**：磁盘写入一个 zip 文件

### 3.4 `cleanup_old_backups(backup_dir: Path = None, keep: int = 10)`

- **输入**：`backup_dir=None` 默认 `<项目根>/backups`；`keep` 默认保留 10 份
- **行为**：
  - 目录不存在 → 静默返回；
  - 仅匹配 `papermind_auto_backup_*.zip`（手动下载流 `papermind_backup_*` 不落盘、不受清理影响；`backups/` 内其他文件名同样不被动）；
  - 按文件 `st_mtime` **倒序**排序，保留前 `keep` 个，其余逐个 `unlink`；`keep=0` 时删除全部匹配文件；
  - 单个文件删除失败 → `logger.warning` 后继续删其余（不中断、不抛出）。
- **副作用**：可能删除若干旧备份文件

### 3.5 每日调度（`main.py::_schedule_daily_backup()`）

- **启动时机**：FastAPI lifespan 中启动一次；daemon 线程，名称 `daily-backup`，随进程退出而消亡（无 join）
- **调度计算**（每次循环重新计算，无持久化状态）：
  1. `next_run = 今日 03:00:00`；
  2. 若 `next_run <= now`（当前已过 3 点）→ `next_run += 1 天`，即**次日 3 点**；
  3. `time.sleep((next_run - now).total_seconds())` 睡到点；
  4. 醒后顺序执行 `auto_backup()` → `cleanup_old_backups(keep=10)`，然后回到步骤 1。
- **失败容错**：步骤 4 整体包在 try/except 内——任何异常仅记 `logger.warning("[backup] 定时备份失败: ...")`，**循环继续、线程不死**（下一次仍按次日 3 点重算）
- **已知语义**：
  - 进程停机跨过 3 点**不补跑**（无 missed-job 概念）；
  - 系统休眠时 `sleep` 计时顺延，醒后可能偏离 3 点整执行；
  - 进程每次重启重新计算下一次 3 点。

### 3.6 手动端点（`routers/export.py`）

#### `POST /api/export/backup`（流式下载，前端「全量备份导出」按钮）

- **不走 `create_backup`**，而是路由内联的 zip 逻辑，清单固定为 `["data", "papers", "notes", "my-thesis", "vector_db", "skills", "logs"]`（恒含 vector_db）；
- 与服务端备份的两处**契约差异**：**不含 `config.yaml`**（故不会泄露 API Key）、zip 不写盘；
- **输出**：`StreamingResponse`（8192 字节分块），`Content-Disposition` 文件名 `papermind_backup_%Y%m%d_%H%M%S.zip`；
- **异常**：打包中异常经全局异常处理 → 500 通用文案（不泄露原文，宪法第 13 条）。

#### `POST /api/export/backup/auto`（服务端立即备份）

- **行为**：`auto_backup()` → `cleanup_old_backups()`（默认 keep=10），等价于立即执行一次每日定时任务；
- **输出**：`{"status": "ok", "path": "<备份文件绝对路径>"}`；
- **异常**：`auto_backup` re-raise 的异常 → 全局 500 通用文案；**注意此时可能已生成半个 zip 或根本没生成，且 `cleanup_old_backups` 不会执行**。

## 4. 边界条件与错误处理

| 场景 | 期望行为 |
|------|----------|
| 清单中某目录不存在 | 静默跳过，不影响其余目录 |
| `config.yaml` 不存在 | 不打包，不报错 |
| `backups/` 不存在（清理时） | `cleanup_old_backups` 静默返回 |
| 备份目录权限不足 / 磁盘满 | `create_backup` 抛出 → `auto_backup` 记 error 后 re-raise → 调度线程记 warning 继续；HTTP 手动触发返回 500 |
| 同一秒内两次 `auto_backup` | 同名文件，后者覆盖前者 |
| 删除某份旧备份失败（如被占用） | 记 warning，继续删其余，不中断 |
| `backups/` 内混入非 `papermind_auto_backup_*` 文件 | 不参与计数与清理，原样保留 |
| 旧备份 mtime 相同 | 并列时顺序按 glob 返回序（稳定排序），极端下保留集合不确定——实际秒级时间戳下罕见 |
| 进程停机跨过 3 点 | 不补跑；重启后重算下一次 3 点 |
| 系统休眠后唤醒 | sleep 顺延，备份在唤醒时刻执行而非 3 点整 |
| `vector_db` 体积大 | 整个 zip 在内存构建（`create_backup` 路径），存在内存峰值风险；流式下载端点同样先在内存构建完整 zip 再分块 yield |
| `summaries/` 目录（AI 概括输出） | **不在任何备份清单内**——该目录数据不会被备份 |
| API Key 泄露面 | 服务端备份 zip（`create_backup` / `auto_backup` / `/api/export/backup/auto`）**含明文 `config.yaml`**；手动下载流（`/api/export/backup`）不含 |

## 5. 依赖

- **上游依赖**：仅标准库 `io` / `zipfile` / `datetime` / `pathlib` / `typing`（`shutil`、`app.core.config.config` 为死导入，函数体未引用）；`app.core.logger`
- **下游消费者**：
  - `app/main.py` lifespan：`_schedule_daily_backup()` 每日 3 点调度线程
  - `routers/export.py`：`POST /api/export/backup`（内联逻辑，仅概念同构）、`POST /api/export/backup/auto`（直接调用）
  - 前端 `DataExport.jsx`（`exportBackup` 下载 / `triggerAutoBackup`）
  - 测试基建反向约定：`tests/conftest.py` 的 TestClient 不触发 lifespan，**备份线程在测试中永不启动**

## 6. 验收标准（可测试）

- [ ] AC1：默认调用 `create_backup()` 产出的 zip 含 `data/papers/notes/my-thesis/skills/logs/vector_db` 下全部文件及 `config.yaml`，不存在目录被跳过，arcname 为相对项目根路径
- [ ] AC2：`include_vector=False` 时 zip 不含 `vector_db`；传自定义 `dirs` 时按自定义清单打包
- [ ] AC3：`auto_backup` 在指定目录生成 `papermind_auto_backup_时间戳.zip` 并返回其路径；失败时异常上抛且记 error 日志
- [ ] AC4：`cleanup_old_backups` 按 mtime 保留最新 10 份、删除更早者；单个删除失败不影响其余；目录不存在时静默返回
- [ ] AC5：调度计算——3 点前下次为当日 3 点，3 点后（含恰 3 点）下次为次日 3 点
- [ ] AC6：定时任务中 `auto_backup` 抛异常时线程不退出，仅记 warning，下一循环仍正常调度
- [ ] AC7：`POST /api/export/backup` 返回 zip 流且**不含** `config.yaml`；文件名形如 `papermind_backup_时间戳.zip`
- [ ] AC8：`POST /api/export/backup/auto` 返回 `{"status":"ok","path":...}`，`backups/` 新增一份且总数被清理到 ≤10

## 7. 现有测试覆盖与盲区

- **已覆盖**：**零**——`backend/tests/` 15 个测试文件中无任何对本模块、两个备份端点或调度函数的引用；conftest 明确不触发 lifespan，调度线程在测试环境从不启动
- **盲区**：
  - `config.yaml` 明文入包与注释声称的「去掉 API Key」不符——无测试也无安全断言固化现状（**高**，密钥随备份文件扩散的风险）
  - 调度时间计算（3 点前 / 后 / 跨天、恰 3 点边界）无测试（**高**，核心契约）
  - `cleanup_old_backups` 的保留 10 份、mtime 排序、单文件失败容错、`keep=0` 语义均无测试（**高**）
  - 备份内容清单（默认目录集、`include_vector` 开关、不存在目录跳过）无测试（**高**）
  - `auto_backup` 失败 re-raise 与调度线程「记 warning 不死」的容错链无测试（**中**）
  - 两个手动端点的契约差异（含/不含 config.yaml、流式 vs 落盘）无测试（**中**）
  - `include_db` 死参数（传入 False 也照样备份数据库）无测试（**中**，误导性 API）
  - `summaries/` 未纳入任何备份清单——无测试、无文档警示（**中**，AI 概括数据存在丢失面）
  - 同秒文件名冲突覆盖、大 zip 内存峰值、`PAPERMIND_DATA_DIR` 下项目根定位失真，均无测试（**低**）

## 8. 关键设计决策

- **全量 zip + 文件级拷贝**：不引入快照/增量机制，实现最简（宪法第 4 条单进程简单架构）；代价是备份期间库文件被写入可能拷到不一致页，单用户场景下凌晨 3 点几乎无写入，视为可接受
- **zip 全量内存构建**：`create_backup` 直接返回 bytes，省去临时文件；数据量随 `vector_db` 增长后存在内存峰值风险，未来应改流式写盘
- **文件名前缀区分 auto / 手动**：清理只认 `papermind_auto_backup_*`，手动下载流本就落客户端、不参与保留策略
- **daemon 线程 + sleep 重算**：不引入 APScheduler 等调度框架；每次循环重算下次 3 点，天然免疫「睡过头后时间漂移累积」，但无 missed-job 补跑
- **容错分层**：`create_backup` 不兜底（纯函数式抛错）→ `auto_backup` 记日志后 re-raise（保留调用方可观测性）→ 调度线程 try/except 兜底（保线程存活）；HTTP 层则交给全局异常处理脱敏
- **注释与行为不符处以代码为准**（宪法第 20 条）：`include_db` 死参数、`config.yaml` 未脱敏、`shutil`/`config` 死导入均按现状记录；修复时应先补测试再改行为，并同步修订本规格
- **`get_project_root` 不经 `config.data_dir`**：与宪法第 3 条可移植性在 Electron 生产包下存在张力（`PAPERMIND_DATA_DIR` 重定向后备份源路径可能失真），属已知遗留，改造时需连同 `create_backup` 的目录解析一并处理
