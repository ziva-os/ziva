import pytest

from ziva.adapters.openai.provider import (
    _build_api_messages,
    _is_minimax_m3,
    _normalize_reasoning,
)
from ziva.shared_types import ChatMessage, ToolCallItem


class FakeDelta:
    def __init__(self, **kwargs):
        for k, v in kwargs.items():
            setattr(self, k, v)


class TestMiniMaxDetection:
    def test_detects_minimax_m3(self):
        assert _is_minimax_m3("https://api.minimaxi.chat/v1", "MiniMax-M3")
        assert _is_minimax_m3("https://api.minimaxi.com/v1", "minimax-m3-2025")

    def test_rejects_non_minimax_base_url(self):
        assert not _is_minimax_m3("https://api.openai.com/v1", "MiniMax-M3")

    def test_rejects_non_m3_model(self):
        assert not _is_minimax_m3("https://api.minimaxi.chat/v1", "abab6.5s")


class TestNormalizeReasoning:
    def test_prefers_reasoning_content(self):
        delta = FakeDelta(reasoning_content="think", reasoning="other", reasoning_details=[])
        assert _normalize_reasoning(delta) == "think"

    def test_falls_back_to_reasoning(self):
        delta = FakeDelta(reasoning="fallback")
        assert _normalize_reasoning(delta) == "fallback"

    def test_normalizes_reasoning_details(self):
        delta = FakeDelta(reasoning_details=[
            {"type": "reasoning.text", "text": "step 1"},
            {"type": "reasoning.text", "text": "step 2"},
        ])
        assert _normalize_reasoning(delta) == "step 1step 2"

    def test_ignores_unknown_reasoning_details_type(self):
        delta = FakeDelta(reasoning_details=[
            {"type": "reasoning.other", "text": "ignore"},
            {"type": "reasoning.text", "text": "keep"},
        ])
        assert _normalize_reasoning(delta) == "keep"

    def test_returns_none_when_empty(self):
        assert _normalize_reasoning(FakeDelta()) is None


class TestBuildApiMessages:
    def test_reasoning_content_for_openai(self):
        msg = ChatMessage(role="assistant", content="hi", reasoning_content="because")
        api = _build_api_messages([msg], model="gpt-4o")
        assert api[0]["reasoning_content"] == "because"
        assert "reasoning_details" not in api[0]

    def test_reasoning_content_converted_to_details_for_minimax_m3(self):
        msg = ChatMessage(role="assistant", content="hi", reasoning_content="because")
        api = _build_api_messages(
            [msg],
            model="MiniMax-M3",
            base_url="https://api.minimaxi.chat/v1",
        )
        assert api[0]["reasoning_details"] == [
            {"type": "reasoning.text", "text": "because"}
        ]
        assert "reasoning_content" not in api[0]

    def test_reasoning_content_kept_for_thinking_models(self):
        tc = ToolCallItem(id="t1", name="foo", arguments={})
        msg = ChatMessage(role="assistant", content="", tool_calls=[tc])
        api = _build_api_messages(
            [msg],
            model="claude-opus-4",
            thinking_enabled=True,
            capabilities={"thinking": True},
        )
        assert api[0]["reasoning_content"] == ""
        assert "tool_calls" in api[0]

    def test_tool_calls_with_empty_reasoning_converted_for_minimax_m3(self):
        tc = ToolCallItem(id="t1", name="foo", arguments={})
        msg = ChatMessage(role="assistant", content="", tool_calls=[tc])
        api = _build_api_messages(
            [msg],
            model="MiniMax-M3",
            base_url="https://api.minimaxi.chat/v1",
            thinking_enabled=True,
            capabilities={"thinking": True},
        )
        assert "reasoning_content" not in api[0]
        assert "reasoning_details" not in api[0]
        assert "tool_calls" in api[0]

    def test_tool_message_fields(self):
        msg = ChatMessage(role="tool", content="result", tool_call_id="t1", name="foo")
        api = _build_api_messages([msg])
        assert api[0]["tool_call_id"] == "t1"
        assert api[0]["name"] == "foo"
