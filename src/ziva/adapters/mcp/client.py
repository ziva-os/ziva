from __future__ import annotations

import asyncio
import base64
import json
import logging
import shlex
from dataclasses import dataclass, field
from typing import Any, Dict, List

from ziva.shared_types import ToolResult

logger = logging.getLogger(__name__)


@dataclass
class MCPServerConfig:
    name: str
    command: str | None = None
    args: List[str] = field(default_factory=list)
    url: str | None = None
    transport: str = "stdio"  # "stdio" or "http"
    environment: Dict[str, str] = field(default_factory=dict)
    headers: Dict[str, str] = field(default_factory=dict)
    cwd: str | None = None
    timeout: int = 120  # seconds
    max_retry_attempts: int = 2  # retry transient failures (timeout / connect / HTTP 5xx)
    retry_backoff_seconds_base: float = 1.0


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

        # If the session has no MCP client, try to connect now using the
        # current runtime config. This handles the case where the user enabled
        # MCP after the session was created, or switched from a workspace
        # without MCP to one with MCP configured.
        if not session.mcp_client:
            try:
                await runtime._connect_mcp_if_needed(ctx.session_id)
            except Exception as e:
                logger.warning("MCP on-demand connect failed: %s", e)

        if not session.mcp_client:
            # Distinguish "no MCP configured" from "configured but failed to connect".
            from ziva.shared_types import MCPConnectStatus
            if session.mcp_status == MCPConnectStatus.NO_CONFIG:
                return ToolResult(
                    text="Error: mcp_not_configured\n"
                         "MCP is not configured. Add servers in Settings → MCP.",
                    error=True,
                )
            if session.mcp_status == MCPConnectStatus.FAILED:
                return ToolResult(
                    text="Error: mcp_connect_failed\n"
                         "MCP server failed to connect. "
                         "Check the configured command and environment.",
                    error=True,
                )
            return ToolResult(
                text="Error: mcp_not_connected\nMCP not connected for this session",
                error=True,
            )
        server = session.mcp_client.get_server(self._server_name)
        if not server:
            return ToolResult(text=f"Error: mcp_server_not_found\nMCP server '{self._server_name}' not found", error=True)
        try:
            result = await asyncio.wait_for(server.call_tool(self._name, input_data), timeout=120)
            return mcp_call_result_to_tool_result(result)
        except asyncio.TimeoutError:
            return ToolResult(text="Error: mcp_timeout\nMCP tool call timed out after 120s", error=True)
        except Exception as e:
            msg = str(e)
            if "cancel scope" in msg:
                return ToolResult(text="Error: mcp_connection_lost\nMCP server connection closed", error=True)
            return ToolResult(text=f"Error: mcp_call_failed\n{msg}", error=True)


def _to_plain_data(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, list):
        return [_to_plain_data(v) for v in value]
    if isinstance(value, tuple):
        return [_to_plain_data(v) for v in value]
    if isinstance(value, dict):
        return {k: _to_plain_data(v) for k, v in value.items()}
    if hasattr(value, "model_dump"):
        return _to_plain_data(value.model_dump(by_alias=True, exclude_none=True))
    if hasattr(value, "dict"):
        try:
            return _to_plain_data(value.dict(by_alias=True, exclude_none=True))
        except TypeError:
            return _to_plain_data(value.dict())
    return value


def _get_field(value: Any, *names: str, default: Any = None) -> Any:
    for name in names:
        if isinstance(value, dict) and name in value:
            return value[name]
        if not isinstance(value, dict) and hasattr(value, name):
            return getattr(value, name)
    return default


def _json_text(value: Any) -> str:
    try:
        return json.dumps(_to_plain_data(value), ensure_ascii=False, indent=2, default=str)
    except TypeError:
        return str(value)


def _data_url(data: Any, mime_type: str) -> str | None:
    if not data:
        return None
    if isinstance(data, bytes):
        encoded = base64.b64encode(data).decode("ascii")
    else:
        encoded = str(data)
    if encoded.startswith("data:"):
        return encoded
    return f"data:{mime_type};base64,{encoded}"


