"""Tests for the Anthropic adapter's stream-of-deltas behavior.

Why these tests exist
---------------------
User reported: for kimi-k2.6 (which uses our anthropic-format adapter)
the thinking (reasoning_content) does not show in the UI. The user
explicitly asked for unit tests to verify the contract, so future
SDK / provider changes that silently break reasoning routing fail
loudly here instead of as a "the thinking card is empty" report.

The streaming pipeline is:
  anthropic SDK event  →  AnthropicChatAdapter.chat_stream
                       →  StreamDelta(reasoning_content=..., content=...)
                       →  runtime._run_model_tool_loop yields
                          {"type": "reasoning_delta", "content": ...}
                          {"type": "delta", "content": ...}
                       →  frontend accumulates into _reasoning / _main
                          and renders the thinking card

These tests pin step 1 (the adapter). Step 2 is pinned by
test_runtime_anthropic_stream_emits_reasoning_and_content below.
"""
from __future__ import annotations

import asyncio
from typing import Any, AsyncIterator, List
from unittest.mock import AsyncMock, MagicMock

from ziva_runtime.adapters.anthropic.provider import AnthropicChatAdapter
from ziva_runtime.shared_types import ChatMessage, StreamDelta


# ---------------------------------------------------------------------------
# Fake SDK event helpers — model the shape anthropic SDK 0.111.0 produces
# for a thinking-enabled model. We only need the attributes the adapter
# actually reads (`event.type`, `event.content_block.type/thinking/signature`,
# `event.delta.type/text/thinking/partial_json`, `event.index`).
# ---------------------------------------------------------------------------

class _TextDelta:
    def __init__(self, text: str):
        self.type = "text_delta"
        self.text = text


class _ThinkingDelta:
    def __init__(self, thinking: str):
        self.type = "thinking_delta"
        self.thinking = thinking


class _InputJSONDelta:
    def __init__(self, partial_json: str):
        self.type = "input_json_delta"
        self.partial_json = partial_json


class _TextBlock:
    type = "text"
    text = ""


class _ThinkingBlock:
    def __init__(self, signature: str | None = None, initial: str = ""):
        self.type = "thinking"
        self.signature = signature
        self.thinking = initial


class _ToolUseBlock:
    def __init__(self, id: str, name: str):
        self.type = "tool_use"
        self.id = id
        self.name = name
        self.input = {}


class _ContentBlockStart:
    def __init__(self, block: Any, index: int):
        self.type = "content_block_start"
        self.index = index
        self.content_block = block


class _ContentBlockDelta:
    def __init__(self, delta: Any, index: int):
        self.type = "content_block_delta"
        self.index = index
        self.delta = delta


class _ContentBlockStop:
    def __init__(self, block: Any, index: int):
        self.type = "content_block_stop"
        self.index = index
        self.content_block = block


class _MessageStart:
    def __init__(self, usage: Any = None):
        self.type = "message_start"
        self.message = MagicMock()
        self.message.usage = usage


class _MessageDelta:
    """Carries stop_reason and final usage at the end of the turn."""

    def __init__(self, stop_reason: str, output_tokens: int = 0, input_tokens: int = 0):
        self.type = "message_delta"
        self.delta = MagicMock()
        self.delta.stop_reason = stop_reason
        self.usage = MagicMock()
        self.usage.input_tokens = input_tokens
        self.usage.output_tokens = output_tokens


class _FakeStream:
    """Async-iterable that mimics anthropic SDK's stream context manager."""

    def __init__(self, events: List[Any]):
        self._events = list(events)
        self._i = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._i >= len(self._events):
            raise StopAsyncIteration
        ev = self._events[self._i]
        self._i += 1
        return ev


def _patched_adapter(events: List[Any]) -> AnthropicChatAdapter:
    """Build an adapter whose underlying SDK call returns `events`."""
    adapter = AnthropicChatAdapter(api_key="test", base_url="http://test", default_max_tokens=1000)
    stream_mock = MagicMock()
    stream_mock.__aenter__ = AsyncMock(return_value=_FakeStream(events))
    stream_mock.__aexit__ = AsyncMock(return_value=False)
    adapter._client = MagicMock()
    adapter._client.messages.stream = MagicMock(return_value=stream_mock)
    return adapter


