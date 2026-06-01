import asyncio
from pathlib import Path

from ziva_runtime.runtime import Runtime
from ziva_runtime.shared_types import ChatMessage, ChatResult


class SimpleAdapter:
    async def chat(self, messages, model, system_prompt=None, tools=None):
        return ChatResult(role="assistant", content=messages[-1].content, model=model, usage={}, finish_reason="stop")


def test_multi_session_event_isolation():
    async def _run():
        root = Path(__file__).resolve().parents[1]
        rt = Runtime.create(workspace_root=root, model_adapter=SimpleAdapter())

        qa = rt.event_bus.subscribe("sid-a")
        qb = rt.event_bus.subscribe("sid-b")
        try:
            await asyncio.gather(
                rt.chat([ChatMessage(role="user", content="a1")], session_id="sid-a"),
                rt.chat([ChatMessage(role="user", content="b1")], session_id="sid-b"),
            )

            events_a = []
            while not qa.empty():
                events_a.append((await qa.get())["session_id"])
            events_b = []
            while not qb.empty():
                events_b.append((await qb.get())["session_id"])

            assert events_a and all(e == "sid-a" for e in events_a)
            assert events_b and all(e == "sid-b" for e in events_b)
        finally:
            rt.event_bus.unsubscribe("sid-a", qa)
            rt.event_bus.unsubscribe("sid-b", qb)

    asyncio.run(_run())
