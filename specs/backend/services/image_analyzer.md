# services/image_analyzer.py（图片分析服务 ImageAnalyzerService）规格说明书

> 本文件描述**行为契约**（做什么），不描述实现细节。依据 `backend/app/services/image_analyzer.py` 与 `backend/app/routers/chat.py` 实际代码反向整理（2026-08-25）。

## 1. 背景与目标

为用户提供「上传论文截图/表格/公式/图表 → 多模态大模型解读」的能力。前端在对话面板中上传图片并附带问题，后端把图片编码为 base64 data URL 后调用 Kimi 多模态接口，以 SSE 流式把分析结果回传给前端。

该服务是独立的多模态调用通道，与 RAG 检索、会话记忆完全解耦：分析结果**不写入会话、不入库**。

## 2. 范围

### 2.1 包含

- 图片字节到 base64 data URL 的编码与 MIME 推断行为
- 非流式（`analyze`）与流式（`analyze_stream`）两种多模态调用的输入/输出/异常契约
- 模型温度选择规则（kimi-k2 系列特判）
- 与 `POST /api/chat/analyze-image` 路由的交互契约（含 SSE 事件格式）
- 模块级单例 `image_analyzer_service`

### 2.2 非目标

- 不做图片内容审核、格式内容嗅探（仅按扩展名猜 MIME）
- 图片大小限制：超过 10MB → HTTP 413（Batch7-F4 新增，04d542b）；仍不做扩展名白名单（MIME 由 `_guess_mime` 按后缀推断，见第 8 节）
- 不负责把分析结果持久化到会话/消息表（路由层也未做）
- 不承担重试、消息截断、错误格式化等统一 LLM 治理（该服务绕过 `services/llm.py`，见第 8 节）

## 3. 行为契约

### 3.0 模块级副作用

- 模块导入时即创建全局单例 `image_analyzer_service = ImageAnalyzerService()`，构造时同步读取 `config` 并实例化 `AsyncOpenAI` 客户端；**导入期不发网络请求**。
- 本服务直接实例化 `openai.AsyncOpenAI`，**不经过** `services/llm.py` 的 `llm_service`。

### 3.1 `ImageAnalyzerService.__init__(self)`

- **输入**：无（从全局 `config` 读取）。
- **输出**：无返回值，初始化实例属性：
  - `self.client`：`AsyncOpenAI(api_key=config.get("llm.api_key"), base_url=config.get("llm.base_url", "https://api.moonshot.cn/v1"), max_retries=1, timeout=120)`
  - `self.model`：`config.get("llm.model", "moonshot-v1-8k")`
- **前置条件**：`app.core.config.config` 已可读取（`llm.api_key` 缺失时客户端仍会被构造，失败推迟到调用时）。
- **后置条件**：实例持有一个 OpenAI 兼容异步客户端，SDK 层最多重试 1 次，默认超时 120 秒。
- **副作用**：读取全局配置单例；创建 HTTP 客户端对象。
- **异常**：构造本身不主动抛错；配置缺失不在此拦截。

### 3.2 `ImageAnalyzerService._get_temperature(self) -> float`

- **输入**：无。
- **输出**：`float`。`self.model` 含 `"kimi-k2.6"` 或 `"kimi-k2"` 时返回 `1.0`；否则返回 `0.3`。
- **前置条件**：`self.model` 已初始化。
- **后置条件**：返回值只可能是 `1.0` 或 `0.3`。
- **副作用**：无。
- **异常**：无。

### 3.3 `ImageAnalyzerService._encode_image(self, image_bytes: bytes) -> str`

- **输入**：`image_bytes: bytes` —— 图片原始字节，可为空字节串（函数本身不校验）。
- **输出**：`str` —— base64 编码后的 ASCII 字符串（`base64.b64encode(...).decode("utf-8")`）。**不添加** `data:` 前缀。
- **前置条件**：无。
- **后置条件**：输出长度约为输入长度的 4/3；空输入返回空字符串。
- **副作用**：无（内存放大约 1.33 倍，大图片会显著占用内存）。
- **异常**：仅当传入非 bytes 类型时抛 `TypeError`。

### 3.4 `ImageAnalyzerService._guess_mime(self, filename: str) -> str`