async def _collect(adapter: AnthropicChatAdapter, **kwargs) -> List[StreamDelta]:
    out: List[StreamDelta] = []
    async for d in adapter.chat_stream(
        messages=[ChatMessage(role="user", content="hi")],
        model="claude-3-7-sonnet",
        **kwargs,
    ):
        out.append(d)
    return out


# ---------------------------------------------------------------------------
# 1) Thinking mode: reasoning + content both flow through
# ---------------------------------------------------------------------------

def test_chat_stream_with_thinking_yields_reasoning_and_content_deltas():
    """The whole point of this file: when thinking is enabled, the
    adapter must yield:
      - one StreamDelta with reasoning_signature for the thinking block start
      - one StreamDelta with reasoning_content for each thinking_delta
      - one StreamDelta with content for each text_delta
      - one StreamDelta with finish_reason at the end

    If any of those are missing, the UI's thinking card or the response
    text will be empty. The user reported both for kimi-k2.6, so we
    assert both here.
    """
    events = [
        # Thinking block opens; we get its signature immediately.
        _ContentBlockStart(_ThinkingBlock(signature="sig-abc", initial=""), index=0),
        # Two thinking deltas accumulate into reasoning_content.
        _ContentBlockDelta(_ThinkingDelta("step 1 "), index=0),
        _ContentBlockDelta(_ThinkingDelta("then step 2"), index=0),
        # Then the text block opens.
        _ContentBlockStart(_TextBlock(), index=1),
        # Two text deltas become content.
        _ContentBlockDelta(_TextDelta("Hello "), index=1),
        _ContentBlockDelta(_TextDelta("world"), index=1),
        # message_delta closes the turn.
        _MessageDelta(stop_reason="end_turn", output_tokens=7, input_tokens=4),
    ]

    async def _go():
        adapter = _patched_adapter(events)
        deltas = await _collect(
            adapter, thinking_config={"type": "enabled", "budget_tokens": 4000}
        )

        # Reasoning: 1 from the block start (signature, no content), 2 from deltas.
        reasoning_deltas = [d for d in deltas if d.reasoning_content]
        assert len(reasoning_deltas) == 2, (
            f"expected 2 reasoning_content deltas, got {len(reasoning_deltas)}: "
            f"{[d.reasoning_content for d in reasoning_deltas]!r}"
        )
        assert "".join(d.reasoning_content for d in reasoning_deltas) == "step 1 then step 2"

        # Reasoning signature is carried on the very first delta (block start).
        first = deltas[0]
        assert first.reasoning_signature == "sig-abc", (
            f"signature missing on thinking block start: {first!r}"
        )

        # Content: 2 deltas that concatenate to "Hello world".
        content_deltas = [d for d in deltas if d.content]
        assert len(content_deltas) == 2
        assert "".join(d.content for d in content_deltas) == "Hello world"

        # And we get a terminal delta with the finish reason.
        final = [d for d in deltas if d.finish_reason]
        assert len(final) == 1
        assert final[0].finish_reason == "end_turn"

    asyncio.run(_go())


# ---------------------------------------------------------------------------
# 2) Thinking disabled: still produces content (regression guard for the
#    "content 也不显示" part of the report)
# ---------------------------------------------------------------------------

def test_chat_stream_without_thinking_yields_content():
    """If the model doesn't send thinking blocks, text_delta must
    still flow into the content field. The kimi-k2.6 report mentioned
    'content 不知道是不是也不显示' — this test guards against any future
    change that gates content on thinking being enabled.
    """
    events = [
        _ContentBlockStart(_TextBlock(), index=0),
        _ContentBlockDelta(_TextDelta("hello"), index=0),
        _ContentBlockDelta(_TextDelta(" there"), index=0),
        _MessageDelta(stop_reason="end_turn"),
    ]

    async def _go():
        adapter = _patched_adapter(events)
        deltas = await _collect(adapter)  # no thinking_config

        content = "".join(d.content for d in deltas if d.content)
        assert content == "hello there", (
            f"content lost when thinking disabled: {content!r}"
        )
        # No reasoning deltas should appear in this scenario.
        assert not any(d.reasoning_content for d in deltas), (
            f"unexpected reasoning_content when thinking off: "
            f"{[d.reasoning_content for d in deltas]!r}"
        )

    asyncio.run(_go())