def _extract_contents(result: Any) -> list[Any]:
    """Extract the content list from various MCP result shapes."""
    if result is None:
        return []
    contents = _get_field(result, "content", default=None)
    if contents is not None:
        return list(contents) if isinstance(contents, (list, tuple)) else [contents]
    if isinstance(result, (list, tuple)):
        return list(result)
    if isinstance(result, dict) and "type" in result:
        return [result]
    return []


def _extract_image_data_urls(text: str) -> tuple[str, list[str]]:
    """Extract data:image/...;base64,... URLs from plain text.

    Some MCP servers (and misbehaving clients) embed image base64 blobs
    inside ``type: "text"`` content items. If left as text, those huge
    blobs inflate the token count and can be truncated by compaction or
    other hooks, leaving the multimodal model with no usable image.

    Returns the cleaned text (with image URLs replaced by placeholders)
    and a list of extracted data URLs that can be placed in
    ``ToolResult.images`` instead.
    """
    import re

    images: list[str] = []
    cleaned_parts: list[str] = []
    last_end = 0

    # Markdown image syntax: ![alt](data:image/...;base64,...)
    md_pattern = re.compile(
        r"!\[([^\]]*)\]\((data:image/[a-zA-Z0-9+.-]+;base64,[A-Za-z0-9+/]+={0,2})\)"
    )
    for match in md_pattern.finditer(text):
        start, end = match.span()
        images.append(match.group(2))
        cleaned_parts.append(text[last_end:start])
        alt = match.group(1).strip()
        cleaned_parts.append(f"[Image: {alt}]" if alt else "[Image]")
        last_end = end

    # Raw data URLs that are not inside markdown images.
    data_url_pattern = re.compile(
        r"data:image/[a-zA-Z0-9+.-]+;base64,[A-Za-z0-9+/]+={0,2}"
    )
    remainder = text[last_end:]
    last_end = 0
    for match in data_url_pattern.finditer(remainder):
        start, end = match.span()
        images.append(match.group(0))
        cleaned_parts.append(remainder[last_end:start])
        cleaned_parts.append("[Image]")
        last_end = end
    cleaned_parts.append(remainder[last_end:])

    return "".join(cleaned_parts), images


def _parse_content_item(item: Any) -> tuple[list[str], list[str], list[str], list[dict]]:
    """Parse a single MCP content item into text, image, audio, and resource parts."""
    texts: list[str] = []
    images: list[str] = []
    audios: list[str] = []
    resources: list[dict] = []

    if not isinstance(item, dict):
        if item is not None:
            texts.append(_json_text(item))
        return texts, images, audios, resources

    item_type = _get_field(item, "type", default="")

    if item_type == "text":
        raw_text = str(_get_field(item, "text", default=""))
        cleaned_text, extracted_images = _extract_image_data_urls(raw_text)
        if cleaned_text:
            texts.append(cleaned_text)
        images.extend(extracted_images)
    elif item_type == "image":
        data = _get_field(item, "data")
        mime_type = _get_field(item, "mimeType", "mime_type", default="image/png")
        url = _data_url(data, str(mime_type))
        if url:
            images.append(url)
    elif item_type == "audio":
        data = _get_field(item, "data")
        mime_type = _get_field(item, "mimeType", "mime_type", default="audio/wav")
        url = _data_url(data, str(mime_type))
        if url:
            audios.append(url)
            texts.append(f"[Audio content: {mime_type}]")
    elif item_type in ("resource", "embedded_resource"):
        resource = _get_field(item, "resource", default=item)
        if isinstance(resource, dict):
            uri = _get_field(resource, "uri")
            mime_type = _get_field(resource, "mimeType", "mime_type", default="application/octet-stream")
            text = _get_field(resource, "text")
            blob = _get_field(resource, "blob")
            resources.append(_to_plain_data(resource))
            if text is not None:
                prefix = f"Resource {uri}:\n" if uri else "Resource:\n"
                texts.append(prefix + str(text))
            elif blob is not None:
                try:
                    # attempt to decode base64 text if mime_type hints at text
                    if str(mime_type).startswith("text/"):
                        decoded = base64.b64decode(blob).decode("utf-8")
                        prefix = f"Resource {uri}:\n" if uri else "Resource:\n"
                        texts.append(prefix + decoded)
                    else:
                        url = _data_url(blob, str(mime_type))
                        if url and str(mime_type).startswith("image/"):
                            images.append(url)
                        label = f"Resource {uri}" if uri else "Resource"
                        texts.append(f"[{label}: {mime_type} blob]")
                except Exception:
                    texts.append(f"[{uri or 'Resource'}: {mime_type} base64 data]")
    elif item_type in ("resource_link", "resourceLink"):
        uri = _get_field(item, "uri", default="")
        name = _get_field(item, "name", default="")
        desc = _get_field(item, "description", default="")
        resources.append(_to_plain_data(item))
        label = name or uri or "resource"
        suffix = f" - {desc}" if desc else ""
        texts.append(f"[Resource link: {label}{suffix}]")
    else:
        plain = _to_plain_data(item)
        if isinstance(plain, dict) and "text" in plain:
            texts.append(str(plain["text"]))
        elif plain not in ({}, None):
            texts.append(_json_text(plain))

    return texts, images, audios, resources


