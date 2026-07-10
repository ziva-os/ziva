from __future__ import annotations

import asyncio
import os
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Literal, Optional

ApprovalPolicy = Literal["suggest", "auto-edit", "full-auto"]


class MCPConnectStatus(Enum):
    """Per-session MCP connection lifecycle.

    Splits the old boolean mcp_connected into four states so the tool
    wrapper can give precise error messages and the connector can decide
    whether to retry on the next turn:

      DISCONNECTED — never tried (initial state, or reset by switch_workspace /
                     save_config_json mcp edit)
      CONNECTING   — _connect_mcp_if_needed is in flight
      CONNECTED    — MCPClient.connect_all succeeded; session.mcp_client is set
      NO_CONFIG    — workspace has no mcp.servers; session.mcp_client stays None
                     (will NOT be retried on subsequent turns)
      FAILED       — MCPClient.connect_all raised; session.mcp_client stays None.
                     Will be retried on the next turn (different from the old
                     behavior where the boolean flagged "connected" on failure
                     and the session was stuck forever).
    """
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    NO_CONFIG = "no_config"
    FAILED = "failed"


@dataclass
class RuntimeContext:
    session_id: str
    config: Dict[str, Any]
    metadata: Dict[str, Any] = field(default_factory=dict)


def resolve_workspace_cwd(ctx) -> str:
    """Resolve the effective working directory for a tool call.

    Tools (shell/edit_file/glob/list/grep/read_file/write_file) must run in the
    session's workspace, not the backend process's ``os.getcwd()`` (which is
    ``$HOME`` in the PyInstaller bundle or the repo root in dev). Each turn
    snapshots ``runtime.workspace_root`` into ``ctx.metadata["_workspace_root"]``
    at turn start (see ``runtime.chat`` / ``chat_streaming``) so the directory is
    stable for the whole turn — switching workspaces mid-turn doesn't relocate
    already-running tools.

    Priority:
      1. ``ctx.metadata["_workspace_root"]`` — turn-start snapshot (preferred).
      2. ``ctx.metadata["_runtime"].workspace_root`` — live runtime value.
      3. ``os.getcwd()`` — last-resort fallback (CLI/tests without a runtime).
    """
    if ctx is not None:
        meta = getattr(ctx, "metadata", None) or {}
        ws = meta.get("_workspace_root")
        if ws:
            return str(ws)
        runtime = meta.get("_runtime")
        wr = getattr(runtime, "workspace_root", None)
        if wr is not None:
            return str(wr)
    return os.getcwd()


@dataclass
class ChatMessage:
    role: str
    content: str | list
    tool_call_id: Optional[str] = None
    name: Optional[str] = None
    tool_calls: List[ToolCallItem] = field(default_factory=list)
    reasoning_content: Optional[str] = None
    reasoning_signature: Optional[str] = None
    _compaction_summary: bool = False
    _hidden: bool = False


@dataclass
class ToolCallItem:
    id: str
    name: str
    arguments: Dict[str, Any]


@dataclass
class ChatResult:
    role: str
    content: str
    model: Optional[str] = None
    usage: Optional[Dict[str, int]] = None
    finish_reason: Optional[str] = None
    tool_calls: List[ToolCallItem] = field(default_factory=list)
    reasoning_content: Optional[str] = None
    reasoning_signature: Optional[str] = None


@dataclass
class ToolCall:
    name: str
    arguments: Dict[str, Any]


@dataclass
class Event:
    type: str
    payload: Dict[str, Any]


@dataclass
class ApprovalRequest:
    request_id: str
    tool_name: str
    arguments: Dict[str, Any]
    risk_level: str = "medium"


@dataclass
class ApprovalDecision:
    request_id: str
    approved: bool
    reason: str | None = None


@dataclass
class CancellationToken:
    _cancelled: bool = field(default=False, init=False, repr=False)
    _event: asyncio.Event = field(default_factory=asyncio.Event, init=False, repr=False)

    def cancel(self) -> None:
        self._cancelled = True
        self._event.set()

    @property
    def is_cancelled(self) -> bool:
        return self._cancelled

    async def wait(self) -> None:
        await self._event.wait()


@dataclass
class StreamDelta:
    content: str = ""
    finish_reason: Optional[str] = None
    tool_calls: List[ToolCallItem] = field(default_factory=list)
    usage: Optional[Dict[str, int]] = None
    reasoning_signature: Optional[str] = None
    reasoning_content: Optional[str] = None


@dataclass
class ToolResult:
    text: str
    images: list[str] = field(default_factory=list)
    error: bool = False
    metadata: dict = field(default_factory=dict)


@dataclass
class SessionState:
    project_id: str | None = None
    history: list[ChatMessage] = field(default_factory=list)
    event_seq: int = 0
    pending_questions: dict[str, asyncio.Future] = field(default_factory=dict)
    hook_states: dict[str, Any] = field(default_factory=dict)
    mcp_client: Any = None
    mcp_status: MCPConnectStatus = MCPConnectStatus.DISCONNECTED
    mcp_connected_event: asyncio.Event = field(default_factory=asyncio.Event, init=False, repr=False)
    cancel_token: CancellationToken | None = None
    turn_task: asyncio.Task | None = None
    event_queue: asyncio.Queue | None = None
    event_history: deque = field(default_factory=lambda: deque(maxlen=100))
    model_name: str | None = None
    # The workspace directory this session was created in. Tools resolve
    # cwd / relative paths against THIS, not runtime.workspace_root (which
    # tracks the currently-focused workspace and would be wrong if the user
    # switches workspaces and then runs a session created elsewhere).
    workspace_root: str | None = None
    load_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    plan: list[dict] | None = None
    plan_last_updated: float = 0.0
    # True for the hidden backing session of a scheduled automation. The
    # runtime skips broadcasting intermediate chat events (delta,
    # tool_start/tool_end, model_response, etc.) for these sessions so
    # they stay in the background and never leak into another session's
    # chat UI — only the final `automation_run` summary event reaches
    # the Automations panel. See runtime._emit().
    is_automation: bool = False
    plan_tool_calls_since_update: int = 0
