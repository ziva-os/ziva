from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Dict, List


@dataclass
class MCPServerConfig:
    name: str
    command: str | None = None
    args: List[str] = field(default_factory=list)
    url: str | None = None
    transport: str = "stdio"  # "stdio" or "http"
    environment: Dict[str, str] = field(default_factory=dict)


class MCPToolWrapper:
    """Wraps an MCP-discovered tool as a Ziva-compatible tool plugin."""

    def __init__(self, tool_name: str, tool_description: str, tool_schema: Dict[str, Any], mcp_server: Any):
        self._name = tool_name
        self._description = tool_description
        self._schema = tool_schema
        self._server = mcp_server

    def spec(self) -> Dict[str, Any]:
        return {
            "name": self._name,
            "description": self._description,
            "input_schema": self._schema,
        }

    async def run(self, input_data: Dict[str, Any], ctx: Any) -> Dict[str, Any]:
        try:
            result = await self._server.call_tool(self._name, input_data)
            if isinstance(result, dict):
                return result
            if hasattr(result, "model_dump"):
                return result.model_dump()
            return {"result": str(result)}
        except Exception as e:
            # anyio cancel-scope errors from MCP stdio cleanup are
            # benign — suppress them so the turn doesn't crash.
            msg = str(e)
            if "cancel scope" in msg:
                return {"error": "mcp_connection_lost", "message": "MCP server connection closed"}
            return {"error": "mcp_call_failed", "message": msg}


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
                client_session_timeout_seconds=30,
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
                mcp_server=server,
            )
            self._tools.append(wrapper)

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
