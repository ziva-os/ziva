"""Multi-turn conversation compaction and context window management.

Aligned with aicoder's SessionCompaction approach:
- Two-level strategy: prune protected tool outputs first, then model-based summary.
- Dedicated CompactAgent abstraction for the summarization step.
"""

from __future__ import annotations

import copy as _copy
import dataclasses
from dataclasses import dataclass
from typing import Any, List, Tuple

from ziva_runtime.shared_types import ChatMessage

OVERFLOW_BUFFER = 20_000

# Tools whose outputs should never be pruned (e.g. skill reads are expensive to re-fetch)
PRUNE_PROTECTED_TOOLS: List[str] = ["skill"]

@dataclass
class CompactAgent:
    """Dedicated agent for session compaction / summarization.
    Aligned with aicoder's CompactAgent (AgentType.COMPACT).
    """

    name: str = "compaction"
    system_prompt: str = "You are a helpful AI assistant tasked with summarizing conversations."
    max_iterations: int = 1

    async def run(
        self,
        messages: List[ChatMessage],
        model_name: str,
        model_adapter: Any,
    ) -> str:
        """Generate a compaction summary for the given message history."""
        history_text = _format_history(messages)
        prompt = f"{history_text}\n\n{COMPACTION_TEMPLATE}"
        summary_result = await model_adapter.chat(
            [ChatMessage(role="user", content=prompt)],
            model=model_name,
        )
        return summary_result.content or ""


COMPACTION_TEMPLATE = """请根据上方对话内容生成一份详细的摘要，供后续对话继续使用。
重点关注：我们做了什么、正在做什么、涉及哪些文件、接下来要做什么。

请按以下模板组织摘要：
---
## 目标

[用户想要达成什么目标？]

## 指令

- [用户给出的重要指令]
- [如果有计划或方案，包含相关信息以便后续继续]

## 发现

[对话中发现的值得记录的重要信息]

## 已完成

[哪些工作已完成、哪些正在进行、哪些还未开始？]

## 相关文件 / 目录

[列出对话中读取、编辑或创建的相关文件。如果某个目录下的所有文件都相关，列出目录路径即可。]
---"""


def _text_of(content: str | list) -> str:
    """Extract plain text from message content (str or multi-part list)."""
    if isinstance(content, str):
        return content
    parts = []
    for p in content:
        if isinstance(p, dict) and p.get("type") == "text":
            parts.append(p.get("text", ""))
    return " ".join(parts)


def estimate_tokens(messages: List[ChatMessage]) -> int:
    """Rough token estimate: ~4 chars per token for English, ~2 for CJK."""
    total = 0
    for m in messages:
        text = _text_of(m.content)
        char_count = len(text)
        cjk = sum(1 for c in text if "一" <= c <= "鿿")
        non_cjk = char_count - cjk
        total += int(cjk / 2) + int(non_cjk / 4)
        total += 10
    return total


def is_overflow(messages: List[ChatMessage], context_window: int) -> bool:
    """Check if message history exceeds context window minus buffer."""
    return estimate_tokens(messages) > context_window - OVERFLOW_BUFFER


def prune(messages: List[ChatMessage], keep_last: int = 2) -> List[ChatMessage]:
    """Prune older tool results to reclaim tokens, keeping the last N turns.

    A "turn" boundary is a user message. We keep the last `keep_last` user
    messages and everything after them. For earlier turns, we keep user
    and assistant messages but strip tool role messages to save space.

    Public alias: `prune_messages`. Cheap operation, no model call.
    """
    if not messages:
        return messages

    # Find indices of user messages
    user_indices = [i for i, m in enumerate(messages) if m.role == "user"]
    if len(user_indices) <= keep_last:
        return messages

    # Keep everything from the (keep_last)th-from-last user message onward
    cutoff = user_indices[-keep_last]
    before = messages[:cutoff]
    after = messages[cutoff:]

    # Replace tool output content with a placeholder for earlier turns so the
    # conversation structure (which tool was called, when) stays visible in
    # the UI, but the bulky payload is reclaimed. Protected tools keep their
    # output intact because their content is expensive to refetch.
    pruned_before = []
    for m in before:
        if m.role == "tool":
            if m.name in PRUNE_PROTECTED_TOOLS:
                pruned_before.append(m)
            else:
                pruned_before.append(_pruned_tool_message(m))
        else:
            pruned_before.append(m)

    return pruned_before + after


