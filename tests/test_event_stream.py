import asyncio
from pathlib import Path

from ziva_runtime.runtime import Runtime
from ziva_runtime.shared_types import ChatMessage, ChatResult, ToolCallItem


class ToolLoopAdapter:
    def __init__(self):
        self.calls = 0

    async def chat(self, messages, model, system_prompt=None, tools=None):
        self.calls += 1
        if self.calls == 1:
            return ChatResult(role="assistant", content='TOOL_CALL echo {"text":"evt"}', model=model, usage={}, finish_reason="tool_calls",
                tool_calls=[ToolCallItem(id="tc_1", name="echo", arguments={"text": "s"})])
        return ChatResult(role="assistant", content="done", model=model, usage={}, finish_reason="stop")


def test_event_stream_contains_tool_and_turn_events():
    async def _run():
        root = Path(__file__).resolve().parents[1]
        rt = Runtime.create(workspace_root=root, model_adapter=ToolLoopAdapter())
        sid = "event-stream-1"
        queue = rt.event_bus.subscribe(sid)
        try:
            await rt.chat([ChatMessage(role="user", content="go")], session_id=sid)
            events = []
            while not queue.empty():
                events.append((await queue.get())["type"])
            assert "turn_start" in events
            assert "tool_start" in events
            assert "tool_end" in events
            assert "turn_end" in events
        finally:
            rt.event_bus.unsubscribe(sid, queue)

    asyncio.run(_run())
