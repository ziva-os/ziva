from __future__ import annotations

import json
import os
import re
from typing import Any, AsyncIterator, Iterable, Protocol

from ziva.adapters.retry import call_with_retry
from ziva.shared_types import ChatMessage, ChatResult, StreamDelta, ToolCallItem


def _is_minimax_m3(base_url: str | None, model: str) -> bool:
    """Detect the MiniMax-M3 OpenAI-compatible endpoint.

    MiniMax-M3 exposes reasoning via an opt-in ``reasoning_split=True``
    field (in ``extra_body``). When enabled, the model returns reasoning
    in ``reasoning_details`` instead of embedding it in ``content``.
    """
    if not base_url:
        return False
    return "minimaxi" in base_url.lower() and model.lower().startswith("minimax-m3")


def _model_slug(model: str) -> str:
    """Return the slug-only part of a model id, lowercased.

    Strips provider prefixes like ``openai/``, ``x-ai/``, ``anthropic/`` so
    matching can anchor at the start of the family name.
    """
    m = (model or "").strip().lower()
    if "/" in m:
        m = m.rsplit("/", 1)[-1]
    return m


# Reasoning-model families that emit structured reasoning deltas/content.
# Aligned with Hermes's reasoning timeout allowlist (agent/reasoning_timeouts.py);
# start-of-slug anchored so derivatives like ``olmo-1`` don't match ``o1``.
_REASONING_MODEL_SLUGS: tuple[str, ...] = (
    # OpenAI o-series (Hermes reasoning_timeouts.py does not list gpt-5.x here;
    # gpt-5 is handled separately by ``_model_forces_max_completion_tokens``).
    "o1",
    "o1-mini",
    "o1-pro",
    "o1-preview",
    "o3",
    "o3-pro",
    "o3-mini",
    "o4-mini",
    # DeepSeek
    "deepseek-r1",
    "deepseek-reasoner",
    "deepseek-v4-flash",
    "deepseek-v4-pro",
    # Qwen
    "qwq-32b",
    "qwen3",
    # Anthropic thinking variants (served via OpenAI-compatible proxies)
    "claude-opus-4",
    "claude-opus-5",
    "claude-sonnet-5",
    "claude-sonnet-4.5",
    "claude-sonnet-4.6",
    # xAI Grok reasoning
    "grok-4-fast-reasoning",
    "grok-4.20-reasoning",
    "grok-4.5",
    # NVIDIA Nemotron hosted NIMs
    "nemotron-3-ultra",
    "nemotron-3-super",
    "nemotron-3-nano",
)

_REASONING_MODEL_RE = re.compile(
    r"^(?:" + "|".join(re.escape(s) for s in _REASONING_MODEL_SLUGS) + r")(?:[-._]|$)"
)

# OpenAI-native o-series: accept top-level ``reasoning_effort`` and reject
# ``max_tokens`` in favor of ``max_completion_tokens``. Future ``oN`` models
# are covered automatically by the ``^o\d+`` pattern.
_OPENAI_REASONING_MODEL_RE = re.compile(r"^o\d+(?:[-._]|$)")


def _is_reasoning_model(model: str) -> bool:
    """Return True when the model is in Hermes's reasoning-model allowlist.

    Used to decide whether to preserve ``reasoning_content`` on replay.
    Includes families served through OpenAI-compatible endpoints (DeepSeek,
    Qwen, Grok, Anthropic-via-proxy, ...) as well as native OpenAI o-series.
    """
    return bool(_REASONING_MODEL_RE.match(_model_slug(model)))


def _is_openai_reasoning_model(model: str) -> bool:
    """Return True for OpenAI-native o-series reasoning models.

    These accept top-level ``reasoning_effort``. gpt-5.x is handled by
    ``_model_forces_max_completion_tokens`` for the token limit, not here,
    because Hermes does not list it in its reasoning allowlist.
    """
    return bool(_OPENAI_REASONING_MODEL_RE.match(_model_slug(model)))


