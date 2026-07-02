import asyncio
from pathlib import Path

from ziva.runtime import Runtime
from ziva.shared_types import ChatMessage, ChatResult


class ToolLoopAdapter:
    def __init__(self):
        self.calls = 0

    async def chat(self, messages, model, system_prompt=None, tools=None):
        self.calls += 1
        if self.calls == 1:
            return ChatResult(role="assistant", content='[[TOOL_CALL]]{"name":"echo","arguments":{"text":"x"}}[[/TOOL_CALL]]', model=model, usage={}, finish_reason="tool")
        return ChatResult(role="assistant", content="ok", model=model, usage={}, finish_reason="stop")


def test_event_metadata_fields_present():
    async def _run():
        root = Path(__file__).resolve().parents[1]
        rt = Runtime.create(workspace_root=root)
        sid, _, events = await rt.chat_with_events([ChatMessage(role="user", content="hello")], session_id="meta-1")
        assert sid == "meta-1"
        assert events
        seqs = [e["seq"] for e in events]
        assert seqs == sorted(seqs)
        assert all(isinstance(e.get("ts"), int) for e in events)
        round_complete = [e for e in events if e["type"] == "round_complete"]
        assert round_complete and isinstance(round_complete[0].get("latency_ms"), int)

    asyncio.run(_run())