def mcp_call_result_to_tool_result(result: Any) -> ToolResult:
    """Convert all standard MCP call result shapes into a Ziva ToolResult."""
    # Official MCP spec defines `isError` flag
    is_error = bool(_get_field(result, "isError", "is_error", default=False))

    contents = _extract_contents(result)

    texts: list[str] = []
    images: list[str] = []
    audios: list[str] = []
    resources: list[dict] = []

    for item in contents:
        t, i, a, r = _parse_content_item(item)
        texts.extend(t)
        images.extend(i)
        audios.extend(a)
        resources.extend(r)

    metadata: dict[str, Any] = {}
    if audios:
        metadata["audio"] = audios
    if resources:
        metadata["resources"] = resources

    if texts or images or metadata:
        return ToolResult(text="\n".join(t for t in texts if t), images=images, error=is_error, metadata=metadata)

    # Fallback to pure json stringification if the result structure is totally non-standard
    plain = _to_plain_data(result)
    return ToolResult(text=_json_text(plain), error=is_error, metadata={"raw": plain} if isinstance(plain, dict) else {})


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
                logger.warning("MCP: Failed to connect to %s: %s", cfg.name, e)
        self._connected = True
        return self._tools

    async def _connect_server(self, cfg: MCPServerConfig) -> None:
        # Local thin wrapper over the `mcp` SDK (adapters/mcp/server.py),
        # replacing the former openai-agents `agents.mcp` dependency.
        from ziva.adapters.mcp.server import (
            MCPServerSse, MCPServerStdio, MCPServerStreamableHttp,
        )

        transport = cfg.transport.lower().replace("-", "_")
        if transport in ("stdio", "local") and cfg.command:
            import os
            env = {**os.environ, **cfg.environment}
            params: Dict[str, Any] = {"command": cfg.command, "args": cfg.args, "env": env}
            if cfg.cwd:
                params["cwd"] = cfg.cwd
            server = MCPServerStdio(
                name=cfg.name,
                params=params,
                client_session_timeout_seconds=cfg.timeout,
                max_retry_attempts=cfg.max_retry_attempts,
                retry_backoff_seconds_base=cfg.retry_backoff_seconds_base,
            )
        elif transport in ("http", "streamable_http", "streamablehttp") and cfg.url:
            params = {"url": cfg.url, "headers": cfg.headers, "timeout": cfg.timeout}
            server = MCPServerStreamableHttp(
                name=cfg.name,
                params=params,
                client_session_timeout_seconds=cfg.timeout,
                max_retry_attempts=cfg.max_retry_attempts,
                retry_backoff_seconds_base=cfg.retry_backoff_seconds_base,
            )
        elif transport == "sse" and cfg.url:
            params = {"url": cfg.url, "headers": cfg.headers, "timeout": cfg.timeout}
            server = MCPServerSse(
                name=cfg.name,
                params=params,
                client_session_timeout_seconds=cfg.timeout,
                max_retry_attempts=cfg.max_retry_attempts,
                retry_backoff_seconds_base=cfg.retry_backoff_seconds_base,
            )
        else:
            raise ValueError(f"Unsupported MCP transport: {cfg.transport}")

        # Suppress MCP subprocess stderr (install logs, startup banners).
        # Use a real file (devnull) so subprocess.spawn gets a valid fd.
        if transport in ("stdio", "local"):
            import os
            _devnull = open(os.devnull, "w")

            def _quiet_streams():
                from mcp.client.stdio import stdio_client
                return stdio_client(server.params, errlog=_devnull)

            server.create_streams = _quiet_streams  # type: ignore[assignment]

        self._servers[cfg.name] = server
        await server.connect()
        tools = await server.list_tools()
        for tool_def in tools:
            name = (
                getattr(tool_def, "name", None)
                or (tool_def.get("name") if isinstance(tool_def, dict) else None)
                or ""
            )
            desc = (
                getattr(tool_def, "description", None)
                or (tool_def.get("description") if isinstance(tool_def, dict) else None)
                or ""
            )
            schema = (
                getattr(tool_def, "inputSchema", None)
                or getattr(tool_def, "input_schema", None)
                or (tool_def.get("inputSchema") if isinstance(tool_def, dict) else None)
                or (tool_def.get("input_schema") if isinstance(tool_def, dict) else None)
                or {"type": "object", "properties": {}}
            )
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
        for _, server in list(self._servers.items()):
            try:
                if hasattr(server, "cleanup"):
                    await server.cleanup()
            except Exception:
                pass
        self._servers.clear()
        self._connected = False


