from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Dict, List, Literal, Optional

ApprovalPolicy = Literal["suggest", "auto-edit", "full-auto"]


@dataclass
class RuntimeContext:
    session_id: str
    config: Dict[str, Any]
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ChatMessage:
    role: str
    content: str | list
    tool_call_id: Optional[str] = None
    name: Optional[str] = None
    tool_calls: List[ToolCallItem] = field(default_factory=list)
    _compaction_summary: bool = False
    # True for the original messages that have been folded into a summary.
    # Kept on disk so the UI can expand the collapse bar to show them, but
    # filtered out of the LLM context (the summary is the new starting
    # point, no "recent tail" is preserved — codex CLI / claude code style).
    _compacted: bool = False


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
