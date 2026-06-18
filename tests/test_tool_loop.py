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
            return ChatResult(
                role="assistant",
                content="",
                model=model,
                usage={},
                finish_reason="tool_calls",
                tool_calls=[ToolCallItem(id="tc_1", name="echo", arguments={"text": "world"})],
            )
        return ChatResult(role="assistant", content="final answer", model=model, usage={}, finish_reason="stop")


def test_model_tool_model_loop():
    async def _run():
        root = Path(__file__).resolve().parents[1]
        adapter = ToolLoopAdapter()
        rt = Runtime.create(workspace_root=root)
        result = await rt.chat([ChatMessage(role="user", content="say hi")], session_id="loop-1")
        assert result.content == "final answer"
        assert adapter.calls == 2

    asyncio.run(_run())
