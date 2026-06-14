from __future__ import annotations

import json
import os
from typing import Any, AsyncIterator, Iterable, Protocol

from ziva_runtime.adapters.retry import call_with_retry
from ziva_runtime.shared_types import ChatMessage, ChatResult, StreamDelta, ToolCallItem


def _build_api_messages(
    messages: Iterable[ChatMessage],
    system_prompt: str | None = None,
    model: str = "",
    thinking_enabled: bool = False,
    capabilities: dict | None = None,
) -> list[dict]:
    api_messages: list[dict] = []
    if system_prompt:
        api_messages.append({"role": "system", "content": system_prompt})
    supports_thinking = bool(capabilities and capabilities.get("thinking"))
    for m in messages:
        msg: dict[str, Any] = {"role": m.role, "content": m.content}

        if m.role == "assistant":
            reasoning = getattr(m, "reasoning_content", None)
            if reasoning:
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
        if m.role == "tool" and m.tool_call_id:
            msg["tool_call_id"] = m.tool_call_id
        if m.role == "tool" and m.name:
            msg["name"] = m.name
        api_messages.append(msg)
    return api_messages


class _ThinkTagParser:
    """Streaming parser that splits provider text into main/reasoning.

    Some OpenAI-compatible providers (e.g. MiniMax) emit the model's
    chain-of-thought wrapped in ``<think>...</think>`` tags inside the
    normal ``content`` delta instead of using the native
    ``reasoning_content`` field. This parser extracts that text and routes
    it to ``reasoning_content`` so the frontend renders it in the thinking
    card, leaving only the final answer in ``content``.
    """

    def __init__(self) -> None:
        self._in_think = False
        self._buffer = ""

    def feed(self, text: str) -> tuple[str, str]:
        """Return (reasoning_content, main_content) for the incoming chunk."""
        text = self._buffer + text
        self._buffer = ""
        if not text:
            return "", ""

        reasoning_parts: list[str] = []
        main_parts: list[str] = []

        while text:
            if self._in_think:
                end = text.find("</think>")
                if end == -1:
                    reasoning_parts.append(text)
                    return "".join(reasoning_parts), ""
                reasoning_parts.append(text[:end])
                text = text[end + len("</think>"):]
                self._in_think = False
            else:
                start = text.find("<think>")
                if start == -1:
                    main_parts.append(text)
                    return "".join(reasoning_parts), "".join(main_parts)
                main_parts.append(text[:start])
                text = text[start + len("<think>"):]
                self._in_think = True

        return "".join(reasoning_parts), "".join(main_parts)

    def flush(self) -> tuple[str, str]:
        """Return any remaining buffered text as main content."""
        text = self._buffer
        self._buffer = ""
        if self._in_think:
            return text, ""
        return "", text


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
            messages, system_prompt, model=model,
            thinking_enabled=thinking_enabled, capabilities=self._capabilities,
        )

        kwargs: dict[str, Any] = {"model": model, "messages": api_messages}
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"

        if thinking_config and thinking_config.get("type") == "enabled":
            if model.startswith("o1") or model.startswith("o3"):
                kwargs["reasoning_effort"] = thinking_config.get("mode", "medium")

        resp = await call_with_retry(self._client.chat.completions.create, **kwargs)
        choice = resp.choices[0]
        content = choice.message.content or ""
        usage_dict = None
        if resp.usage:
            usage_dict = {
                "prompt_tokens": resp.usage.prompt_tokens,
                "completion_tokens": resp.usage.completion_tokens,
            }
            if hasattr(resp.usage, "completion_tokens_details") and resp.usage.completion_tokens_details:
                if hasattr(resp.usage.completion_tokens_details, "reasoning_tokens"):
                    usage_dict["reasoning_tokens"] = resp.usage.completion_tokens_details.reasoning_tokens

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
            messages, system_prompt, model=model,
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
        if extra_body:
            kwargs["extra_body"] = extra_body
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"

        if thinking_config and thinking_config.get("type") == "enabled":
            if model.startswith("o1") or model.startswith("o3"):
                kwargs["reasoning_effort"] = thinking_config.get("mode", "medium")

        response = await call_with_retry(self._client.chat.completions.create, **kwargs)

        tool_calls_acc: dict[int, dict] = {}
        think_parser = _ThinkTagParser()

        async for chunk in response:
            # OpenAI sends usage in a final chunk with empty choices
            if not chunk.choices:
                if hasattr(chunk, "usage") and chunk.usage:
                    u = {
                        "prompt_tokens": chunk.usage.prompt_tokens,
                        "completion_tokens": chunk.usage.completion_tokens,
                    }
                    if hasattr(chunk.usage, "completion_tokens_details") and chunk.usage.completion_tokens_details:
                        if hasattr(chunk.usage.completion_tokens_details, "reasoning_tokens"):
                            u["reasoning_tokens"] = chunk.usage.completion_tokens_details.reasoning_tokens
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

            reasoning = getattr(delta, "reasoning_content", None)
            content = delta.content or ""

            # Fall back to <think> tag parsing for providers that embed
            # reasoning in content instead of the reasoning_content field.
            if not reasoning and content:
                parsed_reasoning, content = think_parser.feed(content)
                if parsed_reasoning:
                    reasoning = parsed_reasoning

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

            usage_dict = None
            if hasattr(chunk, "usage") and chunk.usage:
                usage_dict = {
                    "prompt_tokens": chunk.usage.prompt_tokens,
                    "completion_tokens": chunk.usage.completion_tokens,
                }
                if hasattr(chunk.usage, "completion_tokens_details") and chunk.usage.completion_tokens_details:
                    if hasattr(chunk.usage.completion_tokens_details, "reasoning_tokens"):
                        usage_dict["reasoning_tokens"] = chunk.usage.completion_tokens_details.reasoning_tokens

            yield StreamDelta(
                content=content,
                reasoning_content=reasoning,
                finish_reason=finish_reason,
                tool_calls=final_tool_calls,
                usage=usage_dict,
            )


OpenAIAgentsAdapter = OpenAIChatAdapter
