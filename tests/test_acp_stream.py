import asyncio
from pathlib import Path

from ziva_runtime.protocols.acp import ACPServer
from ziva_runtime.runtime import Runtime
from ziva_runtime.shared_types import ChatResult, ToolCallItem


class LoopAdapter:
    def __init__(self):
        self.calls = 0

    async def chat(self, messages, model, system_prompt=None, tools=None):
        self.calls += 1
        if self.calls == 1:
            return ChatResult(
                role="assistant",
                content='[[TOOL_CALL]]{"name":"echo","arguments":{"text":"s"}}[[/TOOL_CALL]]',
                model=model,
                usage={},
                finish_reason="tool_calls",
                tool_calls=[ToolCallItem(id="tc_1", name="echo", arguments={"text": "s"})],
            )
        return ChatResult(role="assistant", content="stream done", model=model, usage={}, finish_reason="stop")


def test_acp_chat_stream_returns_events():
    async def _run():
        root = Path(__file__).resolve().parents[1]
        runtime = Runtime.create(workspace_root=root)
        server = ACPServer(runtime)
        resp = await server.handle(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "chat_stream",
                "params": {"messages": [{"role": "user", "content": "hi"}]},
            }
        )
        result = resp["result"]
        event_types = [e["type"] for e in result["events"]]
        assert "tool_start" in event_types
        assert "tool_end" in event_types
        assert result["final"]["content"] == "stream done"

    asyncio.run(_run())
