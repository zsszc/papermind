# Phase D：Langfuse 自托管可观测性 规格说明书

> 来源：Phase 2 计划 Phase D 章节 + llm.md 现状契约。
> 目标：所有 LLM 调用可追踪（prompt/延迟/token），与 eval 互补（eval 是离线门禁，Langfuse 是运行时观测）。

## 1. 背景与目标

现状：LLM 调用全部收敛在 `services/llm.py` 单例（宪法第 8 条），是天然的可观测性挂点；但当前除 `logs/app.log` 文本日志外无结构化追踪——无法回答「这次对话的 prompt 是什么、耗时多少、烧了多少 token」。Langfuse（开源 LLM 观测平台）自托管部署 + SDK 接入可补齐，且不引入云端依赖（数据全在本地 docker 容器）。

## 2. 范围

### 2.1 包含

- D1：**Langfuse docker 服务**。`docker-compose.yml` 增加 langfuse-web + langfuse-worker + postgres + clickhouse（官方精简版）；密钥走 `.env`（`.env.example` 给模板）；`docs/DEPLOY.md` 增补章节
- D2：**后端接入**。`pip install langfuse`（**先验证与 httpx 0.27.2 / pydantic 2.7.4 兼容，`pip check` 零冲突后锁定版本**）；`services/llm.py` 的三个真实方法 `chat_stream` / `chat_completion` / `chat_completion_sync` 包裹观测；未配置 `PAPERMIND_LANGFUSE_*` 环境变量时**零侵入跳过**；trace metadata 关联 conversation_id / skill / 检索 chunk 数

### 2.2 非目标

- 不追踪 image_analyzer / web_search 的直连调用（宪法第 8 条例外区，Phase C 后另行收敛）
- 不做成本核算看板（Langfuse UI 自带 token 统计已够）
- 不把 Langfuse 变成硬依赖——它挂了 PaperMind 必须照常工作

## 3. 行为契约

### 3.1 D1：部署形态

- `docker-compose.yml` 新增独立 `langfuse` profile（`docker compose --profile langfuse up` 才拉起），默认 `docker compose up` 不启动
- 服务清单（Langfuse v3 实证修正）：langfuse-web + langfuse-worker + postgres + clickhouse + **redis + minio 共 6 个**（v3 架构必需，初稿写 4 个低估了）
- 数据卷本地化（postgres + clickhouse 各一 volume）；端口默认 3001（避开 3000/8000）
- `.env.example` 列出 `LANGFUSE_SECRET_KEY` / `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_HOST` 等占位符；真实值 `.env` 已 gitignore

### 3.2 D2：观测包裹

- **开关**：`PAPERMIND_LANGFUSE_PUBLIC_KEY` + `PAPERMIND_LANGFUSE_SECRET_KEY` 均未设置 → 三个方法行为与现状逐字节一致（零开销零副作用）
- **配置即启用**：设置后 `LLMService.__init__` 惰性初始化 Langfuse client；初始化失败 → 记 warning 并降级为未启用态
- **trace 内容**：方法名、model、消息条数、是否流式、首 token 延迟/总耗时、（非流式）usage tokens、异常类型；metadata 携带 conversation_id/skill/chunk_count（由调用方经可选参数传入，缺省不报错）
- **降级契约**：观测路径任何异常（网络/序列化/SDK bug）一律吞掉记 `[langfuse]` warning，**绝不影响 LLM 主链路返回值**——含 Langfuse 服务不可达
- **API 形态**：优先 `@observe` 装饰器或 `langfuse.openai` drop-in wrapper 中**对现有代码侵入更小者**（实现时二选一并在 plan.md 记录理由）；禁止改动三个方法的签名与返回结构

### 3.3 依赖决策（实现时第一道工序）

1. `pip install langfuse`（最新 2.x）→ `pip check`
2. 若与 httpx 0.27.2 / pydantic 2.7.4 / FastAPI 0.110 冲突 → 逐级降级 langfuse 版本直至零冲突；降级到 2.0 仍冲突则暂停 D2，规格记录阻塞原因（D1 仍交付）
3. 锁定结果写入 `requirements.txt` 与 `pyproject.toml`（宪法第 16 条：双文件同步）

## 4. 边界条件与错误处理

| 场景 | 期望行为 |
|------|----------|
| 仅配置一个 key | 视为未配置，零侵入 |
| Langfuse 容器未启动 | 初始化/上报失败降级，主链路无感 |
| 流式中途异常 | trace 标记 error 状态（若 SDK 支持），已产出 delta 不丢 |
| 消息含敏感内容 | trace 记录是设计行为（自托管本地容器，数据不出本机）；DEPLOY.md 明确提示 |
| pip check 冲突不可解 | D2 暂停，规格留阻塞记录，不强行升级既有锁定栈 |

## 5. 依赖

- specs/backend/services/llm.md（三方法契约、重试与温度怪癖）
- specs/constitution.md 第 8/16 条
- docker-compose.yml / Dockerfile / docs/DEPLOY.md 现状

## 6. 验收标准（可测试）

- [ ] AC1：未配置环境变量时 `test_llm.py` 全绿且无 Langfuse import 副作用
- [ ] AC2：mock langfuse client 下，三方法调用产生预期 trace 字段（测试断言 metadata/耗时/error 路径）
- [ ] AC3：langfuse client 初始化抛异常 → 降级 warning，三方法行为不变（新用例）
- [ ] AC4：`docker compose --profile langfuse config` 校验通过；DEPLOY.md 章节可复现启动
- [ ] AC5：`pip check` 零冲突；全套件全绿

## 7. 现有测试覆盖与盲区

- test_llm.py（Batch 7-F2 建立）覆盖错误文案分支；无观测路径任何测试
- 新测试文件：`tests/test_llm_observability.py`（新）+ test_llm.py 追加未配置零侵入用例

## 8. 关键设计决策

- **profile 隔离**：Langfuse 栈（4 容器）不进默认 compose 启动路径——它是开发辅助不是运行时依赖
- **挂点只在 llm.py**：宪法第 8 条的唯一入口红利——一处包裹全覆盖；image_analyzer/web_search 例外区不入本轮
- **先依赖兼容验证后写代码**：langfuse SDK 依赖链重（含 httpx/pydantic），与本项目锁定栈冲突风险高；D2 以 pip check 为第一门禁
