from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Dict

from ziva_runtime.shared_types import RuntimeContext, ToolResult

_TOOL_LIMITS: dict[str, int] = {
    "shell": 30_000,
    "read_file": 20_000,
    "grep": 20_000,
    "web_fetch": 50_000,
}
_DEFAULT_LIMIT = 20_000
_PREVIEW_CHARS = 2_048


class TruncationHook:
    event_name: str = "after_tool"
    matcher: str | None = None

    async def handle(self, payload: Dict[str, Any], ctx: RuntimeContext) -> Dict[str, Any]:
        output = payload.get("output")
        tool_name = payload.get("tool", "")

        if isinstance(output, ToolResult):
            if output.error or output.images:
                return payload
            text = output.text
        else:
            return payload

        limit = _TOOL_LIMITS.get(tool_name, _DEFAULT_LIMIT)
        if len(text) <= limit:
            return payload

        workspace = getattr(ctx.metadata.get("_runtime"), "workspace_root", None)
        tmp_dir = Path(workspace) / "tmp" if workspace else Path("tmp")
        tmp_dir.mkdir(exist_ok=True)
        file_name = f"tool_output_{tool_name}_{int(time.time() * 1000)}.txt"
        file_path = tmp_dir / file_name

        try:
            file_path.write_text(text, encoding="utf-8")
        except Exception:
            pass

        preview = text[:_PREVIEW_CHARS]
        output.text = (
            f"Output too large ({len(text)} chars). Full output saved to: {file_path}\n\n"
            f"Preview:\n{preview}\n\n"
            f"Use read_file to view the full output."
        )
        output.metadata["_truncated"] = True
        output.metadata["_full_output_path"] = str(file_path)

        payload["output"] = output
        return payload
