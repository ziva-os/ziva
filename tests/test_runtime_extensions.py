import asyncio
from pathlib import Path

from ziva_runtime.runtime import Runtime
from ziva_runtime.shared_types import ChatResult, ChatMessage


class FakeAdapter:
    async def chat(self, messages, model, system_prompt=None, tools=None):
        joined = " || ".join(m.content for m in messages)
        return ChatResult(role="assistant", content=joined, model=model, usage={}, finish_reason="stop")


def test_runtime_memory_integration():
    async def _run():
        root = Path(__file__).resolve().parents[1]
        rt = Runtime.create(workspace_root=root, model_adapter=FakeAdapter())
        result = await rt.chat([ChatMessage(role="user", content="hello")], session_id="s1")
        assert "hello" in result.content

        mem = rt.registry.list_kind("memory")[0].instance
        summary = await mem.summarize(None)
        assert summary["count"] >= 1

    asyncio.run(_run())