# Model families that must use ``max_completion_tokens`` instead of
# ``max_tokens``. Copied from Hermes ``utils.py:model_forces_max_completion_tokens``.
# Note: gpt-4o / gpt-4.1 are included here because they are deprecated fields,
# but they are NOT reasoning models.
_MODELS_FORCING_MAX_COMPLETION_TOKENS: tuple[str, ...] = (
    "gpt-4o",
    "gpt-4.1",
    "gpt-5",
)


def _model_forces_max_completion_tokens(model: str) -> bool:
    """Return True when the model family rejects ``max_tokens``.

    Mirrors Hermes's ``model_forces_max_completion_tokens`` (utils.py:534).
    OpenAI's newer families (gpt-4o, gpt-4.1, gpt-5, o1, o3, o4) require
    ``max_completion_tokens`` on /v1/chat/completions. Handles vendor prefixes
    like ``openai/gpt-5.4`` by stripping to the tail.
    """
    m = _model_slug(model)
    return (
        m.startswith(_MODELS_FORCING_MAX_COMPLETION_TOKENS)
        or bool(_OPENAI_REASONING_MODEL_RE.match(m))
    )


def _provider_requires_reasoning_echo(base_url: str | None, model: str) -> bool:
    """Providers that require a non-empty reasoning_content echo on every assistant turn.

    DeepSeek, Kimi/Moonshot, and MiMo enforce that every assistant message in
    thinking mode carries a non-empty ``reasoning_content`` (even a single
    space). OpenAI-native reasoning models and most strict OpenAI-compatible
    providers do not accept this field on input, so we only emit it when the
    target is known to require it.
    """
    if not base_url and not model:
        return False
    combined = f"{base_url or ''} {model or ''}".lower()
    return any(k in combined for k in ("deepseek", "kimi", "moonshot", "mimo"))


def _supports_reasoning_content(
    model: str,
    thinking_enabled: bool,
    capabilities: dict | None,
) -> bool:
    """True when the outgoing provider/model accepts reasoning_content on input."""
    if thinking_enabled:
        return True
    if _is_reasoning_model(model):
        return True
    if capabilities and capabilities.get("thinking"):
        return True
    return False


# Top-level message keys accepted by the OpenAI Chat Completions schema.
# Anything else is stripped before the wire to avoid 400/422 from strict
# providers (Mistral, Fireworks, Cerebras, Groq, ...). See Hermes
# ChatCompletionsTransport.convert_messages for the same guard.
_MESSAGE_ALLOWED_KEYS = frozenset({
    "role",
    "content",
    "name",
    "tool_call_id",
    "tool_calls",
    "reasoning_content",
    "reasoning_details",
})
_TOOL_CALL_ALLOWED_KEYS = frozenset({"id", "type", "function"})
_FUNCTION_ALLOWED_KEYS = frozenset({"name", "arguments"})


def _sanitize_openai_messages(messages: list[dict]) -> list[dict]:
    """Strip internal keys from outgoing OpenAI-format messages.

    Internal bookkeeping (``_``-prefixed keys, ``tool_name``, ``api_content``,
    Codex Responses IDs, Gemini thought signatures on non-Gemini targets)
    must not reach the wire. We also tighten each tool_call to the schema
    subset so strict providers cannot reject extra fields.
    """
    sanitized: list[dict] = []
    for msg in messages:
        if not isinstance(msg, dict):
            sanitized.append(msg)
            continue
        clean = {
            k: v
            for k, v in msg.items()
            if k in _MESSAGE_ALLOWED_KEYS and not (isinstance(k, str) and k.startswith("_"))
        }
        tool_calls = clean.get("tool_calls")
        if isinstance(tool_calls, list):
            clean_tool_calls: list[Any] = []
            for tc in tool_calls:
                if isinstance(tc, dict):
                    tc_clean = {k: v for k, v in tc.items() if k in _TOOL_CALL_ALLOWED_KEYS}
                    fn = tc_clean.get("function")
                    if isinstance(fn, dict):
                        tc_clean["function"] = {
                            k: v for k, v in fn.items() if k in _FUNCTION_ALLOWED_KEYS
                        }
                    clean_tool_calls.append(tc_clean)
                else:
                    clean_tool_calls.append(tc)
            clean["tool_calls"] = clean_tool_calls
        sanitized.append(clean)
    return sanitized