def _pruned_tool_message(m: Any) -> Any:
    """Return a copy of `m` whose content is collapsed to a placeholder.

    The tool call structure (role, name, tool_call_id) is preserved so the UI
    can still render "tool was called" rows in order. The content is replaced
    with a fixed short string so the LLM doesn't see the full payload on the
    next turn but does know something was returned for that tool call id.
    """
    placeholder = "[pruned]"
    # Prefer dataclasses.replace so we get back the same type with a fresh
    # __dict__. Fall back to a shallow copy, then to a fresh ChatMessage
    # carrying just the identifying fields, then to a dict last resort.
    if dataclasses.is_dataclass(m):
        return dataclasses.replace(m, content=placeholder)
    if hasattr(m, "model_copy"):
        return m.model_copy(update={"content": placeholder})
    try:
        clone = _copy.copy(m)
        clone.content = placeholder
        return clone
    except Exception:
        pass
    try:
        return ChatMessage(
            role=getattr(m, "role", "tool"),
            content=placeholder,
            tool_call_id=getattr(m, "tool_call_id", None),
            name=getattr(m, "name", None),
        )
    except Exception:
        return {"role": "tool", "name": None, "content": placeholder}


# Public alias — matches the naming used by /prune slash command.
prune_messages = prune


def _is_summary_msg(m: Any) -> bool:
    """Return True if the message is a compaction summary, supporting both
    ChatMessage objects and plain dicts (the latter is how messages are stored
    in session metadata and on disk)."""
    if isinstance(m, dict):
        return bool(m.get("_compaction_summary"))
    return bool(getattr(m, "_compaction_summary", False))


def _llm_context(messages: List[Any]) -> List[Any]:
    """Return the messages that should be sent to the LLM as context.

    On-disk layout is chronological:
        [msg1, ..., summary1, msgN, ..., summary2, ...]
    The LLM sees the *last* summary plus any messages after it.
    If there is no summary, the full list is returned.
    """
    # Find the last summary's index
    last_summary_idx = -1
    for i in range(len(messages) - 1, -1, -1):
        if _is_summary_msg(messages[i]):
            last_summary_idx = i
            break
    if last_summary_idx < 0:
        return list(messages)
    return messages[last_summary_idx:]


def _format_history(messages: List[ChatMessage]) -> str:
    """Format messages into a compact history string for the compaction prompt."""
    parts = []
    for m in messages:
        text = _text_of(m.content)
        if m.role == "user":
            parts.append(f"User: {text}")
        elif m.role == "assistant":
            content = text
            if m.tool_calls:
                tc_desc = ", ".join(f"{tc.name}({tc.arguments})" for tc in m.tool_calls)
                content = f"[tool calls: {tc_desc}]"
            if content:
                parts.append(f"Assistant: {content}")
        elif m.role == "tool":
            parts.append(f"Tool ({m.name}): {text[:500]}")
    return "\n\n".join(parts)


async def compact_messages(
    messages: List[ChatMessage],
    context_window: int,
    model_name: str,
    model_adapter: Any,
) -> Tuple[List[ChatMessage], List[ChatMessage]]:
    """Compact message history into a summary.

    The server appends the summary to the on-disk message list in
    chronological order.  The UI shows collapse bars for folded messages,
    and the LLM only sees the last summary + messages after it.

    Returns `(summary_list, [])` — the second element is unused (kept for
    backward compatibility). The summary is appended to the on-disk message
    list by the server's /compact endpoint in chronological order.
    """
    if not messages:
        return ([], [])

    if len(messages) < 3:
        return ([], [])

    try:
        agent = CompactAgent()
        summary = await agent.run(messages, model_name, model_adapter)
    except Exception:
        # Fall back to simple truncation on model failure
        return _simple_compact(messages)

    if not summary.strip():
        return _simple_compact(messages)

    summary_msg = ChatMessage(
        role="assistant",
        content=summary,
        _compaction_summary=True,
    )
    return ([summary_msg], [])


def _simple_compact(messages: List[ChatMessage]) -> Tuple[List[ChatMessage], List[ChatMessage]]:
    """Fallback compaction: truncate each message to 200 chars into one summary."""
    summary_parts = []
    for m in messages:
        text = _text_of(m.content)
        if m.role == "user":
            summary_parts.append(f"User: {text[:200]}")
        elif m.role == "assistant":
            content = text[:200]
            if content:
                summary_parts.append(f"Assistant: {content}")

    summary = "[Earlier conversation summary]\n" + "\n".join(summary_parts)
    summary_msg = ChatMessage(
        role="assistant",
        content=summary,
        _compaction_summary=True,
    )
    return ([summary_msg], [])