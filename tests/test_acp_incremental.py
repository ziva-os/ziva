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
        return ChatResult(role="assistant", content="incremental done", model=model, usage={}, finish_reason="stop")


def test_acp_incremental_open_next():
    async def _run():
        root = Path(__file__).resolve().parents[1]
        runtime = Runtime.create(workspace_root=root, model_adapter=LoopAdapter())
        server = ACPServer(runtime)

        opened = await server.handle(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "chat_stream_open",
                "params": {"messages": [{"role": "user", "content": "hi"}], "token_granularity": "char"},
            }
        )
        stream_id = opened["result"]["stream_id"]
        assert stream_id

        seen_types = []
        for i in range(200):
            nxt = await server.handle(
                {
                    "jsonrpc": "2.0",
                    "id": 2 + i,
                    "method": "chat_stream_next",
                    "params": {"stream_id": stream_id},
                }
            )
            chunk = nxt["result"]["chunk"]
            if chunk:
                seen_types.append(chunk["type"])
            if nxt["result"]["done"]:
                break

        assert seen_types.count("delta") >= 2
        assert "tool_start" in seen_types
        assert "tool_end" in seen_types
        assert "final" in seen_types

    asyncio.run(_run())
