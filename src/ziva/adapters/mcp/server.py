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


def _is_retryable(exc: BaseException) -> bool:
    """Transient failures worth retrying with backoff: timeouts, connection
    drops, HTTP 5xx. Cancel-scope cleanup noise is NOT retried — it means the
    connection is gone, not a transient hiccup."""
    try:
        import httpx
        if isinstance(exc, (httpx.ConnectError, httpx.TimeoutException)):
            return True
        if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code >= 500:
            return True
    except ImportError:
        pass
    msg = str(exc).lower()
    return "timeout" in msg or "timed out" in msg


def _map_mcp_error(exc: BaseException, name: str) -> BaseException:
    """Translate low-level transport exceptions into readable messages —
    mirrors agents.mcp's httpx → UserError mapping. Cancel-scope noise becomes
    a clear 'connection closed'. Non-mappable errors pass through unchanged."""
    try:
        import httpx
        if isinstance(exc, httpx.ConnectError):
            return RuntimeError(f"MCP server {name!r}: connection lost — {exc}")
        if isinstance(exc, httpx.TimeoutException):
            return RuntimeError(f"MCP server {name!r}: timed out — {exc}")
        if isinstance(exc, httpx.HTTPStatusError):
            return RuntimeError(f"MCP server {name!r}: HTTP {exc.response.status_code} — {exc}")
    except ImportError:
        pass
    if isinstance(exc, RuntimeError) and "cancel scope" in str(exc):
        return RuntimeError(f"MCP server {name!r}: connection closed")
    return exc


class MCPServer:
    """Minimal MCP client over the `mcp` SDK's ClientSession."""

    def __init__(self, name: str, client_session_timeout_seconds: Optional[float] = 5.0,
                 max_retry_attempts: int = 0, retry_backoff_seconds_base: float = 1.0):
        self._name = name
        self.session: Any = None
        self.exit_stack: AsyncExitStack = AsyncExitStack()
        self._cleanup_lock: asyncio.Lock = asyncio.Lock()
        self.client_session_timeout_seconds = client_session_timeout_seconds
        # Retry transient failures (timeouts / connection drops / HTTP 5xx) with
        # exponential backoff. Mirrors agents.mcp's _run_with_retries.
        self.max_retry_attempts = max(0, int(max_retry_attempts))
        self.retry_backoff_seconds_base = max(0.0, float(retry_backoff_seconds_base))
        # Instance attribute so callers (ziva MCPClient) can override to, e.g.,
        # redirect the stdio subprocess's stderr to devnull.
        self.create_streams: Callable[[], Awaitable[Any]] = self._create_streams  # type: ignore[assignment]

    def _create_streams(self):  # pragma: no cover - overridden by subclasses
        raise NotImplementedError

    @property
    def name(self) -> str:
        return self._name

    def _session_timeout(self) -> Any:
        """read_timeout_seconds for ClientSession, typed for the installed SDK.

        mcp 2.x changed the parameter from ``timedelta`` to plain float
        seconds; handing timedelta to 2.x dies inside the SDK with
        "unsupported operand type(s) for +: 'float' and 'datetime.timedelta'"
        (hit on the Android rootfs, which pip-installs the latest mcp), while
        1.x calls ``.total_seconds()`` on what it's given and dies on float.
        Probe the installed signature once and adapt.
        """
        import inspect

        from mcp import ClientSession

        try:
            param = inspect.signature(ClientSession.__init__).parameters.get(
                "read_timeout_seconds"
            )
            wants_float = param is not None and "float" in str(param.annotation)
        except (TypeError, ValueError):  # pragma: no cover - exotic SDK builds
            wants_float = False
        seconds = self.client_session_timeout_seconds
        if seconds is None:
            return None
        return float(seconds) if wants_float else timedelta(seconds=seconds)

    async def connect(self) -> None:
        from mcp import ClientSession
        try:
            transport = await self.exit_stack.enter_async_context(self.create_streams())
            # stdio/sse return (read, write); streamable-http returns
            # (read, write, get_session_id) — we ignore the trailing callback.
            read, write, *_rest = transport
            session = await self.exit_stack.enter_async_context(
                ClientSession(read, write, read_timeout_seconds=self._session_timeout())
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
        # Drop arguments whose value is an empty string. LLMs sometimes
        # pass "" for optional params (e.g. chrome-devtools new_page's
        # isolatedContext), and some MCP servers treat "" as a meaningful
        # value — chrome-devtools creates an isolated browser context for
        # any non-None isolatedContext including "", which then fails with
        # "Target.createBrowserContext: Method not handled". Treating "" as
        # "not provided" matches the common intent and fixes that.
        if arguments:
            arguments = {k: v for k, v in arguments.items() if v != ""}
        # Retry transient failures with exponential backoff, then map transport
        # errors to readable messages (mirrors agents.mcp _run_with_retries +
        # _raise_user_error_for_http_error).
        last_exc: Optional[BaseException] = None
        for attempt in range(self.max_retry_attempts + 1):
            try:
                return await self.session.call_tool(tool_name, arguments or {})
            except Exception as exc:
                last_exc = exc
                if _is_retryable(exc) and attempt < self.max_retry_attempts:
                    delay = self.retry_backoff_seconds_base * (2 ** attempt)
                    logger.debug("MCP %s call %s failed (attempt %d/%d), retrying in %.1fs: %s",
                                 self._name, tool_name, attempt + 1, self.max_retry_attempts, delay, exc)
                    await asyncio.sleep(delay)
                    continue
                raise _map_mcp_error(exc, self._name) from exc
        assert last_exc is not None
        raise _map_mcp_error(last_exc, self._name)

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
    def __init__(self, name: str, params: dict[str, Any], client_session_timeout_seconds: Optional[float] = 5.0,
                 max_retry_attempts: int = 0, retry_backoff_seconds_base: float = 1.0):
        super().__init__(name=name, client_session_timeout_seconds=client_session_timeout_seconds,
                         max_retry_attempts=max_retry_attempts, retry_backoff_seconds_base=retry_backoff_seconds_base)
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
    def __init__(self, name: str, params: dict[str, Any], client_session_timeout_seconds: Optional[float] = 5.0,
                 max_retry_attempts: int = 0, retry_backoff_seconds_base: float = 1.0):
        super().__init__(name=name, client_session_timeout_seconds=client_session_timeout_seconds,
                         max_retry_attempts=max_retry_attempts, retry_backoff_seconds_base=retry_backoff_seconds_base)
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
    def __init__(self, name: str, params: dict[str, Any], client_session_timeout_seconds: Optional[float] = 5.0,
                 max_retry_attempts: int = 0, retry_backoff_seconds_base: float = 1.0):
        super().__init__(name=name, client_session_timeout_seconds=client_session_timeout_seconds,
                         max_retry_attempts=max_retry_attempts, retry_backoff_seconds_base=retry_backoff_seconds_base)
        self.params = params
        self.create_streams = self._create_streams  # type: ignore[assignment]

    def _create_streams(self):
        from mcp.client.streamable_http import streamable_http_client
        return streamable_http_client(
            url=self.params["url"],
            headers=self.params.get("headers"),
            timeout=self.params.get("timeout", 5),
        )
