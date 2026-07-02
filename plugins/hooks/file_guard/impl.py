from __future__ import annotations

from typing import Any, Dict

from ziva.shared_types import RuntimeContext

_HINT = (
    "\nThe user has attached image(s) inline in this message. "
    "You can already see them directly — describe and reason about "
    "the content without calling additional tools. External image "
    "tools should only be used when you have a concrete file path "
    "or URL that is not already in the conversation."
)


def _has_image_url(messages: list[dict]) -> bool:
    for msg in messages:
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if isinstance(block, dict) and block.get("type") == "image_url":
                return True
    return False


class FileGuardHook:
    event_name: str = "before_turn"
    matcher: str | None = None

    async def handle(self, payload: Dict[str, Any], _ctx: RuntimeContext) -> Dict[str, Any]:
        messages = payload.get("messages", [])
        if not _has_image_url(messages):
            return payload

        for msg in messages:
            content = msg.get("content")
            if not isinstance(content, list):
                continue
            if any(isinstance(b, dict) and b.get("type") == "image_url" for b in content):
                content.append({"type": "text", "text": _HINT})

        return payload