- **输入**：`filename: str` —— 原始文件名，允许为空串、无扩展名、任意大小写。
- **输出**：`str` —— MIME 类型，映射规则（先 `lower()` 再匹配后缀）：
  | 后缀 | 返回 |
  |------|------|
  | `.png` | `image/png` |
  | `.jpg` / `.jpeg` | `image/jpeg` |
  | `.gif` | `image/gif` |
  | `.webp` | `image/webp` |
  | 其他/无扩展名 | `image/jpeg`（兜底） |
- **前置条件**：无。
- **后置条件**：返回值必为上述四种之一；**不做文件内容嗅探**，扩展名与实际内容不一致时按扩展名声明。
- **副作用**：无。
- **异常**：无（`filename` 为 `None` 会抛 `AttributeError`，调用方需保证为 str）。

### 3.5 `ImageAnalyzerService.analyze(self, image_bytes: bytes, filename: str, question: str) -> str`

- **输入**：
  - `image_bytes: bytes` —— 图片字节（服务内不做大小/格式校验）
  - `filename: str` —— 用于 MIME 推断
  - `question: str` —— 用户提问；**空串/None 时替换为默认提示** `请描述这张图片的内容，并解释其在学术论文中可能的含义。`
- **输出**：`str` —— 模型的完整分析文本；`response.choices[0].message.content` 为 `None` 时返回 `""`。
- **前置条件**：`llm.api_key` 有效、网络可达 Kimi 接口、所用模型支持多模态图片输入。
- **后置条件**：成功时返回非空分析文本；失败时**不抛异常**，返回通用文案 `[图片分析失败，请稍后再试]`（Batch7-F4 起不再透传异常原文，原文仅入日志）。
- **副作用**：
  - 网络调用：`client.chat.completions.create(model=self.model, messages=..., max_tokens=2048, temperature=self._get_temperature(), timeout=120)`（非流式）。
  - 消息结构：system 固定为 `你是一位学术研究助手，擅长分析论文截图、表格、公式和图表。请用中文简洁回答。`；user 消息为两段式 content（`image_url` 为 `data:{mime};base64,{b64}`，`text` 为 question 或默认提示）。
  - 失败日志只记录异常类型，不记录异常原文或堆栈。
- **异常**：**不向外抛**。一切异常被捕获并转为固定返回值 `[图片分析失败，请稍后再试]`。

### 3.6 `ImageAnalyzerService.analyze_stream(self, image_bytes: bytes, filename: str, question: str) -> AsyncIterator[str]`

- **输入**：同 3.5。
- **输出**：`AsyncIterator[str]` —— 逐个 yield 模型的增量文本（`chunk.choices[0].delta.content`，空增量被跳过）。
- **前置条件**：同 3.5。
- **后置条件**：成功路径只 yield 模型增量文本；异常时抛 `ImageAnalysisError`，错误正文不进入文本流。
- **副作用**：
  - 网络调用：同 3.5，但 `stream=True`。
  - 失败日志只记录异常类型。
- **异常**：抛 `ImageAnalysisError`，由路由转为脱敏 SSE error 终态。

### 3.7 路由交互契约：`POST /api/chat/analyze-image`（`routers/chat.py`）

- **请求**：`multipart/form-data`
  - `file`：上传文件（必填）；`question`：表单字段，默认 `请描述这张图片的内容，并解释其在学术论文中可能的含义。`
- **路由层校验**：
  - `image_bytes = await file.read()` 为空 → `HTTPException 400, detail="图片内容为空"`。
  - `file.filename` 为空时回退 `"image.jpg"`（即 MIME 按 jpeg 处理）。
  - 超过 10MB → 413；仍无扩展名白名单。
- **响应**：`StreamingResponse, media_type="text/event-stream"`，请求头 `Cache-Control: no-cache`、`Connection: keep-alive`、`X-Accel-Buffering: no`。
- **SSE 事件序列**：
  1. 若干 `data: {"delta": <增量文本>, "finished": false}\n\n`
  2. 成功以 `data: {"delta":"","finished":true,"content":"<全文>"}\n\n` 结束。
  3. 服务异常或空结果以固定 `{error,error_code}` 结束；不得再发送 finished，HTTP 状态码仍是 200。
- **无会话副作用**：不写 `conversations`/`messages` 表，无 citations 字段。

## 4. 边界条件与错误处理