def _normalize_reasoning(delta_or_message: Any) -> str | None:
    """Return a single reasoning string from provider-specific fields.

    Preferred order:
      1. ``reasoning_content`` (OpenAI o1/o3)
      2. ``reasoning`` (some OpenAI-compatible providers)
      3. ``reasoning_details`` (MiniMax-M3 with ``reasoning_split=True``)
    """
    reasoning = getattr(delta_or_message, "reasoning_content", None) or getattr(
        delta_or_message, "reasoning", None
    )
    if reasoning:
        return reasoning

    details = getattr(delta_or_message, "reasoning_details", None)
    if isinstance(details, list):
        return "".join(
            d.get("text", "")
            for d in details
            if d.get("type") == "reasoning.text"
        )
    return None


def _build_api_messages(
    messages: Iterable[ChatMessage],
    system_prompt: str | None = None,
    model: str = "",
    base_url: str | None = None,
    thinking_enabled: bool = False,
    capabilities: dict | None = None,
) -> list[dict]:
    api_messages: list[dict] = []
    if system_prompt:
        api_messages.append({"role": "system", "content": system_prompt})
    supports_thinking = bool(capabilities and capabilities.get("thinking"))
    minimax_m3 = _is_minimax_m3(base_url, model)
    supports_reasoning = _supports_reasoning_content(model, thinking_enabled, capabilities)
    requires_reasoning_echo = _provider_requires_reasoning_echo(base_url, model)

    for m in messages:
        msg: dict[str, Any] = {"role": m.role, "content": m.content}

        if m.role == "assistant":
            reasoning = getattr(m, "reasoning_content", None)
            if reasoning and (supports_reasoning or minimax_m3):
                if minimax_m3:
                    # MiniMax expects reasoning as a structured array
                    msg["reasoning_details"] = [
                        {"type": "reasoning.text", "text": reasoning}
                    ]
                else:
                    msg["reasoning_content"] = reasoning

        if m.role == "assistant" and m.tool_calls:
            msg["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {"name": tc.name, "arguments": json.dumps(tc.arguments, ensure_ascii=False)},
                }
                for tc in m.tool_calls
            ]
            # Some providers (DeepSeek/Kimi/MiMo) require a non-empty
            # reasoning_content echo on every assistant turn in thinking
            # mode, including tool-call turns. Others (OpenAI o1/o3) only
            # need a pad when reasoning was actually emitted. Avoid leaking
            # reasoning to strict providers that reject the field.
            has_reasoning = "reasoning_content" in msg or "reasoning_details" in msg
            if not has_reasoning:
                if requires_reasoning_echo:
                    msg["reasoning_content"] = " "
                elif thinking_enabled or supports_thinking:
                    msg["reasoning_content"] = ""

        # Normalize any assistant reasoning_content added above into the
        # MiniMax reasoning_details format when talking to MiniMax-M3.
        if m.role == "assistant" and minimax_m3 and "reasoning_content" in msg:
            rc = msg.pop("reasoning_content")
            if rc:
                msg["reasoning_details"] = [{"type": "reasoning.text", "text": rc}]
        if m.role == "tool" and m.tool_call_id:
            msg["tool_call_id"] = m.tool_call_id
        if m.role == "tool" and m.name:
            msg["name"] = m.name
        api_messages.append(msg)

    # Plain assistant turns for echo-requiring providers also need a pad.
    for msg in api_messages:
        if msg.get("role") == "assistant" and requires_reasoning_echo:
            if not ("reasoning_content" in msg or "reasoning_details" in msg):
                msg["reasoning_content"] = " "

    return _sanitize_openai_messages(api_messages)


