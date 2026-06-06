from __future__ import annotations

import hashlib
import json
from typing import Any, Dict

from ziva_runtime.shared_types import RuntimeContext, ToolResult

_THRESHOLD = 3


class DoomLoopHook:
    event_name: str = "after_tool"
    matcher: str | None = None

    def _args_hash(self, arguments: dict) -> str:
        return hashlib.md5(json.dumps(arguments, sort_keys=True).encode()).hexdigest()[:12]

    async def handle(self, payload: Dict[str, Any], ctx: RuntimeContext) -> Dict[str, Any]:
        runtime = ctx.metadata.get("_runtime")
        if not runtime:
            return payload

        tool_name = payload.get("tool", "")
        arguments = payload.get("arguments", {})

        session = runtime._get_session(ctx.session_id)
        state = session.hook_states.setdefault("doom_loop", {})
        counts: dict = state.setdefault("counts", {})

        key = (tool_name, self._args_hash(arguments))
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
