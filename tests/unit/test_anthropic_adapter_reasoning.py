"""Anthropic reasoning tests using real anthropic SDK types.

We avoid mocking the SDK data model classes; only the network client is
stubbed so these tests don't make real API calls.
"""

from __future__ import annotations

import pytest
from anthropic.types import (
    Message,
    MessageDeltaUsage,
    RawContentBlockDeltaEvent,
    RawContentBlockStartEvent,
    RawContentBlockStopEvent,
    RawMessageDeltaEvent,
    RawMessageStartEvent,
    RawMessageStopEvent,
    RedactedThinkingBlock,
    TextBlock,
    TextDelta,
    ThinkingBlock,
    ThinkingDelta,
    Usage,
)

from ziva.adapters.anthropic.provider import (
    AnthropicChatAdapter,
    _build_anthropic_messages,
)
from ziva.shared_types import ChatMessage


def _make_message(*blocks, usage=None, **kwargs):
    return Message(
        id="msg-1",
        type="message",
        role="assistant",
        model="claude-3-7-sonnet-20250219",
        content=list(blocks),
        stop_reason="end_turn",
        usage=usage or Usage(input_tokens=10, output_tokens=5),
        **kwargs,
    )


class _FakeStream:
    """Yields real Anthropic SDK stream events."""

    def __init__(self, events, final_message=None):
        self._events = events
        self._final_message = final_message

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    async def __aiter__(self):
        for event in self._events:
            yield event

    async def get_final_message(self):
        return self._final_message


class _FakeMessages:
    def __init__(self, create_fn=None, stream_obj=None):
        self._create_fn = create_fn
        self._stream_obj = stream_obj

    async def create(self, **kwargs):
        return self._create_fn(**kwargs)

    def stream(self, **kwargs):
        return self._stream_obj


class _FakeClient:
    def __init__(self, messages: _FakeMessages):
        self.messages = messages


class TestAnthropicChatReasoning:
    @pytest.mark.asyncio
    async def test_extracts_reasoning_and_signature_from_thinking_block(self):
        adapter = AnthropicChatAdapter(api_key="dummy")
        resp = _make_message(
            ThinkingBlock(type="thinking", thinking="step one", signature="sig-abc"),
            TextBlock(type="text", text="hello"),
        )
        adapter._client = _FakeClient(_FakeMessages(create_fn=lambda **kw: resp))
        result = await adapter.chat([], model="claude-3-7-sonnet-20250219")
        assert result.reasoning_content == "step one"
        assert result.reasoning_signature == "sig-abc"
        assert result.content == "hello"

    @pytest.mark.asyncio
    async def test_no_reasoning_for_plain_text_response(self):
        adapter = AnthropicChatAdapter(api_key="dummy")
        resp = _make_message(TextBlock(type="text", text="hello"))
        adapter._client = _FakeClient(_FakeMessages(create_fn=lambda **kw: resp))
        result = await adapter.chat([], model="claude-3-7-sonnet-20250219")
        assert result.reasoning_content is None
        assert result.reasoning_signature is None

    @pytest.mark.asyncio
    async def test_redacted_thinking_does_not_pollute_reasoning(self):
        adapter = AnthropicChatAdapter(api_key="dummy")
        # Redacted thinking blocks don't carry a `thinking` string; only a
        # data blob. We should not expose them as readable reasoning.
        resp = _make_message(
            RedactedThinkingBlock(type="redacted_thinking", data="redacted-abc"),
            TextBlock(type="text", text="hello"),
        )
        adapter._client = _FakeClient(_FakeMessages(create_fn=lambda **kw: resp))
        result = await adapter.chat([], model="claude-3-7-sonnet-20250219")
        assert result.reasoning_content is None
        assert result.reasoning_signature is None
        assert result.content == "hello"