| 场景 | 期望行为 |
|------|----------|
| 上传空文件（0 字节） | 路由层拒绝：HTTP 400 `图片内容为空` |
| `question` 为空串/缺省 | 使用默认提示「请描述这张图片的内容，并解释其在学术论文中可能的含义。」 |
| 文件名无扩展名/未知扩展名 | `_guess_mime` 兜底为 `image/jpeg` |
| 文件名大小写混合（如 `A.PNG`） | 先 `lower()` 再匹配，正确识别 |
| 扩展名与真实内容不符（如 .png 实为 jpeg） | 按扩展名声明 MIME，不做内容嗅探；识别后果由模型侧承担 |
| 超大图片 | 路由在读取后以 10MB 上限拒绝，返回 413 |
| Kimi 接口超时/报错 | `analyze` 返回固定失败文案；`analyze_stream` 抛类型化异常，路由发脱敏 error 且无 finished |
| 模型返回空 content（非流式） | 返回空字符串 `""` |
| 流式中出现空增量 | 跳过不 yield |
| 配置缺 `llm.api_key` | 构造不拦截，调用时以失败文案形式返回 |

## 5. 依赖

- **上游依赖**：
  - `app.core.config.config`（`llm.api_key` / `llm.base_url` / `llm.model`）
  - `app.core.logger.logger`
  - `openai.AsyncOpenAI`（锁定 `openai==1.12.0` + `httpx==0.27.2`，见宪法第 16 条）
  - Kimi 多模态接口（`base_url` 默认 `https://api.moonshot.cn/v1`）
- **下游消费者**：
  - `app/routers/chat.py` 的 `POST /api/chat/analyze-image`（使用 `analyze_stream`）
  - `analyze`（非流式）当前**无调用方**

## 6. 验收标准（可测试）

- [ ] AC1：空文件上传 `/api/chat/analyze-image` 返回 400 且 detail 为「图片内容为空」
- [ ] AC2：`_guess_mime` 对 `.png/.jpg/.jpeg/.gif/.webp`（含大写）返回对应 MIME，其他扩展名返回 `image/jpeg`
- [ ] AC3：`question` 为空时，发给模型的 user 文本为默认提示（mock client 断言 messages 内容）
- [ ] AC4：model 含 `kimi-k2` 时调用参数 `temperature=1.0`，否则 `0.3`；`max_tokens=2048`
- [x] AC5：流式成功路径按增量 yield，SSE 事件以 `{delta, finished:false}` 序列开始、以 `{delta:"", finished:true, content}` 结束
- [x] AC6：client 抛异常时 `analyze` 返回固定失败文案；`analyze_stream` 抛类型化异常，路由发脱敏 error 且无 finished；日志不含原异常
- [ ] AC7：模型返回 `content=None` 时 `analyze` 返回 `""`
- [ ] AC8：分析结果不产生任何 DB 写入（`messages`/`conversations` 表不变）

## 7. 现有测试覆盖与盲区

- **已覆盖**：`tests/test_chat_image.py` 覆盖空/10MB/超限上传、非流式脱敏、流式类型化失败和路由 error 终态。
- **盲区**：
  - 【中】`_guess_mime` 扩展名映射与 jpeg 兜底（AC2）、`_get_temperature` 的 kimi-k2 特判（AC4）未测试
  - 【低】非流式 `analyze` 无调用方亦无测试，属事实上的备用 API

## 8. 关键设计决策

- **绕过 `llm.py` 直连 `AsyncOpenAI`**：宪法第 8 条要求所有 LLM 调用经 `services/llm.py` 统一入口，本服务是现存的两个例外之一（另一个是 `web_search.py`）。后果：不享受统一重试（3 次指数退避）、消息截断与错误格式化，仅依赖 SDK 层 `max_retries=1`。后续治理（纳入 llm.py 或在宪法登记例外）需产品层面决策，本规格只记录现状。
- **流式异常类型化**：服务层不再把失败伪装成正文，路由负责固定 error 终态；前端可统一丢弃 provisional。
- **10MB 上限、格式仍按扩展名**：限制费用与内存放大，但暂不做图片内容嗅探。
- **结果不入库**：图片分析定位为一次性问答，不进入会话历史与记忆体系，避免大段 base64 或长分析文本污染对话上下文。
- **默认提示中文化**：system prompt 与默认 question 均固定中文，贴合单用户中文学术写作场景。
