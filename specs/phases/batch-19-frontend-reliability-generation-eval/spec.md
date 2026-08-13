# Batch 19 规格：前端可靠性与受控生成评测

## 1. 背景与问题

Batch 18 恢复了真实向量检索可信度，充值后的 Kimi `kimi-k2.6` 健康检查也已恢复。但只读 Harness 审计发现：

- ChatPanel 会自动重放已可能落库的非幂等请求，快速重复发送也没有同步单飞；旧 SSE 还能在会话切换后污染新会话。
- 图片分析与重新生成没有传递 `AbortSignal`，组件卸载不取消后台流；SSE 未收到完成帧便 EOF 仍被当作成功。
- WritingDesk 将完整正文放进 URL，首屏空状态会覆盖 localStorage 草稿，切论文/清空后旧响应可以回写。
- PaperDetail 的 1 秒自动保存会在卸载时直接取消，手动与自动保存并发时旧响应可能覆盖新状态；后端笔记写入没有论文存在性、长度和原子替换门禁。
- `eval --with-llm` 没有样本白名单、调用预算、健康预检和生成有效性 Gate；当前 hybrid 评测链路也不等价于生产聊天的纯语义 top5。

## 2. 行为规格

### S1：Chat 操作单飞与会话隔离

- 发送入口必须用同步 ref 实现 single-flight；同一渲染周期的双击/回车只允许一个建会话和一个 POST。
- 不得自动重放已经发出的 `/api/chat` 非幂等请求；连接不完整时提示并通过重新加载历史对账。
- 每个流固定 `operation_id`、目标会话和目标消息标识；delta、citation、error 只可提交到仍匹配的目标，不得按“当前最后一条消息”写入。
- 新建、切换、删除会话及组件真正卸载时必须取消当前流；取消只收尾一次，不显示普通失败提示。
- 图片分析和重新生成必须接收同一个 `AbortSignal`；重生成按 message id 更新，不使用可漂移数组下标。
- 编辑消息的服务端删除失败时，不得不可逆地截断本地历史。

### S2：SSE 终态与长请求预算

- 成功必须收到 `{finished:true}`；body 缺失、提前 EOF、非法终帧均抛出明确协议错误，不调用成功回调。
- `{error}` 与 `{finished}` 是互斥终态，回调最多一次；结束后取消或释放 reader。
- 流请求采用连接/空闲/绝对时长预算；每个合法事件续租空闲计时，用户取消与超时使用不同错误状态，所有 timer 在终态清理。
- 默认预算适配 Kimi 长响应：首事件 60 秒、空闲 180 秒、绝对上限 10 分钟；联网检索可显式放宽但不得无限等待。

### S3：WritingDesk 正文与草稿安全

- `POST /api/thesis/{id}/suggest-citations` 使用 JSON body，URL、响应和访问日志均不包含 paragraph；正文 trim 后必须非空且不超过 20,000 字符。
- 前端只通过 `api.js` 单一路径发送；中文、换行及 `&?#` 必须原样保留在 JSON body。
- localStorage 草稿在任何持久化 effect 前同步读取；按 thesis id 隔离，300–500ms debounce，存储异常不得使页面崩溃。
- 切论文、清空、再次请求或卸载必须取消旧建议请求；旧响应不得覆盖当前论文、当前段落或清空后的状态。
- 章节树与章节正文请求使用递增序号或取消信号，只允许最新选择提交状态。

### S4：笔记不丢写与原子落盘

- GET/POST 笔记必须先确认 paper 存在；不存在返回 404，禁止创建孤立笔记。
- 笔记 UTF-8 大小上限 1MiB；服务端通过同目录临时文件、flush/fsync 和 `os.replace` 原子写入，失败保留旧文件并清理临时文件。
- 前端自动/手动保存共用串行 latest-wins 队列；旧响应不得把 `lastSavedNote` 回退。
- 返回/切换 paper 时先 flush 最新 dirty 内容；真正卸载至少发起最终 flush，并禁止结束后的 setState。
- 保存失败保留 dirty 状态和可见重试入口，不得用旧成功 timer 清除较新的失败状态。

### S5：受控生成评测与生产检索对齐

- `--with-llm` 必须显式给出一个或多个 `--qa-id`，仅允许 private dev，且 `--max-llm-calls` 不得小于选中数量；超预算在 0 次生成调用前退出 2。
- 生成前做 Kimi health preflight；失败立即停止。每题输出上限 512 tokens，不启用 web search。
- LLM 错误字符串或异常必须计入 `generation.error_count`，使 `generation.valid=false` 且进程非零；检索 PASS 不得掩盖生成失败。
- 私有生成报告只能写入已忽略的 `eval/private/`；报告记录模型、选中 QA、预算、成功/错误数，不记录密钥。
- 新增 `semantic-production` 评测 profile，严格复刻生产聊天的 `VectorStore.search(query, top_k=5)`，不得混入 BM25/RRF。指标实验只用 private dev，不读取或运行 holdout。

## 3. 验收标准

1. Chat 双击、断流、停止、切会话、卸载、重新生成均有自动化测试，证明无重复 POST、无旧流污染且 signal 生效。
2. SSE 提前 EOF 必须失败；finished/error 回调互斥；超时和取消正确清理 reader/timer。
3. WritingDesk 正文只在 JSON body；草稿可恢复且按论文隔离；乱序请求不会覆盖当前状态。
4. PaperDetail 离开时最新笔记被 flush；后端 404/1MiB/原子替换与失败保旧内容测试通过。
5. 固定 4 条 private dev QA 的 Kimi 生成烟测不超过 4 次生成调用且 `generation.valid=true`；公开冻结基准和全量工程 Gate 不回退。
