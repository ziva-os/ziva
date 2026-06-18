import asyncio
from pathlib import Path

from ziva_runtime.runtime import Runtime
from ziva_runtime.shared_types import ChatMessage, ChatResult, StreamDelta


class DelayedAdapter:
    """Adapter that yields slowly so we can interleave other work."""

    def __init__(self, name="default"):
        self.name = name
        self._cancelled = False

    async def chat(self, messages, model, system_prompt=None, tools=None):
        return ChatResult(role="assistant", content="ok", model=model, usage={}, finish_reason="stop")

    async def chat_stream(self, messages, model, system_prompt=None, tools=None, thinking_config=None):
        for i in range(5):
            if self._cancelled:
                raise asyncio.CancelledError("adapter cancelled")
            await asyncio.sleep(0.05)
            yield StreamDelta(content=f"chunk-{i}")
        yield StreamDelta(finish_reason="stop", usage={})


def test_switch_model_does_not_kill_background_session():
    async def _run():
        root = Path(__file__).resolve().parents[1]
        adapter_a = DelayedAdapter("A")
        rt = Runtime.create(workspace_root=root)

        # Monkey-patch _create_adapter so it never fails and returns our test adapter
        from ziva_runtime import runtime as runtime_module
        original_create_adapter = runtime_module._create_adapter
        runtime_module._create_adapter = lambda config: adapter_a

        # Start session A
        task_a = asyncio.create_task(
            rt.chat([ChatMessage(role="user", content="hello A")], session_id="sid-a")
        )
        await asyncio.sleep(0.1)  # Let A start streaming

        # Switch model (simulate what frontend does)
        adapter_b = DelayedAdapter("B")
        rt.config["model"] = {"name": "Kimi-K2.6"}
        runtime_module._create_adapter = lambda config: adapter_b

        # Start session B
        task_b = asyncio.create_task(
            rt.chat([ChatMessage(role="user", content="hello B")], session_id="sid-b")
        )

        # Wait for both to finish
        result_a = await task_a
        result_b = await task_b

        print(f"A finish_reason: {result_a.finish_reason}")
        print(f"B finish_reason: {result_b.finish_reason}")

        assert result_a.finish_reason == "stop", f"Session A was killed: {result_a.finish_reason}"
        assert result_b.finish_reason == "stop"

    asyncio.run(_run())
