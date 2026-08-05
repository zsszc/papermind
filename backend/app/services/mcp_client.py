"""MCP Client：连接外部 MCP server 并把它们的工具暴露给对话编排层。

设计说明：
- 外部 MCP server 一律作为独立进程运行（stdio：command+args；或远程 SSE：url），
  与本进程锁定的 mcp 1.3.0 零依赖冲突（如 uvx 为 arxiv-mcp-server 管理独立 venv）。
- 懒连接 + 进程级缓存：首次 discover/call_tool 时才拉起子进程，连接按 server
  name 缓存复用；close() 供 lifespan 关闭钩子调用（可选接入）。
- 故障隔离：单 server 连接/发现失败仅记 warning 并跳过；call_tool 任何异常
  （含超时、传输错误、工具内部错误 isError）都降级返回空串，绝不抛给上层。
- 超时：连接 15s、调用 30s（asyncio.wait_for）。日志前缀统一 [mcp-client]。
"""

import asyncio
from contextlib import AsyncExitStack
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from mcp import ClientSession, StdioServerParameters
from mcp.client.sse import sse_client
from mcp.client.stdio import stdio_client

from app.core.logger import logger

# 超时秒数：连接（拉起子进程 + initialize）与单次调用
CONNECT_TIMEOUT = 15
CALL_TIMEOUT = 30


def _extract_text(content: list) -> str:
    """拼接结果中的全部文本内容块；非文本块（图片/资源等）忽略。"""
    return "\n".join(t for c in content if (t := getattr(c, "text", None)))


@dataclass
class ExternalTool:
    """外部 MCP 工具的统一表示（供 agent_graph 注入上下文使用）。"""

    name: str  # "{server}.{tool}"，如 "arxiv.search"
    description: str
    server: str  # 配置中的 server name
    schema: dict  # 参数 JSON Schema


class _ServerConnection:
    """单个 server 的存活连接：AsyncExitStack 持有 stdio 子进程与 ClientSession。"""

    def __init__(self, stack: AsyncExitStack, session: ClientSession):
        self.stack = stack
        self.session = session


class MCPClientManager:
    """外部 MCP server 的客户端管理器。

    servers_config 来自 config.yaml 的 mcp_servers 块（缺省空列表 = 关闭）。
    """

    def __init__(self, servers_config: List[dict]):
        self._servers: List[dict] = servers_config or []
        # 连接缓存：server name -> _ServerConnection（懒连接，命中即复用）
        self._connections: Dict[str, _ServerConnection] = {}

    def available(self) -> bool:
        """有配置即 True（不代表已连通）。"""
        return bool(self._servers)

    async def discover(self) -> List[ExternalTool]:
        """逐 server 连接 + list_tools；单 server 失败仅记 warning 并跳过。"""
        tools: List[ExternalTool] = []
        for cfg in self._servers:
            name = cfg.get("name", "?")
            try:
                session = await self._ensure_connected(cfg)
                result = await asyncio.wait_for(session.list_tools(), timeout=CALL_TIMEOUT)
            except Exception as exc:
                logger.warning(f"[mcp-client] server {name} 发现工具失败，跳过：{exc}")
                continue
            for t in result.tools:
                tools.append(
                    ExternalTool(
                        name=f"{name}.{t.name}",
                        description=t.description or "",
                        server=name,
                        schema=t.inputSchema or {},
                    )
                )
        return tools

    async def call_tool(self, tool_name: str, args: dict) -> str:
        """按 "{server}.{tool}" 路由调用；任何异常返回空串并记 warning。"""
        server, _, tool = tool_name.partition(".")
        if not server or not tool:
            logger.warning(f"[mcp-client] 非法工具名 {tool_name!r}，应为 server.tool 形式")
            return ""
        cfg = next((c for c in self._servers if c.get("name") == server), None)
        if cfg is None:
            logger.warning(f"[mcp-client] 未配置的 server {server}，放弃调用 {tool_name}")
            return ""
        try:
            session = await self._ensure_connected(cfg)
            result = await asyncio.wait_for(
                session.call_tool(tool, args), timeout=CALL_TIMEOUT
            )
        except Exception as exc:
            logger.warning(f"[mcp-client] 调用 {tool_name} 失败：{exc}")
            return ""
        if result.isError:
            # 工具内部错误（如 arXiv 检索失败）同样降级为空串
            logger.warning(
                f"[mcp-client] 工具 {tool_name} 返回错误：{_extract_text(result.content)}"
            )
            return ""
        return _extract_text(result.content)

    async def close(self) -> None:
        """关闭全部缓存连接（供 lifespan 关闭钩子调用）；单个失败不影响其余。

        注意捕获 CancelledError：anyio cancel scope 跨 task 退出时 aclose 可能
        抛出它（BaseException，不在 Exception 谱系），此处绝不向上抛——
        lifespan 关闭路径不能被 MCP 清理失败中断。
        """
        for name, conn in list(self._connections.items()):
            try:
                await conn.stack.aclose()
            except (Exception, asyncio.CancelledError) as exc:
                logger.warning(f"[mcp-client] 关闭 server {name} 连接失败：{exc}")
        self._connections.clear()

    # ---------- 内部 ----------

    async def _ensure_connected(self, cfg: dict) -> ClientSession:
        """懒连接：缓存命中直接复用，否则带 15s 超时建立新连接。"""
        name = cfg.get("name", "?")
        conn = self._connections.get(name)
        if conn is not None:
            return conn.session
        return await asyncio.wait_for(self._connect(cfg), timeout=CONNECT_TIMEOUT)

    async def _connect(self, cfg: dict) -> ClientSession:
        """建立到单个 server 的连接（stdio 或 SSE），完成 initialize 握手。"""
        name = cfg.get("name", "?")
        stack = AsyncExitStack()
        try:
            if cfg.get("command"):
                params = StdioServerParameters(
                    command=cfg["command"],
                    args=cfg.get("args") or [],
                    env=cfg.get("env"),
                )
                read, write = await stack.enter_async_context(stdio_client(params))
            elif cfg.get("url"):
                read, write = await stack.enter_async_context(sse_client(cfg["url"]))
            else:
                raise ValueError(f"server {name} 配置缺少 command 或 url")
            session = await stack.enter_async_context(ClientSession(read, write))
            await session.initialize()
        except BaseException:
            # 连接半途失败（含超时取消）：尽力清理已建立的子进程/流，再向上抛。
            # 清理本身也可能抛 CancelledError（anyio cancel scope 跨 task 退出），
            # 同样吞掉——保证原始失败原因优先传播给调用方。
            try:
                await stack.aclose()
            except (Exception, asyncio.CancelledError):
                pass
            raise
        self._connections[name] = _ServerConnection(stack, session)
        return session