def _usage_from_openai(usage_obj: Any) -> dict | None:
    """Convert an OpenAI SDK Usage object into a flat dict.

    Unlike Anthropic, OpenAI's prompt prefix cache is fully automatic
    (no opt-in flag needed). The server detects stable prefixes and
    short-circuits re-computation; the savings surface as a cache-hit
    counter inside `usage`.

    Field locations differ across providers:
      - OpenAI official:   usage.prompt_tokens_details.cached_tokens
      - DeepSeek:          usage.prompt_cache_hit_tokens (+ prompt_cache_miss_tokens)
      - Kimi (OA格式):     usage.prompt_cache_hit_tokens
      - 通义/百川:          usage.prompt_tokens_details.cached_tokens (mostly)

    IMPORTANT semantic difference from the Anthropic adapter:
    OpenAI's `prompt_tokens` ALREADY INCLUDES cached tokens (cached tokens
    are a subset of prompt_tokens, not additive). So we surface
    prompt_tokens as-is and only add `cache_read_input_tokens` as a
    breakdown for display — we do NOT add them together.

    We normalize the cache-hit field name to `cache_read_input_tokens`
    so the runtime / UI can treat Anthropic and OpenAI identically.
    """
    if not usage_obj:
        return None
    prompt_tokens = getattr(usage_obj, "prompt_tokens", 0) or 0
    completion_tokens = getattr(usage_obj, "completion_tokens", 0) or 0
    if not (prompt_tokens or completion_tokens):
        return None
    result: dict[str, Any] = {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
    }

    # Cache hit — try the OpenAI official field first, then DeepSeek/Kimi.
    cached = 0
    ptd = getattr(usage_obj, "prompt_tokens_details", None)
    if ptd is not None:
        cached = getattr(ptd, "cached_tokens", 0) or 0
    if not cached:
        cached = getattr(usage_obj, "prompt_cache_hit_tokens", 0) or 0
    if cached:
        result["cache_read_input_tokens"] = cached

    # Reasoning tokens (o1/o3 thinking models) — live inside
    # completion_tokens_details, surfacing them lets the UI itemize
    # "X of the Y completion tokens were reasoning".
    ctd = getattr(usage_obj, "completion_tokens_details", None)
    if ctd is not None:
        rt = getattr(ctd, "reasoning_tokens", 0) or 0
        if rt:
            result["reasoning_tokens"] = rt
    return result


class ModelAdapter(Protocol):
    async def chat(
        self,
        messages: Iterable[ChatMessage],
        model: str,
        system_prompt: str | None = None,
        tools: list[dict] | None = None,
    ) -> ChatResult: ...

    def chat_stream(
        self,
        messages: Iterable[ChatMessage],
        model: str,
        system_prompt: str | None = None,
        tools: list[dict] | None = None,
        thinking_config: dict[str, Any] | None = None,
    ) -> AsyncIterator[StreamDelta]: ...