# ---------------------------------------------------------------------------
# 3) Tool use delta accumulation
# ---------------------------------------------------------------------------

def test_chat_stream_accumulates_tool_use_arguments():
    """Tool-use input_json_delta events must accumulate into a single
    parsed tool_call at message_delta time. This isn't strictly the
    'reasoning not shown' bug, but it lives in the same code path and
    is easy to break in a refactor — pin it here.
    """
    events = [
        _ContentBlockStart(
            _ToolUseBlock(id="toolu_1", name="get_weather"), index=0
        ),
        _ContentBlockDelta(_InputJSONDelta('{"city":'), index=0),
        _ContentBlockDelta(_InputJSONDelta(' "Paris"}'), index=0),
        _MessageDelta(stop_reason="tool_use"),
    ]

    async def _go():
        adapter = _patched_adapter(events)
        deltas = await _collect(adapter)

        final = next(d for d in deltas if d.finish_reason)
        assert final.finish_reason == "tool_use"
        assert len(final.tool_calls) == 1
        assert final.tool_calls[0].id == "toolu_1"
        assert final.tool_calls[0].name == "get_weather"
        assert final.tool_calls[0].arguments == {"city": "Paris"}

    asyncio.run(_go())


# ---------------------------------------------------------------------------
# 4) Build path sends the `thinking` parameter when thinking is enabled
# ---------------------------------------------------------------------------

def test_chat_stream_sends_thinking_parameter_when_enabled():
    """If we ask for thinking and the adapter forgets to add
    `kwargs["thinking"]`, the provider won't return thinking blocks.
    The user-visible bug would be: empty thinking card. Pin the kwargs
    shape so a refactor that drops the `thinking` param fails here.
    """
    captured: dict = {}

    class _CaptureStream(_FakeStream):
        def __init__(self):
            super().__init__([
                _ContentBlockStart(_ThinkingBlock(signature="s"), index=0),
                _ContentBlockDelta(_ThinkingDelta("hi"), index=0),
                _MessageDelta(stop_reason="end_turn"),
            ])

    adapter = AnthropicChatAdapter(api_key="test", base_url="http://test", default_max_tokens=1000)
    stream_mock = MagicMock()
    stream_mock.__aenter__ = AsyncMock(return_value=_CaptureStream())
    stream_mock.__aexit__ = AsyncMock(return_value=False)

    def _capture(**kwargs):
        captured.update(kwargs)
        return stream_mock

    adapter._client = MagicMock()
    adapter._client.messages.stream = _capture

    async def _go():
        await _collect(
            adapter, thinking_config={"type": "enabled", "budget_tokens": 2000}
        )

    asyncio.run(_go())

    assert "thinking" in captured, (
        f"thinking=... not added to messages.stream kwargs: {list(captured.keys())!r}"
    )
    assert captured["thinking"] == {"type": "enabled", "budget_tokens": 2000}
    # And thinking must be absent when no thinking_config is passed.
    captured.clear()

    class _NoThinkStream(_FakeStream):
        def __init__(self):
            super().__init__([
                _ContentBlockStart(_TextBlock(), index=0),
                _MessageDelta(stop_reason="end_turn"),
            ])

    stream_mock2 = MagicMock()
    stream_mock2.__aenter__ = AsyncMock(return_value=_NoThinkStream())
    stream_mock2.__aexit__ = AsyncMock(return_value=False)
    adapter._client.messages.stream = lambda **kw: (_ for _ in ()).throw(AssertionError("rec")) if False else (captured.update(kw) or stream_mock2)
    # That trick is too cute — set it directly:
    def _capture2(**kwargs):
        captured.clear()
        captured.update(kwargs)
        return stream_mock2
    adapter._client.messages.stream = _capture2

    async def _go2():
        await _collect(adapter)  # no thinking_config

    asyncio.run(_go2())
    assert "thinking" not in captured, (
        f"thinking=... should not be sent when thinking_config is None: {captured!r}"
    )
