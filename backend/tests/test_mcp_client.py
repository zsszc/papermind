"""MCP Client 管理器测试：进程内 FastMCP echo server 的 discover / call_tool 往返。

- echo server 以 stdio 子进程方式拉起（tests/echo_mcp_server.py），全程本地，无网络。
- 坏 command 的 server 必须被降级跳过，不影响其他 server。
- call_tool 任何异常（含工具内部错误 isError）都返回空串，不抛给上层。
"""

import sys
from pathlib import Path

import pytest

from app.services.mcp_client import MCPClientManager

_ECHO_SCRIPT = Path(__file__).parent / "echo_mcp_server.py"


@pytest.fixture()
def echo_config() -> list[dict]:
    """指向进程内 echo server 的 stdio 配置。"""
    return [{"name": "echo", "command": sys.executable, "args": [str(_ECHO_SCRIPT)]}]


@pytest.fixture()
def bad_config() -> list[dict]:
    """不存在命令的坏 server 配置（连接必然失败）。"""
    return [{"name": "bad", "command": "papermind-nonexistent-command-xyz", "args": []}]


# ---------- available ----------


@pytest.mark.asyncio()
async def test_available_false_when_no_servers():
    """未配置 mcp_servers（空列表）时 available() 为 False，discover 返回空。"""
    manager = MCPClientManager([])
    assert manager.available() is False
    assert await manager.discover() == []


@pytest.mark.asyncio()
async def test_available_true_when_configured(echo_config):
    """有配置即 available()=True（不要求已连通）。"""
    manager = MCPClientManager(echo_config)
    assert manager.available() is True


# ---------- discover + call_tool 往返 ----------


@pytest.mark.asyncio()
async def test_discover_and_call_tool_roundtrip(echo_config):
    """对 echo server 完成 discover（列出工具）与 call_tool（回显）往返。"""
    manager = MCPClientManager(echo_config)
    try:
        tools = await manager.discover()
        names = {t.name for t in tools}
        assert "echo.echo" in names
        assert "echo.boom" in names
        # ExternalTool 结构：server 字段与参数 schema
        echo_tool = next(t for t in tools if t.name == "echo.echo")
        assert echo_tool.server == "echo"
        assert "text" in echo_tool.schema.get("properties", {})

        result = await manager.call_tool("echo.echo", {"text": "你好 MCP"})
        assert "你好 MCP" in result

        # 连接缓存：再次 discover 不重连，结果一致
        tools2 = await manager.discover()
        assert {t.name for t in tools2} == names
    finally:
        await manager.close()


# ---------- 坏 server 降级 ----------


@pytest.mark.asyncio()
async def test_bad_command_server_skipped(bad_config, echo_config):
    """坏 command 的 server 仅记 warning 并跳过，不影响好 server 的发现。"""
    manager = MCPClientManager(bad_config + echo_config)
    try:
        tools = await manager.discover()
        names = {t.name for t in tools}
        assert "echo.echo" in names
        assert not any(n.startswith("bad.") for n in names)
    finally:
        await manager.close()


@pytest.mark.asyncio()
async def test_bad_command_only_discover_empty(bad_config):
    """全是坏 server 时 discover 返回空列表而不抛异常。"""
    manager = MCPClientManager(bad_config)
    try:
        assert await manager.discover() == []
    finally:
        await manager.close()


# ---------- call_tool 异常降级为空串 ----------


@pytest.mark.asyncio()
async def test_call_tool_tool_error_returns_empty(echo_config):
    """工具内部抛异常（isError）时返回空串，不抛给上层。"""
    manager = MCPClientManager(echo_config)
    try:
        assert await manager.call_tool("echo.boom", {"text": "x"}) == ""
    finally:
        await manager.close()


@pytest.mark.asyncio()
async def test_call_tool_unreachable_server_returns_empty(bad_config):
    """连接不上 server 时 call_tool 返回空串。"""
    manager = MCPClientManager(bad_config)
    try:
        assert await manager.call_tool("bad.anything", {}) == ""
    finally:
        await manager.close()


@pytest.mark.asyncio()
async def test_call_tool_unknown_server_returns_empty(echo_config):
    """tool_name 路由到未配置的 server 时返回空串。"""
    manager = MCPClientManager(echo_config)
    try:
        assert await manager.call_tool("ghost.nothing", {}) == ""
        assert await manager.call_tool("no-dot-name", {}) == ""
    finally:
        await manager.close()


# ---------- close 健壮性 ----------


def test_close_never_raises_under_plain_asyncio_run(echo_config, bad_config):
    """真实 lifespan 场景（裸 asyncio.run）下 close 不得抛出任何异常。

    anyio cancel scope 跨 task 退出时 aclose 可能抛 CancelledError
    （BaseException，非 Exception），close 必须连它一起吞掉——
    否则 FastAPI lifespan 关闭钩子会被 MCP 清理失败炸掉。
    本用例为同步测试，内部自建事件循环以复现生产 task 结构。
    """
    import asyncio

    async def lifecycle():
        # 混合配置：坏 server 失败 + 好 server 成功，覆盖两条清理路径
        m1 = MCPClientManager(bad_config + echo_config)
        await m1.discover()
        await m1.call_tool("echo.echo", {"text": "x"})
        await m1.close()
        await m1.close()  # 幂等：二次 close 同样不抛

        m2 = MCPClientManager(echo_config)
        await m2.discover()
        await m2.close()

    asyncio.run(lifecycle())  # close 若抛出（含 CancelledError）则此测试失败
