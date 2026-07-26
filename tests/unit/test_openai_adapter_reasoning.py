import pytest

from ziva.adapters.openai.provider import (
    _build_api_messages,
    _is_minimax_m3,
    _is_openai_reasoning_model,
    _is_reasoning_model,
    _model_forces_max_completion_tokens,
    _normalize_reasoning,
    _provider_requires_reasoning_echo,
    _sanitize_openai_messages,
    _supports_reasoning_content,
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


class TestReasoningSupportHelpers:
    def test_o_series_are_openai_reasoning_models(self):
        assert _is_openai_reasoning_model("o1-mini")
        assert _is_openai_reasoning_model("o3-mini-2025")
        assert _is_openai_reasoning_model("openai/o4-mini")
        assert not _is_openai_reasoning_model("gpt-5")
        assert not _is_openai_reasoning_model("gpt-5.4")
        assert not _is_openai_reasoning_model("gpt-4o")
        assert not _is_openai_reasoning_model("claude-3-5-sonnet")
        assert not _is_openai_reasoning_model("olmo-1-preview")

    def test_reasoning_model_detection_matches_hermes_allowlist(self):
        # OpenAI o-series
        assert _is_reasoning_model("o1-mini")
        assert _is_reasoning_model("openai/o3-mini")
        assert _is_reasoning_model("o4-mini")
        # DeepSeek
        assert _is_reasoning_model("deepseek-r1")
        assert _is_reasoning_model("deepseek-v4-pro")
        # Qwen
        assert _is_reasoning_model("qwq-32b")
        assert _is_reasoning_model("qwen3-235b-a22b")
        # Anthropic thinking variants (served via OpenAI-compatible proxies)
        assert _is_reasoning_model("claude-opus-4")
        assert _is_reasoning_model("claude-sonnet-4.5")
        # xAI Grok reasoning
        assert _is_reasoning_model("grok-4-fast-reasoning")
        assert _is_reasoning_model("grok-4.5")
        # gpt-5.x is NOT in Hermes's reasoning allowlist (reasoning_timeouts.py)
        assert not _is_reasoning_model("gpt-5")
        assert not _is_reasoning_model("gpt-5.4")
        # False positives
        assert not _is_reasoning_model("gpt-4o")
        assert not _is_reasoning_model("olmo-1-preview")

    def test_model_forces_max_completion_tokens_matches_hermes(self):
        # Native reasoning o-series
        assert _model_forces_max_completion_tokens("o1-mini")
        assert _model_forces_max_completion_tokens("openai/o3-mini")
        assert _model_forces_max_completion_tokens("o4-mini")
        # gpt-5.x and gpt-4o/gpt-4.1 (Hermes utils.py:model_forces_max_completion_tokens)
        assert _model_forces_max_completion_tokens("gpt-5")
        assert _model_forces_max_completion_tokens("openai/gpt-5.4")
        assert _model_forces_max_completion_tokens("gpt-4o")
        assert _model_forces_max_completion_tokens("gpt-4.1")
        # Negatives
        assert not _model_forces_max_completion_tokens("gpt-4")
        assert not _model_forces_max_completion_tokens("deepseek-r1")
        assert not _model_forces_max_completion_tokens("claude-opus-4")

    def test_requires_reasoning_echo_for_deepseek_kimi_mimo(self):
        assert _provider_requires_reasoning_echo("https://api.deepseek.com", "deepseek-chat")
        assert _provider_requires_reasoning_echo("https://api.moonshot.cn", "kimi-k1.5")
        assert _provider_requires_reasoning_echo(None, "MiMo-7B")
        assert not _provider_requires_reasoning_echo("https://api.openai.com", "gpt-4o")
        assert not _provider_requires_reasoning_echo("https://api.minimaxi.chat", "MiniMax-M3")

    def test_supports_reasoning_content_gating(self):
        assert _supports_reasoning_content("gpt-4o", True, {})
        assert _supports_reasoning_content("o3-mini", False, {})
        assert _supports_reasoning_content("deepseek-r1", False, {})
        assert _supports_reasoning_content("claude-opus-4", False, {})
        assert _supports_reasoning_content("claude-opus-4", False, {"thinking": True})
        assert not _supports_reasoning_content("gpt-4o", False, {})
        assert not _supports_reasoning_content("gpt-5", False, {})


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
    def test_reasoning_content_dropped_for_non_reasoning_model(self):
        msg = ChatMessage(role="assistant", content="hi", reasoning_content="because")
        api = _build_api_messages([msg], model="gpt-4o")
        assert "reasoning_content" not in api[0]
        assert "reasoning_details" not in api[0]

    def test_reasoning_content_kept_for_reasoning_model(self):
        msg = ChatMessage(role="assistant", content="hi", reasoning_content="because")
        api = _build_api_messages([msg], model="o3-mini")
        assert api[0]["reasoning_content"] == "because"
        assert "reasoning_details" not in api[0]

    def test_reasoning_content_kept_when_thinking_enabled(self):
        msg = ChatMessage(role="assistant", content="hi", reasoning_content="because")
        api = _build_api_messages([msg], model="gpt-4o", thinking_enabled=True)
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

    def test_deepseek_thinking_echo_pad_for_plain_assistant_turn(self):
        msg = ChatMessage(role="assistant", content="hi")
        api = _build_api_messages(
            [msg],
            model="deepseek-chat",
            base_url="https://api.deepseek.com/v1",
            thinking_enabled=True,
        )
        assert api[0]["reasoning_content"] == " "
        assert "reasoning_details" not in api[0]

    def test_deepseek_thinking_echo_pad_for_tool_call_turn(self):
        tc = ToolCallItem(id="t1", name="foo", arguments={})
        msg = ChatMessage(role="assistant", content="", tool_calls=[tc])
        api = _build_api_messages(
            [msg],
            model="deepseek-chat",
            base_url="https://api.deepseek.com/v1",
            thinking_enabled=True,
        )
        assert api[0]["reasoning_content"] == " "
        assert "tool_calls" in api[0]

    def test_kimi_thinking_echo_pad(self):
        msg = ChatMessage(role="assistant", content="hi")
        api = _build_api_messages(
            [msg],
            model="kimi-k1.5",
            base_url="https://api.moonshot.cn/v1",
            thinking_enabled=True,
        )
        assert api[0]["reasoning_content"] == " "

    def test_tool_message_fields(self):
        msg = ChatMessage(role="tool", content="result", tool_call_id="t1", name="foo")
        api = _build_api_messages([msg])
        assert api[0]["tool_call_id"] == "t1"
        assert api[0]["name"] == "foo"

    def test_sanitizes_internal_tool_call_fields(self):
        msg = ChatMessage(role="assistant", content="hi", tool_calls=[
            ToolCallItem(id="t1", name="foo", arguments={"x": 1})
        ])
        api = _build_api_messages([msg], model="gpt-4o")
        tc = api[0]["tool_calls"][0]
        assert set(tc.keys()) == {"id", "type", "function"}
        assert set(tc["function"].keys()) == {"name", "arguments"}

    def test_sanitizes_unknown_message_keys(self):
        raw = {
            "role": "assistant",
            "content": "hi",
            "reasoning_content": "because",
            "_hidden": True,
            "tool_name": "sqlite",
            "api_content": "secret",
            "extra": "should-go",
        }
        sanitized = _sanitize_openai_messages([raw])
        assert set(sanitized[0].keys()) == {"role", "content", "reasoning_content"}

    def test_system_prompt_is_included(self):
        api = _build_api_messages([], system_prompt="sys", model="gpt-4o")
        assert api[0] == {"role": "system", "content": "sys"}
