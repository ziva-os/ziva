from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Dict, List

from ziva_runtime.shared_types import ToolResult


@dataclass
class MCPServerConfig:
    name: str
    command: str | None = None
    args: List[str] = field(default_factory=list)
    url: str | None = None
    transport: str = "stdio"  # "stdio" or "http"
    environment: Dict[str, str] = field(default_factory=dict)
    timeout: int = 120  # seconds


class MCPToolWrapper:
    """Wraps an MCP-discovered tool as a Ziva-compatible tool plugin.

    Tool spec (name, description, parameters) is registered once globally.
    At run time, routes to the calling session's MCP client so each session
    has its own stdio subprocess.
    """

    def __init__(self, tool_name: str, tool_description: str, tool_schema: Dict[str, Any], server_name: str):
        self._name = tool_name
        self._description = tool_description
        self._schema = tool_schema
        self._server_name = server_name

    def spec(self) -> Dict[str, Any]:
        return {
            "name": self._name,
            "description": self._description,
            "input_schema": self._schema,
        }

    async def run(self, input_data: Dict[str, Any], ctx: Any) -> ToolResult:
        runtime = ctx.metadata.get("_runtime")
        if not runtime:
            return ToolResult(text="Error: mcp_unavailable\nRuntime not accessible", error=True)
        session = runtime._get_session(ctx.session_id)
        if not session.mcp_client:
            return ToolResult(text="Error: mcp_not_connected\nMCP not connected for this session", error=True)
        server = session.mcp_client.get_server(self._server_name)
        if not server:
            return ToolResult(text=f"Error: mcp_server_not_found\nMCP server '{self._server_name}' not found", error=True)
        try:
            result = await server.call_tool(self._name, input_data)
            # MCP CallToolResult has a .content list of TextContent / ImageContent.
            # Extract text from content items; fall back to stringifying.
            texts = []
            images = []
            contents = getattr(result, "content", None) or []
            if not contents and isinstance(result, dict):
                contents = result.get("content", [])
            for item in contents:
                if isinstance(item, dict):
                    if item.get("type") == "text":
                        texts.append(item.get("text", ""))
                    elif item.get("type") == "image" and item.get("data"):
                        images.append(f"data:{item.get('mimeType', 'image/png')};base64,{item['data']}")
                elif hasattr(item, "type"):
                    if item.type == "text":
                        texts.append(getattr(item, "text", ""))
                    elif item.type == "image":
                        data = getattr(item, "data", "")
                        if data:
                            mime = getattr(item, "mimeType", "image/png")
                            images.append(f"data:{mime};base64,{data}")
            if texts or images:
                return ToolResult(text="\n".join(texts), images=images)
            # Fallback for non-standard results
            if hasattr(result, "model_dump"):
                return ToolResult(text=str(result.model_dump()))
            return ToolResult(text=str(result))
        except Exception as e:
            msg = str(e)
            if "cancel scope" in msg:
                return ToolResult(text="Error: mcp_connection_lost\nMCP server connection closed", error=True)
            return ToolResult(text=f"Error: mcp_call_failed\n{msg}", error=True)


class MCPClient:
    """Manages connections to MCP servers and discovers tools."""

    def __init__(self, configs: List[MCPServerConfig]):
        self._configs = configs
        self._servers: Dict[str, Any] = {}
        self._tools: List[MCPToolWrapper] = []
        self._connected = False

    async def connect_all(self) -> List[MCPToolWrapper]:
        if self._connected:
            return self._tools

        for cfg in self._configs:
            try:
                await self._connect_server(cfg)
            except Exception as e:
                print(f"MCP: Failed to connect to {cfg.name}: {e}")
        self._connected = True
        return self._tools

    async def _connect_server(self, cfg: MCPServerConfig) -> None:
        try:
            from agents.mcp import MCPServerStdio
        except ImportError:
            raise RuntimeError("openai-agents is required for MCP support")

        if cfg.transport in ("stdio", "local") and cfg.command:
            import os
            env = {**os.environ, **cfg.environment}
            server = MCPServerStdio(
                name=cfg.name,
                params={"command": cfg.command, "args": cfg.args, "env": env},
                client_session_timeout_seconds=cfg.timeout,
            )
        else:
            raise ValueError(f"Unsupported MCP transport: {cfg.transport}")

        self._servers[cfg.name] = server
        await server.connect()
        tools = await server.list_tools()
        for tool_def in tools:
            name = getattr(tool_def, "name", "") or ""
            desc = getattr(tool_def, "description", "") or ""
            schema = getattr(tool_def, "inputSchema", None) or {"type": "object", "properties": {}}
            wrapper = MCPToolWrapper(
                tool_name=name,
                tool_description=desc,
                tool_schema=schema,
                server_name=cfg.name,
            )
            self._tools.append(wrapper)

    @property
    def connected_servers(self) -> List[str]:
        return list(self._servers.keys())

    def get_server(self, name: str) -> Any:
        return self._servers.get(name)

    async def cleanup(self) -> None:
        """Gracefully close all MCP server connections in the current task."""
        for name, server in list(self._servers.items()):
            try:
                if hasattr(server, "cleanup"):
                    await server.cleanup()
            except Exception:
                pass
        self._servers.clear()
        self._connected = False


def parse_mcp_config(config: Dict[str, Any]) -> List[MCPServerConfig]:
    mcp_section = config.get("mcp", {})
    servers = mcp_section.get("servers", [])

    if not servers:
        return []

    result = []

    # Dict format: {name: {type, command, environment, enabled}}
    if isinstance(servers, dict):
        for name, srv in servers.items():
            if not isinstance(srv, dict):
                continue
            if not srv.get("enabled", True):
                continue
            cmd = srv.get("command")
            if isinstance(cmd, list):
                cmd, args = cmd[0], cmd[1:]
            else:
                args = srv.get("args", [])
            result.append(MCPServerConfig(
                name=name,
                command=cmd,
                args=args,
                url=srv.get("url"),
                transport=srv.get("type", "stdio"),
                environment=srv.get("environment", {}),
                timeout=int(srv.get("timeout", 120)),
            ))
    # List format: [{name, command, args, ...}]
    elif isinstance(servers, list):
        for s in servers:
            if not isinstance(s, dict):
                continue
            result.append(MCPServerConfig(
                name=s.get("name", "unnamed"),
                command=s.get("command"),
                args=s.get("args", []),
                url=s.get("url"),
                transport=s.get("transport", "stdio"),
            ))
    return result
