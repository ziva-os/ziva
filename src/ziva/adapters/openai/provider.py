from __future__ import annotations

import json
import os
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
    for m in messages:
        msg: dict[str, Any] = {"role": m.role, "content": m.content}

        if m.role == "assistant":
            reasoning = getattr(m, "reasoning_content", None)
            if reasoning:
                if minimax_m3:
                    # MiniMax expects reasoning as a structured array
                    msg["reasoning_details"] = [
                        {"type": "reasoning.text", "text": reasoning}
                    ]
                else:
                    msg["reasoning_content"] = reasoning

        if m.role == "assistant" and m.tool_calls:
            if "reasoning_content" not in msg and (thinking_enabled or supports_thinking):
                msg["reasoning_content"] = ""
            msg["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {"name": tc.name, "arguments": json.dumps(tc.arguments, ensure_ascii=False)},
                }
                for tc in m.tool_calls
            ]

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
    return api_messages


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

        # o1/o3 reasoning models reject `max_tokens` (they use
        # max_completion_tokens / reasoning_effort); skip it for them.
        is_reasoning_model = model.startswith("o1") or model.startswith("o3")
        if self._default_max_tokens and not is_reasoning_model:
            kwargs["max_tokens"] = self._default_max_tokens

        if thinking_config and thinking_config.get("type") == "enabled":
            if is_reasoning_model:
                kwargs["reasoning_effort"] = thinking_config.get("mode", "medium")

        if _is_minimax_m3(self._base_url, model):
            kwargs.setdefault("extra_body", {})
            kwargs["extra_body"].setdefault("reasoning_split", True)

        resp = await call_with_retry(self._client.chat.completions.create, **kwargs)
        choice = resp.choices[0]
        content = choice.message.content or ""
        reasoning_content = _normalize_reasoning(choice.message)
        usage_dict = _usage_from_openai(getattr(resp, "usage", None))

        tool_calls: list[ToolCallItem] = []
        if choice.message.tool_calls:
            for tc in choice.message.tool_calls:
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
            finish_reason=choice.finish_reason or "stop",
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

        # o1/o3 reasoning models reject `max_tokens`; skip it for them.
        is_reasoning_model = model.startswith("o1") or model.startswith("o3")
        if self._default_max_tokens and not is_reasoning_model:
            kwargs["max_tokens"] = self._default_max_tokens

        if thinking_config and thinking_config.get("type") == "enabled":
            if is_reasoning_model:
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
