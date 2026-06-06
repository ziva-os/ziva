from __future__ import annotations

import hashlib
import json
from typing import Any, Dict

from ziva_runtime.shared_types import RuntimeContext, ToolResult

_THRESHOLD = 3


class DoomLoopHook:
    event_name: str = "after_tool"
    matcher: str | None = None

    def __init__(self) -> None:
        self._history: dict[str, dict[tuple[str, str], int]] = {}

    def _args_hash(self, arguments: dict) -> str:
        return hashlib.md5(json.dumps(arguments, sort_keys=True).encode()).hexdigest()[:12]

    async def handle(self, payload: Dict[str, Any], ctx: RuntimeContext) -> Dict[str, Any]:
        tool_name = payload.get("tool", "")
        arguments = payload.get("arguments", {})
        session_id = ctx.session_id

        if session_id not in self._history:
            self._history[session_id] = {}

        key = (tool_name, self._args_hash(arguments))
        counts = self._history[session_id]
        counts[key] = counts.get(key, 0) + 1
        count = counts[key]

        if count >= _THRESHOLD:
            output = payload.get("output")
            warning = (
                f"\n\n<reminder>'{tool_name}' has been called {count} times with the same arguments. "
                f"Check prior results or try a different approach.</reminder>"
            )
            if isinstance(output, ToolResult):
                output.text += warning

        return payload

    def clear_session(self, session_id: str) -> None:
        self._history.pop(session_id, None)