class TestAnthropicChatStreamReasoning:
    @pytest.mark.asyncio
    async def test_streaming_emits_reasoning_and_signature(self):
        adapter = AnthropicChatAdapter(api_key="dummy")
        events = [
            RawMessageStartEvent(
                type="message_start",
                message=_make_message(),
            ),
            RawContentBlockStartEvent(
                type="content_block_start",
                index=0,
                content_block=ThinkingBlock(type="thinking", thinking="think", signature="sig-x"),
            ),
            RawContentBlockDeltaEvent(
                type="content_block_delta",
                index=0,
                delta=ThinkingDelta(type="thinking_delta", thinking=" more"),
            ),
            RawContentBlockStopEvent(type="content_block_stop", index=0),
            RawContentBlockStartEvent(
                type="content_block_start",
                index=1,
                content_block=TextBlock(type="text", text=""),
            ),
            RawContentBlockDeltaEvent(
                type="content_block_delta",
                index=1,
                delta=TextDelta(type="text_delta", text="hello"),
            ),
            RawContentBlockStopEvent(type="content_block_stop", index=1),
            RawMessageDeltaEvent(
                type="message_delta",
                delta={"stop_reason": "end_turn"},
                usage=MessageDeltaUsage(output_tokens=5),
            ),
            RawMessageStopEvent(type="message_stop"),
        ]
        stream = _FakeStream(events, final_message=_make_message())
        adapter._client = _FakeClient(_FakeMessages(stream_obj=stream))
        deltas = [d async for d in adapter.chat_stream([], model="claude-3-7-sonnet-20250219")]
        reasoning_parts = [d.reasoning_content for d in deltas if d.reasoning_content]
        assert reasoning_parts == ["think", " more"]
        signatures = [d.reasoning_signature for d in deltas if d.reasoning_signature]
        assert signatures == ["sig-x"]
        text_parts = [d.content for d in deltas if d.content]
        assert text_parts == ["hello"]

    @pytest.mark.asyncio
    async def test_streaming_message_start_usage_is_emitted(self):
        adapter = AnthropicChatAdapter(api_key="dummy")
        events = [
            RawMessageStartEvent(
                type="message_start",
                message=_make_message(
                    TextBlock(type="text", text="hi"),
                    usage=Usage(
                        input_tokens=20,
                        output_tokens=3,
                        cache_creation_input_tokens=5,
                        cache_read_input_tokens=2,
                    ),
                ),
            ),
            RawContentBlockStartEvent(
                type="content_block_start",
                index=0,
                content_block=TextBlock(type="text", text=""),
            ),
            RawContentBlockDeltaEvent(
                type="content_block_delta",
                index=0,
                delta=TextDelta(type="text_delta", text="hello"),
            ),
            RawContentBlockStopEvent(type="content_block_stop", index=0),
            RawMessageDeltaEvent(
                type="message_delta",
                delta={"stop_reason": "end_turn"},
                usage=MessageDeltaUsage(output_tokens=3),
            ),
            RawMessageStopEvent(type="message_stop"),
        ]
        stream = _FakeStream(events, final_message=_make_message())
        adapter._client = _FakeClient(_FakeMessages(stream_obj=stream))
        deltas = [d async for d in adapter.chat_stream([], model="claude-3-7-sonnet-20250219")]
        usage_deltas = [d.usage for d in deltas if d.usage]
        assert usage_deltas[0] == {
            "prompt_tokens": 27,
            "cache_creation_input_tokens": 5,
            "cache_read_input_tokens": 2,
        }
        assert usage_deltas[1] == {"completion_tokens": 3}


class TestBuildAnthropicMessagesReasoning:
    def test_replays_thinking_block_with_signature(self):
        msg = ChatMessage(
            role="assistant",
            content="hello",
            reasoning_content="think",
            reasoning_signature="sig-y",
        )
        _, api = _build_anthropic_messages([msg])
        assert api[0]["content"][0] == {
            "type": "thinking",
            "thinking": "think",
            "signature": "sig-y",
        }

    def test_drops_thinking_block_without_signature(self):
        msg = ChatMessage(
            role="assistant",
            content="hello",
            reasoning_content="think",
        )
        _, api = _build_anthropic_messages([msg])
        assert api[0]["content"] == [{"type": "text", "text": "hello"}]

    def test_drops_thinking_block_without_content(self):
        msg = ChatMessage(
            role="assistant",
            content="hello",
            reasoning_signature="sig-y",
        )
        _, api = _build_anthropic_messages([msg])
        assert api[0]["content"] == [{"type": "text", "text": "hello"}]