class OpenAIChatAdapter:
    """Adapter using openai SDK Chat Completions with native function calling."""

    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        capabilities: dict | None = None,
        options: dict | None = None,
        default_max_tokens: int = 0,
    ):
        from openai import AsyncOpenAI
        self._base_url = base_url or os.environ.get("OPENAI_BASE_URL")
        self._api_key = api_key or os.environ.get("OPENAI_API_KEY")
        # Allow the adapter to be constructed even when no key is configured
        # (e.g. during startup). The actual request will fail with 401 if a
        # real key is never supplied, matching opencode's lazy-fail behavior.
        if not self._api_key:
            self._api_key = "dummy"
        self._capabilities = capabilities or {}
        self._options = options or {}
        # max_tokens to send on each request. OpenAI-compatible providers
        # apply their own (often small, e.g. 4096) server-side default when
        # the request omits max_tokens, which silently truncates long
        # outputs with finish_reason=length. Sending an explicit large value
        # avoids that. 0 = omit (let the provider decide).
        self._default_max_tokens = int(default_max_tokens or 0)
        self._client = AsyncOpenAI(
            base_url=self._base_url, api_key=self._api_key, timeout=120.0
        )

    async def chat(
        self,
        messages: Iterable[ChatMessage],
        model: str,
        system_prompt: str | None = None,
        tools: list[dict] | None = None,
        thinking_config: dict[str, Any] | None = None,
    ) -> ChatResult:
        thinking_enabled = thinking_config is not None and thinking_config.get("type") == "enabled"
        api_messages = _build_api_messages(
            messages, system_prompt, model=model, base_url=self._base_url,
            thinking_enabled=thinking_enabled, capabilities=self._capabilities,
        )

        kwargs: dict[str, Any] = {"model": model, "messages": api_messages}
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"

        # Newer OpenAI families (gpt-4o, gpt-4.1, gpt-5, o-series) reject
        # ``max_tokens`` and require ``max_completion_tokens``; reasoning_effort
        # is only valid for native o-series. See Hermes
        # ``utils.py:model_forces_max_completion_tokens``.
        is_openai_reasoning_model = _is_openai_reasoning_model(model)
        forces_max_completion_tokens = _model_forces_max_completion_tokens(model)
        if self._default_max_tokens:
            if forces_max_completion_tokens:
                kwargs["max_completion_tokens"] = self._default_max_tokens
            else:
                kwargs["max_tokens"] = self._default_max_tokens

        if thinking_config and thinking_config.get("type") == "enabled":
            if is_openai_reasoning_model:
                kwargs["reasoning_effort"] = thinking_config.get("mode", "medium")

        if _is_minimax_m3(self._base_url, model):
            kwargs.setdefault("extra_body", {})
            kwargs["extra_body"].setdefault("reasoning_split", True)

        resp = await call_with_retry(self._client.chat.completions.create, **kwargs)
        choice = resp.choices[0]
        msg = choice.message
        content = msg.content or ""
        reasoning_content = _normalize_reasoning(msg)
        usage_dict = _usage_from_openai(getattr(resp, "usage", None))

        # OpenAI structured-refusal field. When a model declines, the SDK
        # populates ``message.refusal`` with the explanation and leaves
        # ``content`` empty. OpenAI-compatible proxies that front Anthropic /
        # Bedrock surface a Claude refusal this way — or via
        # ``finish_reason="content_filter"`` — instead of the native
        # ``stop_reason="refusal"``. Promote it to content + a
        # ``content_filter`` finish reason so the runtime doesn't treat it as
        # an empty response and retry.
        refusal = getattr(msg, "refusal", None)
        if refusal is None and hasattr(msg, "model_extra"):
            refusal = (msg.model_extra or {}).get("refusal")
        finish_reason = choice.finish_reason or "stop"
        if isinstance(refusal, str) and refusal.strip() and not content and not msg.tool_calls:
            content = refusal
            finish_reason = "content_filter"

        tool_calls: list[ToolCallItem] = []
        if msg.tool_calls:
            for tc in msg.tool_calls:
                try:
                    args = json.loads(tc.function.arguments)
                except (json.JSONDecodeError, TypeError):
                    args = {"raw": tc.function.arguments}
                tool_calls.append(ToolCallItem(
                    id=tc.id,
                    name=tc.function.name,
                    arguments=args,
                ))

        return ChatResult(
            role="assistant",
            content=content,
            model=resp.model,
            usage=usage_dict,
            finish_reason=finish_reason,
            tool_calls=tool_calls,
            reasoning_content=reasoning_content,
        )

    async def chat_stream(
        self,
        messages: Iterable[ChatMessage],
        model: str,
        system_prompt: str | None = None,
        tools: list[dict] | None = None,
        thinking_config: dict[str, Any] | None = None,
    ) -> AsyncIterator[StreamDelta]:
        thinking_enabled = thinking_config is not None and thinking_config.get("type") == "enabled"
        api_messages = _build_api_messages(
            messages, system_prompt, model=model, base_url=self._base_url,
            thinking_enabled=thinking_enabled, capabilities=self._capabilities,
        )

        kwargs: dict[str, Any] = {"model": model, "messages": api_messages, "stream": True, "stream_options": {"include_usage": True}}
        # Provider-specific options (e.g. MiniMax reasoning_split) go through
        # extra_body so the OpenAI SDK doesn't reject unknown top-level kwargs.
        extra_body: dict[str, Any] = {}
        for key, val in self._options.items():
            if key in {"model", "messages", "stream", "stream_options", "tools", "tool_choice", "reasoning_effort"}:
                kwargs[key] = val
            else:
                extra_body[key] = val

        if _is_minimax_m3(self._base_url, model):
            extra_body.setdefault("reasoning_split", True)

        if extra_body:
            kwargs["extra_body"] = extra_body
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"

        # Newer OpenAI families require ``max_completion_tokens``; reasoning_effort
        # is only valid for native o-series. See Hermes
        # ``utils.py:model_forces_max_completion_tokens``.
        is_openai_reasoning_model = _is_openai_reasoning_model(model)
        forces_max_completion_tokens = _model_forces_max_completion_tokens(model)
        if self._default_max_tokens:
            if forces_max_completion_tokens:
                kwargs["max_completion_tokens"] = self._default_max_tokens
            else:
                kwargs["max_tokens"] = self._default_max_tokens

        if thinking_config and thinking_config.get("type") == "enabled":
            if is_openai_reasoning_model:
                kwargs["reasoning_effort"] = thinking_config.get("mode", "medium")

        response = await call_with_retry(self._client.chat.completions.create, **kwargs)

        tool_calls_acc: dict[int, dict] = {}

        async for chunk in response:
            # OpenAI sends usage in a final chunk with empty choices
            if not chunk.choices:
                if hasattr(chunk, "usage") and chunk.usage:
                    u = _usage_from_openai(chunk.usage)
                    if u:
                        yield StreamDelta(
                            content="",
                            finish_reason=None,
                            tool_calls=[],
                            usage=u,
                        )
                continue

            choice = chunk.choices[0]
            delta = choice.delta
            finish_reason = choice.finish_reason

            reasoning = _normalize_reasoning(delta)
            content = delta.content or ""

            # Streamed refusal delta (some OpenAI-compatible proxies emit
            # ``delta.refusal`` instead of ``delta.content`` for safety blocks).
            refusal = getattr(delta, "refusal", None)
            if refusal is None and hasattr(delta, "model_extra"):
                refusal = (delta.model_extra or {}).get("refusal")
            if isinstance(refusal, str) and refusal.strip() and not content and not delta.tool_calls:
                content = refusal
                finish_reason = "content_filter"

            if delta.tool_calls:
                for tc_delta in delta.tool_calls:
                    idx = tc_delta.index
                    if idx not in tool_calls_acc:
                        tool_calls_acc[idx] = {"id": "", "name": "", "arguments": ""}
                    if tc_delta.id:
                        tool_calls_acc[idx]["id"] = tc_delta.id
                    if tc_delta.function:
                        if tc_delta.function.name:
                            tool_calls_acc[idx]["name"] = tc_delta.function.name
                        if tc_delta.function.arguments:
                            tool_calls_acc[idx]["arguments"] += tc_delta.function.arguments

            final_tool_calls: list[ToolCallItem] = []
            if finish_reason == "tool_calls" and tool_calls_acc:
                for idx in sorted(tool_calls_acc):
                    tc = tool_calls_acc[idx]
                    try:
                        args = json.loads(tc["arguments"])
                    except (json.JSONDecodeError, TypeError):
                        args = {"raw": tc["arguments"]}
                    final_tool_calls.append(ToolCallItem(
                        id=tc["id"], name=tc["name"], arguments=args,
                    ))

            usage_dict = _usage_from_openai(getattr(chunk, "usage", None))

            yield StreamDelta(
                content=content,
                reasoning_content=reasoning,
                finish_reason=finish_reason,
                tool_calls=final_tool_calls,
                usage=usage_dict,
            )


OpenAIAgentsAdapter = OpenAIChatAdapter
