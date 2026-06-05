from __future__ import annotations

import json
import os
from typing import Any, AsyncIterator, Iterable

from ziva_runtime.shared_types import ChatMessage, ChatResult, StreamDelta, ToolCallItem


def _build_anthropic_messages(
    messages: Iterable[ChatMessage], system_prompt: str | None = None
) -> tuple[str | None, list[dict]]:
    """Convert ziva messages to Anthropic API format.

    Returns (system_prompt, api_messages).
    Anthropic uses a top-level system field and content blocks for tool_use/tool_result.
    """
    system = system_prompt
    api_messages: list[dict] = []

    for m in messages:
        role = m.role
        # Anthropic doesn't have system role messages; skip (handled by top-level system)
        if role == "system":
            continue

        # tool role → user message with tool_result content block
        if role == "tool":
            content: list[dict] = [
                {
                    "type": "tool_result",
                    "tool_use_id": m.tool_call_id or "",
                    "content": m.content if isinstance(m.content, str) else str(m.content),
                }
            ]
            api_messages.append({"role": "user", "content": content})
            continue

        msg: dict[str, Any] = {"role": role}

        # assistant with tool_calls
        if role == "assistant" and m.tool_calls:
            parts: list[dict] = []
            if m.content:
                text = m.content if isinstance(m.content, str) else str(m.content)
                if text:
                    parts.append({"type": "text", "text": text})
            for tc in m.tool_calls:
                parts.append({
                    "type": "tool_use",
                    "id": tc.id,
                    "name": tc.name,
                    "input": tc.arguments,
                })
            msg["content"] = parts
            api_messages.append(msg)
            continue

        # regular user/assistant
        if isinstance(m.content, list):
            # Multi-part content (e.g. image_url blocks)
            parts = []
            for block in m.content:
                if isinstance(block, dict):
                    if block.get("type") == "text":
                        parts.append({"type": "text", "text": block.get("text", "")})
                    elif block.get("type") == "image_url":
                        url = block.get("image_url", {})
                        if isinstance(url, dict):
                            url_str = url.get("url", "")
                        else:
                            url_str = str(url)
                        # Convert data URL to Anthropic base64 format
                        if url_str.startswith("data:"):
                            header, data = url_str.split(",", 1)
                            media_type = header.split(";")[0].split(":")[1]
                            parts.append({
                                "type": "image",
                                "source": {"type": "base64", "media_type": media_type, "data": data},
                            })
                        else:
                            parts.append({"type": "text", "text": url_str})
                else:
                    parts.append({"type": "text", "text": str(block)})
            msg["content"] = parts
        else:
            msg["content"] = m.content

        api_messages.append(msg)

    return system, api_messages


def _convert_tools_to_anthropic(tools: list[dict] | None) -> list[dict] | None:
    """Convert OpenAI-style tool definitions to Anthropic format."""
    if not tools:
        return None
    anthropic_tools = []
    for t in tools:
        func = t.get("function", t)
        anthropic_tools.append({
            "name": func.get("name", ""),
            "description": func.get("description", ""),
            "input_schema": func.get("parameters", func.get("input_schema", {"type": "object", "properties": {}})),
        })
    return anthropic_tools


class AnthropicChatAdapter:
    """Adapter using anthropic SDK with native tool calling."""

    def __init__(self, api_key: str | None = None, base_url: str | None = None):
        self._api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        self._base_url = base_url

    async def chat(
        self,
        messages: Iterable[ChatMessage],
        model: str,
        system_prompt: str | None = None,
        tools: list[dict] | None = None,
    ) -> ChatResult:
        from anthropic import AsyncAnthropic

        client = AsyncAnthropic(api_key=self._api_key, base_url=self._base_url)
        system, api_messages = _build_anthropic_messages(messages, system_prompt)
        anthropic_tools = _convert_tools_to_anthropic(tools)

        kwargs: dict[str, Any] = {"model": model, "messages": api_messages, "max_tokens": 16384}
        if system:
            kwargs["system"] = system
        if anthropic_tools:
            kwargs["tools"] = anthropic_tools

        resp = await client.messages.create(**kwargs)

        text_parts = [b.text for b in resp.content if b.type == "text"]
        content = "".join(text_parts)

        usage_dict = None
        if resp.usage:
            usage_dict = {
                "prompt_tokens": resp.usage.input_tokens,
                "completion_tokens": resp.usage.output_tokens,
            }

        tool_calls = []
        for b in resp.content:
            if b.type == "tool_use":
                tool_calls.append(ToolCallItem(
                    id=b.id,
                    name=b.name,
                    arguments=b.input if isinstance(b.input, dict) else {"raw": str(b.input)},
                ))

        return ChatResult(
            role="assistant",
            content=content,
            model=resp.model,
            usage=usage_dict,
            finish_reason=resp.stop_reason or "stop",
            tool_calls=tool_calls,
        )

    async def chat_stream(
        self,
        messages: Iterable[ChatMessage],
        model: str,
        system_prompt: str | None = None,
        tools: list[dict] | None = None,
    ) -> AsyncIterator[StreamDelta]:
        from anthropic import AsyncAnthropic

        client = AsyncAnthropic(api_key=self._api_key, base_url=self._base_url)
        system, api_messages = _build_anthropic_messages(messages, system_prompt)
        anthropic_tools = _convert_tools_to_anthropic(tools)

        kwargs: dict[str, Any] = {"model": model, "messages": api_messages, "max_tokens": 16384}
        if system:
            kwargs["system"] = system
        if anthropic_tools:
            kwargs["tools"] = anthropic_tools

        async with client.messages.stream(**kwargs) as stream:
            tool_calls_acc: dict[int, dict] = {}

            async for event in stream:
                if event.type == "content_block_delta":
                    if hasattr(event.delta, "text"):
                        yield StreamDelta(content=event.delta.text, finish_reason=None, tool_calls=[], usage=None)
                    elif hasattr(event.delta, "partial_json"):
                        idx = event.index
                        if idx in tool_calls_acc:
                            tool_calls_acc[idx]["arguments"] += event.delta.partial_json

                elif event.type == "content_block_start":
                    if hasattr(event.content_block, "type") and event.content_block.type == "tool_use":
                        tool_calls_acc[event.index] = {
                            "id": event.content_block.id,
                            "name": event.content_block.name,
                            "arguments": "",
                        }

                elif event.type == "message_delta":
                    finish_reason = event.delta.stop_reason if hasattr(event.delta, "stop_reason") else None
                    usage_dict = None
                    if hasattr(event, "usage") and event.usage:
                        usage_dict = {
                            "prompt_tokens": event.usage.input_tokens,
                            "completion_tokens": event.usage.output_tokens,
                        }

                    final_tool_calls = []
                    if finish_reason == "tool_use" and tool_calls_acc:
                        for idx in sorted(tool_calls_acc):
                            tc = tool_calls_acc[idx]
                            try:
                                args = json.loads(tc["arguments"])
                            except (json.JSONDecodeError, TypeError):
                                args = {"raw": tc["arguments"]}
                            final_tool_calls.append(ToolCallItem(
                                id=tc["id"], name=tc["name"], arguments=args,
                            ))

                    yield StreamDelta(
                        content="",
                        finish_reason=finish_reason,
                        tool_calls=final_tool_calls,
                        usage=usage_dict,
                    )

                elif event.type == "message_start":
                    if hasattr(event.message, "usage") and event.message.usage:
                        yield StreamDelta(
                            content="",
                            finish_reason=None,
                            tool_calls=[],
                            usage={
                                "prompt_tokens": event.message.usage.input_tokens,
                                "completion_tokens": 0,
                            },
                        )
