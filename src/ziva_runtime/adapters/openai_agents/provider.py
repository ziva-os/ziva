from __future__ import annotations

import json
import os
from typing import Any, AsyncIterator, Iterable, Protocol

from ziva_runtime.shared_types import ChatMessage, ChatResult, StreamDelta, ToolCallItem


def _build_api_messages(messages: Iterable[ChatMessage], system_prompt: str | None = None) -> list[dict]:
    api_messages: list[dict] = []
    if system_prompt:
        api_messages.append({"role": "system", "content": system_prompt})
    for m in messages:
        msg: dict[str, Any] = {"role": m.role, "content": m.content}
        if m.role == "assistant" and m.tool_calls:
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
    ) -> AsyncIterator[StreamDelta]: ...


class OpenAIChatAdapter:
    """Adapter using openai SDK Chat Completions with native function calling."""

    def __init__(self, base_url: str | None = None, api_key: str | None = None):
        self._base_url = base_url or os.environ.get("OPENAI_BASE_URL")
        self._api_key = api_key or os.environ.get("OPENAI_API_KEY")

    async def chat(
        self,
        messages: Iterable[ChatMessage],
        model: str,
        system_prompt: str | None = None,
        tools: list[dict] | None = None,
    ) -> ChatResult:
        from openai import AsyncOpenAI

        client = AsyncOpenAI(base_url=self._base_url, api_key=self._api_key)
        api_messages = _build_api_messages(messages, system_prompt)

        kwargs: dict[str, Any] = {"model": model, "messages": api_messages}
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"

        resp = await client.chat.completions.create(**kwargs)
        choice = resp.choices[0]
        content = choice.message.content or ""
        usage_dict = None
        if resp.usage:
            usage_dict = {
                "prompt_tokens": resp.usage.prompt_tokens,
                "completion_tokens": resp.usage.completion_tokens,
            }

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
    ) -> AsyncIterator[StreamDelta]:
        from openai import AsyncOpenAI

        client = AsyncOpenAI(base_url=self._base_url, api_key=self._api_key)
        api_messages = _build_api_messages(messages, system_prompt)

        kwargs: dict[str, Any] = {"model": model, "messages": api_messages, "stream": True, "stream_options": {"include_usage": True}}
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"

        response = await client.chat.completions.create(**kwargs)

        tool_calls_acc: dict[int, dict] = {}

        async for chunk in response:
            # OpenAI sends usage in a final chunk with empty choices
            if not chunk.choices:
                if hasattr(chunk, "usage") and chunk.usage:
                    yield StreamDelta(
                        content="",
                        finish_reason=None,
                        tool_calls=[],
                        usage={
                            "prompt_tokens": chunk.usage.prompt_tokens,
                            "completion_tokens": chunk.usage.completion_tokens,
                        },
                    )
                continue

            choice = chunk.choices[0]
            delta = choice.delta
            content = delta.content or ""
            finish_reason = choice.finish_reason

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

            yield StreamDelta(
                content=content,
                finish_reason=finish_reason,
                tool_calls=final_tool_calls,
                usage=usage_dict,
            )


OpenAIAgentsAdapter = OpenAIChatAdapter
