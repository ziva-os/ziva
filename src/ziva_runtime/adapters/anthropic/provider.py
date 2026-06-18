from __future__ import annotations

import json
import os
from typing import Any, AsyncIterator, Iterable

from ziva_runtime.adapters.retry import call_with_retry
from ziva_runtime.shared_types import ChatMessage, ChatResult, StreamDelta, ToolCallItem


def _build_anthropic_messages(
    messages: Iterable[ChatMessage],
    system_prompt: str | None = None,
    *,
    thinking_enabled: bool = False,
) -> tuple[str | None, list[dict]]:
    """Convert ziva messages to Anthropic API format.

    Returns (system_prompt, api_messages).
    Anthropic uses a top-level system field and content blocks for tool_use/tool_result.
    """
    system = system_prompt
    api_messages: list[dict] = []

    for m in messages:
        role = m.role
        if role == "system":
            continue

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

        if role == "assistant":
            parts: list[dict] = []
            reasoning_content = getattr(m, "reasoning_content", None)
            reasoning_signature = getattr(m, "reasoning_signature", None)

            # Send a thinking block back only when we have a real signature.
            # A fake/placeholder signature triggers Anthropic API rejections on
            # multi-turn thinking; better to drop the block and let the model
            # re-reason than to hard-fail the request.
            if reasoning_content and reasoning_signature:
                parts.append({
                    "type": "thinking",
                    "thinking": reasoning_content,
                    "signature": reasoning_signature,
                })

            if m.content:
                if isinstance(m.content, list):
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

        if isinstance(m.content, list):
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

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        default_max_tokens: int = 16384,
        capabilities: dict | None = None,
    ):
        from anthropic import AsyncAnthropic
        self._api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        # Allow the adapter to be constructed even when no key is configured
        # (e.g. during startup). The actual request will fail with 401 if a
        # real key is never supplied, matching opencode's lazy-fail behavior.
        if not self._api_key:
            self._api_key = "dummy"
        self._base_url = base_url
        self._default_max_tokens = default_max_tokens
        self._capabilities = capabilities or {}
        self._client = AsyncAnthropic(
            api_key=self._api_key, base_url=self._base_url, timeout=120.0
        )

    def _resolve_max_tokens(self, thinking_config: dict[str, Any] | None) -> int:
        if thinking_config and thinking_config.get("max_tokens"):
            return int(thinking_config["max_tokens"])
        return self._default_max_tokens

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

        kwargs: dict[str, Any] = {
            "model": model,
            "messages": api_messages,
            "max_tokens": self._resolve_max_tokens(thinking_config),
        }
        if system:
            kwargs["system"] = system
        if anthropic_tools:
            kwargs["tools"] = anthropic_tools
        if thinking_config:
            kwargs["thinking"] = {"type": "enabled", "budget_tokens": thinking_config.get("budget_tokens", 4000)}

        resp = await call_with_retry(self._client.messages.create, **kwargs)

        content = ""
        reasoning_content = ""
        for b in resp.content:
            if getattr(b, "type", None) == "thinking":
                reasoning_content += getattr(b, "thinking", "")
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
            reasoning_content=reasoning_content,
            reasoning_signature=getattr(resp, "signature", None),
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

        kwargs: dict[str, Any] = {
            "model": model,
            "messages": api_messages,
            "max_tokens": self._resolve_max_tokens(thinking_config),
        }
        if system:
            kwargs["system"] = system
        if anthropic_tools:
            kwargs["tools"] = anthropic_tools
        if thinking_config:
            kwargs["thinking"] = {"type": "enabled", "budget_tokens": thinking_config.get("budget_tokens", 4000)}

        # Wrap only the connection setup (__aenter__) with retry. Once the
        # stream starts yielding, errors propagate — the runtime's
        # turn-level retry loop handles mid-stream failures via stream_reset.
        stream_ctx = self._client.messages.stream(**kwargs)
        stream = await call_with_retry(stream_ctx.__aenter__)
        try:
            tool_calls_acc: dict[int, dict] = {}

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
                        sig = getattr(event.content_block, "signature", None)
                        initial_thinking = getattr(event.content_block, "thinking", "") or ""
                        yield StreamDelta(
                            content="",
                            reasoning_content=initial_thinking,
                            reasoning_signature=sig,
                        )

                elif event.type == "content_block_delta":
                    dtype = getattr(event.delta, "type", None)
                    if dtype == "text_delta" and hasattr(event.delta, "text"):
                        yield StreamDelta(content=event.delta.text)
                    elif dtype == "thinking_delta" and hasattr(event.delta, "thinking"):
                        yield StreamDelta(content="", reasoning_content=event.delta.thinking)
                    elif dtype == "input_json_delta" and hasattr(event.delta, "partial_json"):
                        idx = getattr(event, "index", -1)
                        if idx in tool_calls_acc:
                            tool_calls_acc[idx]["arguments"] += event.delta.partial_json

                elif event.type == "content_block_stop":
                    btype = getattr(event.content_block, "type", None)
                    if btype == "thinking":
                        sig = getattr(event.content_block, "signature", None)
                        if sig:
                            yield StreamDelta(content="", reasoning_signature=sig)

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
        finally:
            await stream_ctx.__aexit__(None, None, None)
