"""真实 MCP stdio 客户端占位模块（阶段 B1 实现）。

阶段 B1 将通过官方 `mcp` 客户端拉起 `skills.skill_2_stat_mcp.server`，
完成标准 stdio 协议的 `list_tools` 与 `call_tool` 调用并记录耗时。
严禁通过直接 import 统计函数伪装成 MCP 协议通信。
"""

from __future__ import annotations

from typing import Any


class MCPClientNotImplementedError(NotImplementedError):
    """阶段 A 留桩异常，提示在阶段 B1 实现真实 stdio MCP 客户端。"""


class StatMCPClient:
    """阶段 A 占位客户端，阶段 B1 替换为真实 MCP stdio 实现。"""

    def __init__(self, config_path: str = "configs/mcp_servers.yaml") -> None:
        self.config_path = config_path

    async def list_tools(self) -> list[dict[str, Any]]:
        raise MCPClientNotImplementedError(
            "StatMCPClient.list_tools is stubbed in Stage A and must be implemented in Stage B1."
        )

    async def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        raise MCPClientNotImplementedError(
            "StatMCPClient.call_tool is stubbed in Stage A and must be implemented in Stage B1."
        )
