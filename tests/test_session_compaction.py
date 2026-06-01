import asyncio
import json
from pathlib import Path

from ziva_runtime.runtime import Runtime
from ziva_runtime.shared_types import ChatMessage, ChatResult, StreamDelta, ToolCallItem


def test_compaction_estimates_tokens():
    from ziva_runtime.session.compaction import estimate_tokens

    msgs = [
        ChatMessage(role="user", content="Hello world"),
        ChatMessage(role="assistant", content="Hi there!"),
    ]
    tokens = estimate_tokens(msgs)
    assert tokens > 0
    # Each message has ~10 overhead + content estimate
    assert tokens < 100


def test_compaction_detects_overflow():
    from ziva_runtime.session.compaction import is_overflow

    # Small context window to trigger overflow
    msgs = [
        ChatMessage(role="user", content="x" * 1000),
        ChatMessage(role="assistant", content="y" * 1000),
    ]
    assert is_overflow(msgs, 100)
    assert not is_overflow(msgs, 100000)


def test_compaction_prunes_tool_messages():
    from ziva_runtime.session.compaction import prune

    msgs = [
        ChatMessage(role="user", content="first"),
        ChatMessage(role="assistant", content="a1"),
        ChatMessage(role="tool", content="tool output 1", tool_call_id="tc1"),
        ChatMessage(role="assistant", content="a2"),
        ChatMessage(role="user", content="second"),
        ChatMessage(role="assistant", content="a3"),
    ]
    pruned = prune(msgs, keep_last=1)
    # Tool message from earlier turn should be removed
    tool_msgs = [m for m in pruned if m.role == "tool"]
    assert len(tool_msgs) == 0
    # User messages preserved
    user_msgs = [m for m in pruned if m.role == "user"]
    assert len(user_msgs) == 2


def test_compaction_creates_summary():
    from ziva_runtime.session.compaction import compact_messages

    class MockAdapter:
        async def chat(self, messages, model, system_prompt=None, tools=None):
            return ChatResult(role="assistant", content="Mock summary of earlier work.", model=model)

    msgs = [
        ChatMessage(role="user", content="x" * 500),
        ChatMessage(role="assistant", content="y" * 500),
        ChatMessage(role="user", content="z" * 500),
        ChatMessage(role="assistant", content="w" * 500),
    ]

    async def _run():
        compacted = await compact_messages(msgs, context_window=100, model_name="gpt-4", model_adapter=MockAdapter())
        # Should have framed user + assistant + last user + assistant
        assert len(compacted) < len(msgs) + 2  # 2 extra for framing
        # First message should be the framed user message with summary
        assert "compact" in compacted[0].content.lower() or "summary" in compacted[0].content.lower()

    asyncio.run(_run())


def test_doom_loop_detection():
    class DoomLoopAdapter:
        async def chat(self, messages, model, system_prompt=None, tools=None):
            return ChatResult(
                role="assistant",
                content="",
                model=model,
                usage={},
                finish_reason="tool_calls",
                tool_calls=[ToolCallItem(id="tc_1", name="echo", arguments={"text": "same"})],
            )

        async def chat_stream(self, messages, model, system_prompt=None, tools=None):
            yield StreamDelta(
                content="",
                tool_calls=[ToolCallItem(id="tc_1", name="echo", arguments={"text": "same"})],
                finish_reason="tool_calls",
            )

    async def _run():
        root = Path(__file__).resolve().parents[1]
        rt = Runtime.create(
            workspace_root=root,
            model_adapter=DoomLoopAdapter(),
            session_override={"tool": {"max_rounds": 10}},
        )
        result = await rt.chat([ChatMessage(role="user", content="loop")], session_id="doom-1")
        assert result.finish_reason == "doom_loop"
        assert "repeated" in result.content.lower()

    asyncio.run(_run())
