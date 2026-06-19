"""Minimal local MCP client wrappers — a thin replacement for the client side
of `openai-agents`' `agents.mcp`.

We only need: connect to an MCP server (stdio/sse/streamable-http), list tools,
call a tool, clean up. `openai-agents` wraps the `mcp` SDK with ~2800 lines of
tool caching, tool filtering, retry/serialization, httpx error mapping, and
SDK (RunContext/Agent/MCPTool) integration — none of which ziva uses. This
module keeps a ~130-line shim directly on the `mcp` SDK so ziva doesn't have to
depend on openai-agents (which drags in openai/httpx/starlette/uvicorn/...).

API surface mirrors the subset of `agents.mcp` that ziva's MCPClient relies on:
    server = MCPServerStdio(name=..., params={...}, client_session_timeout_seconds=...)
    await server.connect()
    tools = await server.list_tools()
    result = await server.call_tool(name, args)
    await server.cleanup()
`MCPServerStdio.params` (a `StdioServerParameters`) is exposed because ziva's
client reassigns `server.create_streams` to silence the subprocess's stderr.
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import AsyncExitStack
from datetime import timedelta
from typing import Any, Awaitable, Callable, Optional

logger = logging.getLogger(__name__)


def _swallow_cleanup_noise(name: str, exc: BaseException) -> bool:
    """Return True if `exc` is the MCP SDK's benign cleanup noise that should
    not propagate. The stdio transport's async-generator cleanup occasionally
    surfaces 'cancel scope in different task' / ExceptionGroup chatter."""
    if isinstance(exc, asyncio.CancelledError):
        return False  # must preserve cancellation
    if isinstance(exc, RuntimeError) and "cancel scope" in str(exc):
        return True
    return False


class MCPServer:
    """Minimal MCP client over the `mcp` SDK's ClientSession."""

    def __init__(self, name: str, client_session_timeout_seconds: Optional[float] = 5.0):
        self._name = name
        self.session: Any = None
        self.exit_stack: AsyncExitStack = AsyncExitStack()
        self._cleanup_lock: asyncio.Lock = asyncio.Lock()
        self.client_session_timeout_seconds = client_session_timeout_seconds
        # Instance attribute so callers (ziva MCPClient) can override to, e.g.,
        # redirect the stdio subprocess's stderr to devnull.
        self.create_streams: Callable[[], Awaitable[Any]] = self._create_streams  # type: ignore[assignment]

    def _create_streams(self):  # pragma: no cover - overridden by subclasses
        raise NotImplementedError

    @property
    def name(self) -> str:
        return self._name

    async def connect(self) -> None:
        from mcp import ClientSession
        try:
            transport = await self.exit_stack.enter_async_context(self.create_streams())
            # stdio/sse return (read, write); streamable-http returns
            # (read, write, get_session_id) — we ignore the trailing callback.
            read, write, *_rest = transport
            timeout = (
                timedelta(seconds=self.client_session_timeout_seconds)
                if self.client_session_timeout_seconds is not None
                else None
            )
            session = await self.exit_stack.enter_async_context(
                ClientSession(read, write, read_timeout_seconds=timeout)
            )
            await session.initialize()
            self.session = session
        except Exception:
            # Connection failed — tear down the partial stack so we don't leak
            # a half-opened subprocess/transport, swallowing the SDK's benign
            # cleanup chatter so the real connect error reaches the caller.
            try:
                await self.cleanup()
            except BaseException as cleanup_err:
                if not _swallow_cleanup_noise(self._name, cleanup_err):
                    logger.debug("MCP %s cleanup-after-failed-connect: %s", self._name, cleanup_err)
            raise

    async def list_tools(self) -> list[Any]:
        if self.session is None:
            raise RuntimeError(f"MCP server {self._name!r} not connected")
        result = await self.session.list_tools()
        return list(result.tools)

    async def call_tool(self, tool_name: str, arguments: Optional[dict[str, Any]]) -> Any:
        if self.session is None:
            raise RuntimeError(f"MCP server {self._name!r} not connected")
        return await self.session.call_tool(tool_name, arguments or {})

    async def cleanup(self) -> None:
        async with self._cleanup_lock:
            try:
                await self.exit_stack.aclose()
            except asyncio.CancelledError:
                raise
            except BaseException as exc:
                # Background transport tasks can surface an ExceptionGroup;
                # log the real inner errors but don't mask the caller's flow.
                inner = getattr(exc, "exceptions", None) or (exc,)
                for e in inner:
                    if _swallow_cleanup_noise(self._name, e):
                        logger.debug("MCP %s cancel-scope cleanup noise ignored", self._name)
                    elif not isinstance(e, asyncio.CancelledError):
                        logger.debug("MCP %s cleanup error: %s", self._name, e)


class MCPServerStdio(MCPServer):
    def __init__(self, name: str, params: dict[str, Any], client_session_timeout_seconds: Optional[float] = 5.0):
        super().__init__(name=name, client_session_timeout_seconds=client_session_timeout_seconds)
        from mcp import StdioServerParameters
        self.params = StdioServerParameters(
            command=params["command"],
            args=list(params.get("args", []) or []),
            env=params.get("env"),
            cwd=params.get("cwd"),
            encoding=params.get("encoding", "utf-8"),
            encoding_error_handler=params.get("encoding_error_handler", "strict"),
        )
        self.create_streams = self._create_streams  # type: ignore[assignment]

    def _create_streams(self):
        from mcp import stdio_client
        return stdio_client(self.params)


class MCPServerSse(MCPServer):
    def __init__(self, name: str, params: dict[str, Any], client_session_timeout_seconds: Optional[float] = 5.0):
        super().__init__(name=name, client_session_timeout_seconds=client_session_timeout_seconds)
        self.params = params
        self.create_streams = self._create_streams  # type: ignore[assignment]

    def _create_streams(self):
        from mcp.client.sse import sse_client
        return sse_client(
            url=self.params["url"],
            headers=self.params.get("headers"),
            timeout=self.params.get("timeout", 5),
            sse_read_timeout=self.params.get("sse_read_timeout", 60 * 5),
        )


class MCPServerStreamableHttp(MCPServer):
    def __init__(self, name: str, params: dict[str, Any], client_session_timeout_seconds: Optional[float] = 5.0):
        super().__init__(name=name, client_session_timeout_seconds=client_session_timeout_seconds)
        self.params = params
        self.create_streams = self._create_streams  # type: ignore[assignment]

    def _create_streams(self):
        from mcp.client.streamable_http import streamable_http_client
        return streamable_http_client(
            url=self.params["url"],
            headers=self.params.get("headers"),
            timeout=self.params.get("timeout", 5),
        )
