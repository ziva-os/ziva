"""OpenAI adapter chat/stream tests using real openai SDK types.

Only the network client is stubbed; responses and stream chunks are real
openai SDK objects so we exercise field access paths exactly as they appear
in production.
"""

from __future__ import annotations

import pytest
from openai.types import CompletionUsage
from openai.types.chat.chat_completion import ChatCompletion, Choice
from openai.types.chat.chat_completion_chunk import ChatCompletionChunk, Choice as ChunkChoice, ChoiceDelta
from openai.types.chat.chat_completion_message import ChatCompletionMessage
from openai.types.chat.chat_completion_message_tool_call import (
    ChatCompletionMessageToolCall,
    Function,
)

from ziva.adapters.openai.provider import OpenAIChatAdapter


class _FakeCompletions:
    def __init__(self, create_fn=None, stream_fn=None):
        self._create_fn = create_fn
        self._stream_fn = stream_fn

    async def create(self, **kwargs):
        if self._stream_fn is not None and kwargs.get("stream"):
            return self._stream_fn(**kwargs)
        return self._create_fn(**kwargs)


class _FakeClient:
    def __init__(self, completions: _FakeCompletions):
        self.chat = type("_Chat", (), {"completions": completions})()


class TestOpenAIChatAdapter:
    @pytest.mark.asyncio
    async def test_chat_promotes_refusal_to_content_and_content_filter(self):
        adapter = OpenAIChatAdapter(api_key="dummy")
        msg = ChatCompletionMessage(role="assistant", content=None, refusal="I cannot help with that")
        choice = Choice(index=0, message=msg, finish_reason="stop")
        resp = ChatCompletion(
            id="chatcmpl-1",
            choices=[choice],
            model="gpt-4o",
            object="chat.completion",
            created=1234567890,
            usage=CompletionUsage(completion_tokens=5, prompt_tokens=10, total_tokens=15),
        )
        adapter._client = _FakeClient(_FakeCompletions(create_fn=lambda **kw: resp))
        result = await adapter.chat([], model="gpt-4o")
        assert result.content == "I cannot help with that"
        assert result.finish_reason == "content_filter"
        assert result.reasoning_content is None

    @pytest.mark.asyncio
    async def test_chat_keeps_content_when_refusal_and_content_both_present(self):
        adapter = OpenAIChatAdapter(api_key="dummy")
        msg = ChatCompletionMessage(role="assistant", content="here is safe info", refusal="policy note")
        choice = Choice(index=0, message=msg, finish_reason="stop")
        resp = ChatCompletion(
            id="chatcmpl-1",
            choices=[choice],
            model="gpt-4o",
            object="chat.completion",
            created=1234567890,
            usage=CompletionUsage(completion_tokens=5, prompt_tokens=10, total_tokens=15),
        )
        adapter._client = _FakeClient(_FakeCompletions(create_fn=lambda **kw: resp))
        result = await adapter.chat([], model="gpt-4o")
        assert result.content == "here is safe info"
        assert result.finish_reason == "stop"

    @pytest.mark.asyncio
    async def test_chat_extracts_reasoning_content(self):
        adapter = OpenAIChatAdapter(api_key="dummy")
        msg = ChatCompletionMessage(role="assistant", content="hello", reasoning_content="think")
        choice = Choice(index=0, message=msg, finish_reason="stop")
        resp = ChatCompletion(
            id="chatcmpl-1",
            choices=[choice],
            model="o3-mini",
            object="chat.completion",
            created=1234567890,
            usage=CompletionUsage(completion_tokens=5, prompt_tokens=10, total_tokens=15),
        )
        adapter._client = _FakeClient(_FakeCompletions(create_fn=lambda **kw: resp))
        result = await adapter.chat([], model="o3-mini")
        assert result.content == "hello"
        assert result.reasoning_content == "think"

    @pytest.mark.asyncio
    async def test_chat_tool_calls_are_parsed(self):
        adapter = OpenAIChatAdapter(api_key="dummy")
        tc = ChatCompletionMessageToolCall(
            id="call_1",
            function=Function(name="foo", arguments='{"x": 1}'),
            type="function",
        )
        msg = ChatCompletionMessage(role="assistant", content="", tool_calls=[tc])
        choice = Choice(index=0, message=msg, finish_reason="tool_calls")
        resp = ChatCompletion(
            id="chatcmpl-1",
            choices=[choice],
            model="gpt-4o",
            object="chat.completion",
            created=1234567890,
            usage=CompletionUsage(completion_tokens=5, prompt_tokens=10, total_tokens=15),
        )
        adapter._client = _FakeClient(_FakeCompletions(create_fn=lambda **kw: resp))
        result = await adapter.chat([], model="gpt-4o")
        assert result.finish_reason == "tool_calls"
        assert len(result.tool_calls) == 1
        assert result.tool_calls[0].name == "foo"
        assert result.tool_calls[0].arguments == {"x": 1}


class TestOpenAIChatStreamAdapter:
    @pytest.mark.asyncio
    async def test_stream_promotes_refusal_to_content_and_content_filter(self):
        adapter = OpenAIChatAdapter(api_key="dummy")
        delta = ChoiceDelta(content=None, refusal="I cannot help with that")
        choice = ChunkChoice(index=0, delta=delta, finish_reason=None)
        chunk = ChatCompletionChunk(
            id="chatcmpl-1",
            choices=[choice],
            model="gpt-4o",
            object="chat.completion.chunk",
            created=1234567890,
        )

        async def _stream(**kw):
            yield chunk

        adapter._client = _FakeClient(_FakeCompletions(stream_fn=_stream))
        deltas = [d async for d in adapter.chat_stream([], model="gpt-4o")]
        assert len(deltas) == 1
        assert deltas[0].content == "I cannot help with that"
        assert deltas[0].finish_reason == "content_filter"

    @pytest.mark.asyncio
    async def test_stream_emits_reasoning_content_delta(self):
        adapter = OpenAIChatAdapter(api_key="dummy")
        delta = ChoiceDelta(content="hi", reasoning_content="think")
        choice = ChunkChoice(index=0, delta=delta, finish_reason="stop")
        chunk = ChatCompletionChunk(
            id="chatcmpl-1",
            choices=[choice],
            model="o3-mini",
            object="chat.completion.chunk",
            created=1234567890,
        )

        async def _stream(**kw):
            yield chunk

        adapter._client = _FakeClient(_FakeCompletions(stream_fn=_stream))
        deltas = [d async for d in adapter.chat_stream([], model="o3-mini")]
        assert len(deltas) == 1
        assert deltas[0].content == "hi"
        assert deltas[0].reasoning_content == "think"
        assert deltas[0].finish_reason == "stop"

    @pytest.mark.asyncio
    async def test_stream_usage_chunk_with_empty_choices(self):
        adapter = OpenAIChatAdapter(api_key="dummy")
        usage_chunk = ChatCompletionChunk(
            id="chatcmpl-1",
            choices=[],
            model="gpt-4o",
            object="chat.completion.chunk",
            created=1234567890,
            usage=CompletionUsage(completion_tokens=5, prompt_tokens=10, total_tokens=15),
        )

        async def _stream(**kw):
            yield usage_chunk

        adapter._client = _FakeClient(_FakeCompletions(stream_fn=_stream))
        deltas = [d async for d in adapter.chat_stream([], model="gpt-4o")]
        assert len(deltas) == 1
        assert deltas[0].usage == {"prompt_tokens": 10, "completion_tokens": 5}
        assert deltas[0].content == ""
        assert deltas[0].finish_reason is None
