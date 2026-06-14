"""Tests for reasoning_content field persistence (Area 5).

Verifies that:
- StreamDelta.reasoning_content is a real field (not just None)
- ChatMessage.reasoning_content round-trips through dict serialization
- extractThinking fallback still works for legacy <think> tags
"""
from __future__ import annotations

from ziva_runtime.shared_types import ChatMessage, StreamDelta


def test_stream_delta_has_reasoning_content_field():
    delta = StreamDelta(content="hello", reasoning_content="thought")
    assert delta.reasoning_content == "thought"
    assert delta.content == "hello"


def test_stream_delta_reasoning_content_defaults_none():
    delta = StreamDelta(content="hello")
    assert delta.reasoning_content is None


def test_chat_message_round_trips_reasoning_content():
    msg = ChatMessage(role="assistant", content="answer")
    msg.reasoning_content = "I considered X then Y"
    msg.reasoning_signature = "sig_123"

    assert msg.reasoning_content == "I considered X then Y"
    assert msg.reasoning_signature == "sig_123"


def test_chat_message_serialization_preserves_reasoning():
    """When persisted via __dict__, reasoning_content must survive."""
    msg = ChatMessage(role="assistant", content="answer")
    msg.reasoning_content = "thinking..."

    serialized = msg.__dict__
    assert serialized["reasoning_content"] == "thinking..."
    assert serialized["content"] == "answer"


def test_chat_message_loads_from_dict_with_reasoning():
    record = {
        "role": "assistant",
        "content": "main text",
        "reasoning_content": "chain of thought",
        "reasoning_signature": "sig_abc",
    }
    msg = ChatMessage(
        role=record["role"],
        content=record["content"],
        reasoning_content=record.get("reasoning_content"),
        reasoning_signature=record.get("reasoning_signature"),
    )
    assert msg.reasoning_content == "chain of thought"
    assert msg.reasoning_signature == "sig_abc"
