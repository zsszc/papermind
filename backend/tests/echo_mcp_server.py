"""测试用 echo MCP server：经 stdio 暴露两个工具。

由 test_mcp_client.py 通过 stdio_client 以子进程方式拉起（sys.executable 即
venv 内带 mcp 的 Python），纯本地进程通信，不做任何网络调用。

工具：
- echo：原样回显输入文本，用于 discover / call_tool 往返验证。
- boom：总是抛异常，用于验证工具执行失败时客户端降级为空串。
"""

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("echo")


@mcp.tool()
def echo(text: str) -> str:
    """原样回显输入文本。"""
    return text


@mcp.tool()
def boom(text: str) -> str:
    """总是失败的工具（模拟外部工具内部错误）。"""
    raise RuntimeError("echo server 内部错误")


if __name__ == "__main__":
    # stdio 传输：由 MCPClientManager 以子进程方式拉起
    mcp.run()
