import asyncio
from pathlib import Path

from ziva_runtime.runtime import Runtime
from ziva_runtime.shared_types import ChatMessage, ChatResult, ToolCallItem

class StructuredToolAdapter:
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
                tool_calls=[ToolCallItem(id="tc_1", name="echo", arguments={"text": "abc"})],
            )
        return ChatResult(role="assistant", content="done", model=model, usage={}, finish_reason="stop")

class InvalidToolAdapter:
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
                tool_calls=[ToolCallItem(id="tc_2", name="missing_tool", arguments={"x": 1})],
            )
        return ChatResult(role="assistant", content="fallback", model=model, usage={}, finish_reason="stop")

class InfiniteToolAdapter:
    async def chat(self, messages, model, system_prompt=None, tools=None):
        return ChatResult(
            role="assistant",
            content="",
            model=model,
            usage={},
            finish_reason="tool_calls",
            tool_calls=[ToolCallItem(id="tc_inf", name="echo", arguments={"text": "loop"})],
        )

def test_structured_tool_call_protocol():
    async def _run():
        root = Path(__file__).resolve().parents[1]
        rt = Runtime.create(workspace_root=root)
        result = await rt.chat([ChatMessage(role="user", content="go")], session_id="proto-1")
        assert result.content == "done"

    asyncio.run(_run())

def test_invalid_tool_yields_recoverable_flow():
    async def _run():
        root = Path(__file__).resolve().parents[1]
        rt = Runtime.create(workspace_root=root)
        result = await rt.chat([ChatMessage(role="user", content="go")], session_id="proto-2")
        assert result.content == "fallback"

    asyncio.run(_run())

def test_max_rounds_guardrail():
    async def _run():
        root = Path(__file__).resolve().parents[1]
        rt = Runtime.create(
            workspace_root=root,

            session_override={"tool": {"max_rounds": 2}},
        )
        result = await rt.chat([ChatMessage(role="user", content="loop")], session_id="proto-3")
        assert result.finish_reason == "max_rounds"

    asyncio.run(_run())
