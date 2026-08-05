# Phase E：MCP 客户端化 规格说明书

> 来源：Phase 2 计划 Phase E 章节 + mcp_server.md / agent_graph.md 现状契约。
> 目标：PaperMind 从「只被外部调」（MCP 服务端已上线）升级为「也能消费外部 MCP 工具」。

## 1. 背景与目标

现状：PaperMind 已是 MCP **服务端**（`services/mcp_server.py`，FastMCP 挂载 /mcp，暴露 4 个只读工具）。本阶段增加 MCP **客户端**能力：连接外部 MCP server（首个场景：arXiv 检索），把外部工具结果注入对话上下文。

设计原则：默认关闭（未配置 `mcp_servers:` 时零行为变化）；外部工具故障隔离（任何一步失败都降级为纯本地上下文，不影响对话主链路）；触发保守（命中明确信号才调外部工具，避免无谓延迟与噪声）。

## 2. 现状（代码实证）

- `services/mcp_server.py`：服务端，与本阶段无关但共享 `mcp==1.3.0` 锁定版本
- `services/agent_graph.py`：LangGraph 前置编排图，现状为线性 3 节点（memory → retrieve → assemble，见 agent_graph.md 3.x）
- `core/config.py`：Config 单例读 config.yaml；新增配置块须走 `get()` 带默认值路径，缺失不报错
- 依赖约束：`mcp==1.3.0` 锁定（宪法第 16 条；更高版本与 FastAPI 0.110/starlette 冲突）。**mcp 1.3.0 自带 client 端 API**（`mcp.client.stdio` / `mcp.client.sse` / `ClientSession`），E1 零新增 Python 依赖。

## 3. 设计

### 3.1 E1：MCP client 管理器（`services/mcp_client.py`，新建）

**关键架构决策：外部 MCP server 一律作为独立进程运行**（stdio：`command+args`，或远程 SSE：`url`）。arXiv 等服务经 `uvx arxiv-mcp-server` 启动——uvx 为其管理**独立虚拟环境**，与本项目锁定的 mcp 1.3.0 零依赖冲突。因此 **E2 的 arxiv-mcp-server 不进 backend/requirements.txt**。

config.yaml 新增配置块（缺省为空列表 = 关闭）：

```yaml
mcp_servers:
  - name: arxiv
    command: uvx                # stdio 型：command + args [+ env]
    args: ["arxiv-mcp-server"]
    # env: { ... }              # 可选
  # - name: remote-x            # SSE 型：url
  #   url: http://127.0.0.1:9000/sse
```

接口契约（两侧子代理以此为准）：

```python
@dataclass
class ExternalTool:
    name: str          # "{server}.{tool}"，如 "arxiv.search"
    description: str
    server: str        # 配置中的 server name
    schema: dict       # 参数 JSON Schema

class MCPClientManager:
    def __init__(self, servers_config: list[dict]): ...
    def available(self) -> bool: ...        # 有配置即 True（不代表已连通）
    async def discover(self) -> list[ExternalTool]:
        # 逐 server 连接 + list_tools；单 server 失败仅记 warning 并跳过，不影响其他
        ...
    async def call_tool(self, tool_name: str, args: dict) -> str:
        # 按 "{server}.{tool}" 路由；超时 30s；异常 → 返回空串并记 [mcp-client] warning（不抛给上层）
        ...
    async def close(self) -> None: ...
```

- 连接生命周期：懒连接 + 进程级缓存；`close()` 供 lifespan 关闭钩子调用（可选接入，本轮可不接）
- 超时：连接 15s、调用 30s（asyncio.wait_for）
- 日志前缀统一 `[mcp-client]`

### 3.2 E2：agent_graph 接入 `external_tools` 节点

- 位置：retrieve 之后、assemble 之前（补充上下文而非替代本地检索）
- 触发信号（全部满足才调）：① 配置了至少一个 server 且 discover 有可用工具；② 用户问题命中信号词（初版：`arxiv`、`论文检索`、`最新研究`、`未收录`、`没有收录`、`不在库中`，小写匹配）
- 行为：命中后按信号选工具（初版：含 arxiv 信号且有 arxiv.* 工具则调用 search，query 原样传入，limit=3），结果以「外部检索补充」段落追加进 RAG 上下文；未命中任何工具时仅记日志
- **降级契约**：discover/call 任何异常 → 跳过外部补充，对话走纯本地路径（与现状一致）；总耗时预算 +10s 封顶（节点内 asyncio.timeout）
- 不改 SSE 帧格式、不改 citations 结构（外部结果不进 citations——引用忠实度校验只覆盖本地 chunk）

### 3.3 测试计划

- **E1**：用 `mcp` SDK 的 FastMCP 在测试进程内起 stdio echo server（ fixture 形式），断言 discover 列出工具、call_tool 往返、坏 command 降级、未配置 available()=False
- **E2**：mock MCPClientManager，断言信号命中/未命中两条路径、异常降级、上下文注入格式
- 全套件回归；pip check 零新增（E1 用既有 mcp 1.3.0）

## 4. 接口与数据

新增内部 API 仅 `MCPClientManager` / `ExternalTool`（3.1 契约）；无 HTTP 端点变化；config.yaml 新增 `mcp_servers:` 块（示例进 config.yaml.example）。

## 5. 验收标准（可测试）

- [ ] AC1：未配置 mcp_servers 时全套件全绿、对话路径字节级不回归（既有 chat 测试全过）
- [ ] AC2：进程内 echo server 的 discover/call_tool 集成用例通过
- [ ] AC3：信号命中时上下文含「外部检索补充」段；信号未命中或工具异常时上下文与现状一致
- [ ] AC4：pip check 零新增依赖（arxiv-mcp-server 不进 requirements）
- [ ] AC5：config.yaml.example 含 mcp_servers 注释样例

## 6. 现有测试覆盖与盲区

- 新增 `tests/test_mcp_client.py`（E1）、`tests/test_agent_external_tools.py`（E2）
- 遗留（后继批次）：真实 arxiv server 的端到端联调（需 uvx 环境与网络）；close() 接 lifespan；信号词表配置化

## 7. 风险与回退

- **风险**：mcp 1.3.0 client API 与新版 server 的协议协商差异 → 缓解：stdio JSON-RPC 是稳定子集；真实联调列遗留
- **风险**：外部进程启动慢拖慢首问 → 缓解：懒连接 + 触发信号保守 + 10s 预算
- **回退**：config.yaml 不配 mcp_servers 即完全关闭本特性
