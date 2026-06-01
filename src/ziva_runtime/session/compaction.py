"""Multi-turn conversation compaction and context window management.

Aligned with aicoder's SessionCompaction approach:
- Two-level strategy: prune protected tool outputs first, then model-based summary.
- Dedicated CompactAgent abstraction for the summarization step.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, List

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


def estimate_tokens(messages: List[ChatMessage]) -> int:
    """Rough token estimate: ~4 chars per token for English, ~2 for CJK."""
    total = 0
    for m in messages:
        # Count content characters
        char_count = len(m.content)
        # Rough heuristic: mix of CJK detection
        cjk = sum(1 for c in m.content if "一" <= c <= "鿿")
        non_cjk = char_count - cjk
        total += int(cjk / 2) + int(non_cjk / 4)
        # Each message has overhead (role, metadata)
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

    # Strip tool messages from earlier part, but protect certain tools
    pruned_before = []
    for m in before:
        if m.role == "tool" and m.name in PRUNE_PROTECTED_TOOLS:
            pruned_before.append(m)
        elif m.role != "tool":
            pruned_before.append(m)

    return pruned_before + after


def _skip_before_summary(messages: List[ChatMessage]) -> List[ChatMessage]:
    """Return messages starting from the last compaction summary (inclusive).
    All messages before the last summary are dropped — they've been compacted."""
    last_summary_idx = None
    for i in range(len(messages) - 1, -1, -1):
        if messages[i]._compaction_summary:
            last_summary_idx = i
            break
    if last_summary_idx is not None:
        return messages[last_summary_idx:]
    return messages


def _format_history(messages: List[ChatMessage]) -> str:
    """Format messages into a compact history string for the compaction prompt."""
    parts = []
    for m in messages:
        if m.role == "user":
            parts.append(f"User: {m.content}")
        elif m.role == "assistant":
            content = m.content
            if m.tool_calls:
                tc_desc = ", ".join(f"{tc.name}({tc.arguments})" for tc in m.tool_calls)
                content = f"[tool calls: {tc_desc}]"
            if content:
                parts.append(f"Assistant: {content}")
        elif m.role == "tool":
            parts.append(f"Tool ({m.name}): {m.content[:500]}")
    return "\n\n".join(parts)


def _simple_compact(messages: List[ChatMessage]) -> List[ChatMessage]:
    """Fallback compaction: truncate each message to 200 chars."""
    if len(messages) < 3:
        return messages

    last_user_idx = None
    for i in range(len(messages) - 1, -1, -1):
        if messages[i].role == "user":
            last_user_idx = i
            break

    if last_user_idx is None or last_user_idx == 0:
        return messages

    older = messages[:last_user_idx]
    summary_parts = []
    for m in older:
        if m.role == "user":
            summary_parts.append(f"User: {m.content[:200]}")
        elif m.role == "assistant":
            content = m.content[:200]
            if content:
                summary_parts.append(f"Assistant: {content}")

    summary = "[Earlier conversation summary]\n" + "\n".join(summary_parts)
    summary_msg = ChatMessage(role="system", content=summary)

    recent = messages[last_user_idx:]
    return [summary_msg] + recent


async def compact_messages(
    messages: List[ChatMessage],
    context_window: int,
    model_name: str,
    model_adapter: Any,
) -> List[ChatMessage]:
    """Compact message history if it exceeds the context window.

    Strategy:
    1. First try pruning tool outputs from old turns
    2. If still over limit, use the model to generate a structured summary
       of older messages, framed as a user/assistant pair.
    3. If the model call fails, fall back to simple truncation.
    """
    if not is_overflow(messages, context_window):
        return messages

    # Step 1: Prune old tool outputs
    pruned = prune(messages)
    if not is_overflow(pruned, context_window):
        return pruned

    # Step 2: Model-based compaction
    if len(pruned) < 3:
        return pruned

    # Find the last user message
    last_user_idx = None
    for i in range(len(pruned) - 1, -1, -1):
        if pruned[i].role == "user":
            last_user_idx = i
            break

    if last_user_idx is None or last_user_idx == 0:
        return pruned

    older = pruned[:last_user_idx]

    try:
        agent = CompactAgent()
        summary = await agent.run(older, model_name, model_adapter)
    except Exception:
        # Fall back to simple truncation on model failure
        return _simple_compact(pruned)

    if not summary.strip():
        return _simple_compact(pruned)

    # Frame summary as a user/assistant pair for better LLM continuity
    framed_user = (
        "[本次会话从之前的对话继续，之前的对话因上下文长度限制已被压缩。]\n\n"
        f"以下是之前对话的摘要：\n\n{summary}"
    )
    framed_assistant = "好的，我已了解之前的对话内容，将从中断处继续。"

    recent = pruned[last_user_idx:]
    return [
        ChatMessage(role="user", content=framed_user, _compaction_summary=True),
        ChatMessage(role="assistant", content=framed_assistant),
    ] + recent
