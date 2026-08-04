from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Dict

from ziva.capabilities.interfaces import BaseHook
from ziva.shared_types import RuntimeContext, ToolResult

_TOOL_LIMITS: dict[str, int] = {
    "shell": 30_000,
    "grep": 20_000,
    "web_fetch": 50_000,
    # read_file self-limits by LINES (2000), not bytes — a binary / long-line
    # file (e.g. a PDF read as text) can be huge in bytes/tokens while still
    # under the line limit (one such read hit 293KB → 203K tokens). Cap it by
    # size: over ~198KB, save the full output to a file and return a head/tail
    # preview instead of dumping it into the context.
    "read_file": 198 * 1024,
}
_DEFAULT_LIMIT = 20_000
_UNLIMITED: set[str] = set()
_PREVIEW_HEAD_LINES = 50
_PREVIEW_TAIL_LINES = 20
_PREVIEW_LINE_WIDTH = 200


class TruncationHook(BaseHook):
    event_name: str = "after_tool"

    async def handle(self, payload: Dict[str, Any], ctx: RuntimeContext) -> Dict[str, Any]:
        output = payload.get("output")
        tool_name = payload.get("tool", "")

        if isinstance(output, ToolResult):
            # Structured / multimodal tool outputs (images, audio, resources)
            # should not be flattened into a single text file. Keep them intact
            # so the model can see the media directly.
            if (
                output.error
                or output.images
                or output.metadata.get("audio")
                or output.metadata.get("resources")
            ):
                return payload
            text = output.text
        else:
            return payload

        if tool_name in _UNLIMITED:
            return payload

        limit = _TOOL_LIMITS.get(tool_name, _DEFAULT_LIMIT)
        if len(text) <= limit:
            return payload

        lines = text.split("\n")
        total_lines = len(lines)

        workspace = getattr(ctx.metadata.get("_runtime"), "workspace_root", None)
        tmp_dir = Path(workspace) / "tmp" if workspace else Path("tmp")
        tmp_dir.mkdir(exist_ok=True)
        file_name = f"tool_output_{tool_name}_{int(time.time() * 1000)}.txt"
        file_path = tmp_dir / file_name

        try:
            file_path.write_text(text, encoding="utf-8")
        except Exception:
            pass

        # Build preview with line numbers (head + tail)
        def _trim(line: str) -> str:
            return line if len(line) <= _PREVIEW_LINE_WIDTH else line[:_PREVIEW_LINE_WIDTH] + "..."

        if total_lines <= _PREVIEW_HEAD_LINES + _PREVIEW_TAIL_LINES:
            preview_lines = [f"{i + 1:6d} | {_trim(lines[i])}" for i in range(total_lines)]
        else:
            head_end = _PREVIEW_HEAD_LINES
            tail_start = total_lines - _PREVIEW_TAIL_LINES + 1
            preview_lines = [f"{i + 1:6d} | {_trim(lines[i])}" for i in range(head_end)]
            omitted = tail_start - head_end
            preview_lines.append(f"       ... {omitted} lines omitted (lines {head_end + 1}–{tail_start - 1}) ...")
            for i in range(tail_start - 1, total_lines):
                preview_lines.append(f"{i + 1:6d} | {_trim(lines[i])}")

        preview = "\n".join(preview_lines)

        output.text = (
            f"Output too large ({len(text)} chars, {total_lines} lines). "
            f"Full output saved to: {file_path}\n\n"
            f"{preview}\n\n"
            f"Use read_file(\"{file_path}\") to view full content, "
            f"or read_file with offset/limit for specific line ranges."
        )
        output.metadata["_truncated"] = True
        output.metadata["_full_output_path"] = str(file_path)
        output.metadata["_total_lines"] = total_lines

        payload["output"] = output
        return payload
