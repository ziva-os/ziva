from __future__ import annotations

import json
import os
from typing import Any, AsyncIterator, Iterable

from ziva_runtime.shared_types import ChatMessage, ChatResult, StreamDelta, ToolCallItem


def _build_anthropic_messages(
    messages: Iterable[ChatMessage], system_prompt: str | None = None, *, thinking_enabled: bool = False
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

        # assistant with tool_calls or extended thinking
        if role == "assistant":
            parts: list[dict] = []
            
            # Extract thinking blocks if extended thinking signature is present OR if we have thinking tags
            import re
            content_str = m.content if isinstance(m.content, str) else str(m.content)
            thinking_matches = re.findall(r'(?i)<think[^>]*>([\s\S]*?)</think[^>]*>', content_str) if isinstance(m.content, str) else []
            
            if thinking_matches or (hasattr(m, "reasoning_signature") and m.reasoning_signature):
                main_text = re.sub(r'(?i)<think[^>]*>[\s\S]*?</think[^>]*>', '', content_str).strip() if isinstance(m.content, str) else content_str
                thinking_text = "\n\n---\n\n".join([t.strip() for t in thinking_matches]) if thinking_matches else ""
                
                parts.append({
                    "type": "thinking",
                    "thinking": thinking_text,
                    "signature": getattr(m, "reasoning_signature", None) or "signature",
                })
                if main_text:
                    parts.append({"type": "text", "text": main_text})
            else:
                # Only inject a synthetic thinking block when extended thinking
                # is actually enabled for this request. Without it, the API
                # rejects the message for having a thinking block without
                # thinking.type="enabled" in the parameters.
                if thinking_enabled and m.tool_calls:
                    parts.append({
                        "type": "thinking",
                        "thinking": " ",
                        "signature": "signature",
                    })
                if m.content:
                    if isinstance(m.content, list):
                        # Multi-part content for assistant is unlikely but possible
                        for block in m.content:
                            if isinstance(block, dict) and block.get("type") == "text":
                                parts.append({"type": "text", "text": block.get("text", "")})
                            else:
                                parts.append({"type": "text", "text": str(block)})
                    else:
                        text = str(m.content)
                        if text:
                            parts.append({"type": "text", "text": text})

            if m.tool_calls:
                for tc in m.tool_calls:
                    parts.append({
                        "type": "tool_use",
                        "id": tc.id,
                        "name": tc.name,
                        "input": tc.arguments,
                    })

            if parts:
                msg["content"] = parts
            else:
                msg["content"] = ""
            api_messages.append(msg)
            continue

        # regular user (assistant is handled above)
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
        from anthropic import AsyncAnthropic
        self._api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        self._base_url = base_url
        self._client = AsyncAnthropic(api_key=self._api_key, base_url=self._base_url)

    async def chat(
        self,
        messages: Iterable[ChatMessage],
        model: str,
        system_prompt: str | None = None,
        tools: list[dict] | None = None,
        thinking_config: dict[str, Any] | None = None,
    ) -> ChatResult:
        system, api_messages = _build_anthropic_messages(messages, system_prompt, thinking_enabled=bool(thinking_config))
        anthropic_tools = _convert_tools_to_anthropic(tools)

        kwargs: dict[str, Any] = {"model": model, "messages": api_messages, "max_tokens": 16384}
        if system:
            kwargs["system"] = system
        if anthropic_tools:
            kwargs["tools"] = anthropic_tools
        if thinking_config:
            kwargs["thinking"] = {"type": "enabled", "budget_tokens": thinking_config.get("budget_tokens", 4000)}

        resp = await self._client.messages.create(**kwargs)

        content = ""
        for b in resp.content:
            if getattr(b, "type", None) == "thinking":
                content += f"<think>\n{getattr(b, 'thinking', '')}\n</think>\n"
            elif getattr(b, "type", None) == "text":
                content += getattr(b, "text", "")

        usage_dict = None
        if resp.usage:
            usage_dict = {
                "prompt_tokens": resp.usage.input_tokens,
                "completion_tokens": resp.usage.output_tokens,
            }

        tool_calls = []
        for b in resp.content:
            if getattr(b, "type", None) == "tool_use":
                tool_calls.append(ToolCallItem(
                    id=getattr(b, "id", ""),
                    name=getattr(b, "name", ""),
                    arguments=getattr(b, "input", {}) if isinstance(getattr(b, "input", None), dict) else {"raw": str(getattr(b, "input", ""))},
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
        thinking_config: dict[str, Any] | None = None,
    ) -> AsyncIterator[StreamDelta]:
        system, api_messages = _build_anthropic_messages(messages, system_prompt, thinking_enabled=bool(thinking_config))
        anthropic_tools = _convert_tools_to_anthropic(tools)

        kwargs: dict[str, Any] = {"model": model, "messages": api_messages, "max_tokens": 16384}
        if system:
            kwargs["system"] = system
        if anthropic_tools:
            kwargs["tools"] = anthropic_tools
        if thinking_config:
            kwargs["thinking"] = {"type": "enabled", "budget_tokens": thinking_config.get("budget_tokens", 4000)}

        async with self._client.messages.stream(**kwargs) as stream:
            tool_calls_acc: dict[int, dict] = {}
            thinking_blocks = set()

            async for event in stream:
                if event.type == "content_block_start":
                    btype = getattr(event.content_block, "type", None)
                    if btype == "tool_use":
                        tool_calls_acc[event.index] = {
                            "id": getattr(event.content_block, "id", ""),
                            "name": getattr(event.content_block, "name", ""),
                            "arguments": "",
                        }
                    elif btype == "thinking":
                        thinking_blocks.add(event.index)
                        sig = getattr(event.content_block, "signature", None)
                        yield StreamDelta(content="<think>\n", finish_reason=None, tool_calls=[], usage=None, reasoning_signature=sig)

                elif event.type == "content_block_delta":
                    dtype = getattr(event.delta, "type", None)
                    if dtype == "text_delta" and hasattr(event.delta, "text"):
                        yield StreamDelta(content=event.delta.text, finish_reason=None, tool_calls=[], usage=None)
                    elif dtype == "thinking_delta" and hasattr(event.delta, "thinking"):
                        yield StreamDelta(content=event.delta.thinking, finish_reason=None, tool_calls=[], usage=None)
                    elif dtype == "input_json_delta" and hasattr(event.delta, "partial_json"):
                        idx = getattr(event, "index", -1)
                        if idx in tool_calls_acc:
                            tool_calls_acc[idx]["arguments"] += event.delta.partial_json

                elif event.type == "content_block_stop":
                    idx = getattr(event, "index", -1)
                    if idx in thinking_blocks:
                        yield StreamDelta(content="\n</think>\n", finish_reason=None, tool_calls=[], usage=None)

                elif event.type == "message_delta":
                    finish_reason = event.delta.stop_reason if hasattr(event.delta, "stop_reason") else None
                    usage_dict = None
                    usage = getattr(event, "usage", None)
                    if usage:
                        input_tokens = getattr(usage, "input_tokens", 0) or 0
                        output_tokens = getattr(usage, "output_tokens", 0) or 0
                        if input_tokens or output_tokens:
                            usage_dict = {
                                "prompt_tokens": input_tokens,
                                "completion_tokens": output_tokens,
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
                    msg_usage = getattr(getattr(event, "message", None), "usage", None)
                    if msg_usage:
                        input_tokens = getattr(msg_usage, "input_tokens", 0) or 0
                        if input_tokens:
                            yield StreamDelta(
                                content="",
                                finish_reason=None,
                                tool_calls=[],
                                usage={
                                    "prompt_tokens": input_tokens,
                                },
                            )
