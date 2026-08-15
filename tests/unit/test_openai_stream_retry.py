"""Tests for OpenAI adapter stream-interruption retry logic.

Covers the scenario where the provider closes the connection mid-stream
(observed with DeepSeek on long outputs), producing
``httpx.RemoteProtocolError`` / ``httpx.ReadError``.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import httpx
import pytest
from openai.types.chat.chat_completion_chunk import (
    ChatCompletionChunk,
    Choice as ChunkChoice,
    ChoiceDelta,
)

from ziva.adapters.openai.provider import OpenAIChatAdapter


# ---- helpers (mirrors test_openai_adapter_chat.py mock structure) ----

class _FakeCompletions:
    def __init__(self, stream_fn=None):
        self._stream_fn = stream_fn

    async def create(self, **kwargs):
        if self._stream_fn is not None and kwargs.get("stream"):
            return self._stream_fn(**kwargs)
        raise AssertionError("non-stream create not expected")


class _FakeClient:
    def __init__(self, completions):
        self.chat = type("_Chat", (), {"completions": completions})()


def _make_chunk(content: str = "", finish_reason: str | None = None) -> ChatCompletionChunk:
    return ChatCompletionChunk(
        id="chatcmpl-1",
        choices=[ChunkChoice(index=0, delta=ChoiceDelta(content=content), finish_reason=finish_reason)],
        model="test-model",
        object="chat.completion.chunk",
        created=1234567890,
    )


# ---- tests ----

class TestStreamInterruptionRetry:

    @pytest.mark.asyncio
    async def test_normal_stream_not_affected(self):
        """A normal stream should produce all deltas with no retries."""
        adapter = OpenAIChatAdapter(api_key="dummy")

        async def _stream(**kw):
            yield _make_chunk("hello")
            yield _make_chunk(" world", finish_reason="stop")

        adapter._client = _FakeClient(_FakeCompletions(stream_fn=_stream))
        deltas = [d async for d in adapter.chat_stream([], model="test-model")]

        assert len(deltas) == 2
        assert deltas[0].content == "hello"
        assert deltas[1].content == " world"
        assert deltas[1].finish_reason == "stop"

    @pytest.mark.asyncio
    async def test_partial_interrupt_keeps_content_no_retry(self):
        """Stream interrupted AFTER yielding content → keep partial, do NOT retry."""
        adapter = OpenAIChatAdapter(api_key="dummy")

        call_count = 0

        async def _stream(**kw):
            nonlocal call_count
            call_count += 1
            yield _make_chunk("partial output")
            raise httpx.RemoteProtocolError("peer closed connection without sending complete message body")

        adapter._client = _FakeClient(_FakeCompletions(stream_fn=_stream))
        deltas = [d async for d in adapter.chat_stream([], model="test-model")]

        # Partial content preserved
        assert len(deltas) == 1
        assert deltas[0].content == "partial output"
        # Must NOT retry (would duplicate output)
        assert call_count == 1

    @pytest.mark.asyncio
    async def test_no_data_interrupt_retries_and_succeeds(self):
        """Stream interrupted BEFORE any data → retry, second attempt succeeds."""
        adapter = OpenAIChatAdapter(api_key="dummy")

        call_count = 0

        async def _stream(**kw):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                # First attempt: die immediately, no yield
                raise httpx.RemoteProtocolError("peer closed connection")
            # Second attempt: succeed
            yield _make_chunk("recovered", finish_reason="stop")

        adapter._client = _FakeClient(_FakeCompletions(stream_fn=_stream))

        # Patch sleep to avoid real delays during retry backoff
        with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            deltas = [d async for d in adapter.chat_stream([], model="test-model")]

        assert len(deltas) == 1
        assert deltas[0].content == "recovered"
        assert call_count == 2  # first failed, second succeeded
        mock_sleep.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_no_data_interrupt_retries_exhausted(self):
        """Stream interrupted BEFORE any data → all retries fail → raise."""
        adapter = OpenAIChatAdapter(api_key="dummy")

        call_count = 0

        async def _stream(**kw):
            nonlocal call_count
            call_count += 1
            raise httpx.RemoteProtocolError("peer closed connection")
            yield  # unreachable — forces async generator semantics

        adapter._client = _FakeClient(_FakeCompletions(stream_fn=_stream))

        with patch("asyncio.sleep", new_callable=AsyncMock):
            with pytest.raises(httpx.RemoteProtocolError):
                [d async for d in adapter.chat_stream([], model="test-model")]

        # MAX_RETRIES + 1 = 3 total attempts
        from ziva.adapters.retry import MAX_RETRIES
        assert call_count == MAX_RETRIES + 1

    @pytest.mark.asyncio
    async def test_read_error_also_handled(self):
        """httpx.ReadError should be treated the same as RemoteProtocolError."""
        adapter = OpenAIChatAdapter(api_key="dummy")

        call_count = 0

        async def _stream(**kw):
            nonlocal call_count
            call_count += 1
            yield _make_chunk("data before error")
            raise httpx.ReadError("read timed out")

        adapter._client = _FakeClient(_FakeCompletions(stream_fn=_stream))
        deltas = [d async for d in adapter.chat_stream([], model="test-model")]

        assert len(deltas) == 1
        assert deltas[0].content == "data before error"
        assert call_count == 1  # no retry after partial output

    @pytest.mark.asyncio
    async def test_usage_only_chunk_then_interrupt_retries(self):
        """A usage chunk with empty choices sets stream_started → no retry.

        The usage chunk IS yielded, so even though it has no content,
        stream_started is True and we must not retry.
        """
        adapter = OpenAIChatAdapter(api_key="dummy")

        from openai.types import CompletionUsage

        usage_chunk = ChatCompletionChunk(
            id="chatcmpl-1",
            choices=[],
            model="test-model",
            object="chat.completion.chunk",
            created=1234567890,
            usage=CompletionUsage(completion_tokens=5, prompt_tokens=10, total_tokens=15),
        )

        call_count = 0

        async def _stream(**kw):
            nonlocal call_count
            call_count += 1
            yield usage_chunk
            raise httpx.RemoteProtocolError("connection lost")

        adapter._client = _FakeClient(_FakeCompletions(stream_fn=_stream))
        deltas = [d async for d in adapter.chat_stream([], model="test-model")]

        # Usage delta was yielded, so stream_started=True → no retry
        assert call_count == 1
        assert len(deltas) == 1
        assert deltas[0].usage is not None