def _coerce_str_dict(value: Any) -> Dict[str, str]:
    if not isinstance(value, dict):
        return {}
    return {str(k): str(v) for k, v in value.items()}


def _normalize_transport(raw: Any, *, has_url: bool) -> str:
    value = str(raw or "").lower().replace("-", "_")
    if value in ("", "local"):
        return "stdio"
    if value in ("stdio", "sse", "http", "streamable_http", "streamablehttp"):
        return "streamable_http" if value == "streamablehttp" else value
    if value == "remote" and has_url:
        return "streamable_http"
    return value


def _mcp_server_from_mapping(name: str, srv: Dict[str, Any]) -> MCPServerConfig | None:
    if not srv.get("enabled", True) or srv.get("disabled", False):
        return None
    cmd = srv.get("command")
    if isinstance(cmd, list):
        cmd, args = (cmd[0] if cmd else None), [str(a) for a in cmd[1:]]
    elif isinstance(cmd, str) and cmd:
        parts = shlex.split(cmd)
        cmd, args = (parts[0] if parts else None), [str(a) for a in parts[1:]]
    else:
        args = [str(a) for a in srv.get("args", []) or []]
    url = srv.get("url") or srv.get("server_url")
    transport = _normalize_transport(srv.get("transport", srv.get("type")), has_url=bool(url))
    return MCPServerConfig(
        name=name,
        command=str(cmd) if cmd else None,
        args=args,
        url=str(url) if url else None,
        transport=transport,
        environment=_coerce_str_dict(srv.get("environment") or srv.get("env")),
        headers=_coerce_str_dict(srv.get("headers")),
        cwd=str(srv.get("cwd")) if srv.get("cwd") else None,
        timeout=int(srv.get("timeout", 120)),
        max_retry_attempts=int(srv.get("max_retry_attempts", 2)),
        retry_backoff_seconds_base=float(srv.get("retry_backoff_seconds_base", 1.0)),
    )


def parse_mcp_config(config: Dict[str, Any]) -> List[MCPServerConfig]:
    mcp_section = config.get("mcp", {})
    servers = mcp_section.get("servers", [])
    if not servers:
        servers = config.get("mcpServers", {}) or config.get("mcp_servers", {})

    if not servers:
        return []

    result = []

    # Dict format: {name: {type, command, environment, enabled}}
    if isinstance(servers, dict):
        for name, srv in servers.items():
            if not isinstance(srv, dict):
                continue
            cfg = _mcp_server_from_mapping(str(name), srv)
            if cfg:
                result.append(cfg)
    # List format: [{name, command, args, ...}]
    elif isinstance(servers, list):
        for s in servers:
            if not isinstance(s, dict):
                continue
            cfg = _mcp_server_from_mapping(str(s.get("name", "unnamed")), s)
            if cfg:
                result.append(cfg)
    return result