"""Multi-turn conversation compaction and context window management.

Aligned with aicoder's SessionCompaction approach:
- Two-level strategy: prune protected tool outputs first, then model-based summary.
- Dedicated CompactAgent abstraction for the summarization step.
"""

from __future__ import annotations

import copy as _copy
import dataclasses
from dataclasses import dataclass
from typing import Any, List

from ziva.shared_types import ChatMessage

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
        lang: str = "zh",
    ) -> str:
        """Generate a compaction summary for the given message history."""
        history_text = _format_history(messages)
        template = COMPACTION_TEMPLATE if lang != "en" else COMPACTION_TEMPLATE_EN
        prompt = f"{history_text}\n\n{template}"
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

COMPACTION_TEMPLATE_EN = """Based on the conversation above, generate a detailed summary for continuing the work later.
Focus on: what was done, what is in progress, which files are involved, and what to do next.

Organize the summary using the following template:
---
## Goal

[What does the user want to achieve?]

## Instructions

- [Important instructions from the user]
- [If there is a plan or approach, include relevant info for continuity]

## Findings

[Important information discovered during the conversation]

## Completed

[What work is done, what is in progress, what has not started yet?]

## Related Files / Directories

[List files that were read, edited, or created. If all files in a directory are relevant, listing the directory path is sufficient.]
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


def prune(messages: List[ChatMessage], keep_last: int = 2) -> List[ChatMessage]:
    """Prune older tool results to reclaim tokens, keeping the last N turns.

    A "turn" = user msg → (asst or asst-with-tool_call) → tool_result. The
    turn boundary is the next `role == "user"` message. We keep the last
    `keep_last` user messages and everything after them (which includes the
    associated tool results and assistant responses). For earlier turns, we
    keep user and assistant messages but strip tool role messages to save
    space.

    INVARIANT: the turn boundary detection relies on the internal
    `role="tool"` distinction. If a future change serializes tool results
    as `role="user"` (to match the Anthropic API), the boundary logic will
    split one turn in half. Keep this comment in sync with the API adapter.

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

    The placeholder includes the tool name so the model can still reason
    about what kind of output was there (e.g. "this was a grep result"
    vs "this was a file read") without the model having to re-call the
    tool. Format: "[old tool result pruned — tool: <name>]"
    """
    tool_name = getattr(m, "name", None) or "unknown"
    placeholder = f"[old tool result pruned — tool: {tool_name}]"
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
    keep_last_assistant_turns: int = 5,
    lang: str = "zh",
) -> List[ChatMessage]:
    """Compact message history into a summary, keeping the last K model-call
    cycles verbatim.

    The unit of K is **assistant turns** (= one model call, optionally
    followed by 0+ tool_result messages). Counting by assistant turns (not
    user messages) matters because a single user message can produce many
    model calls when the agent loops with tools — K=5 user messages might
    preserve 50+ messages, K=5 assistant turns preserves a much tighter
    window of recent context.

    Splits `messages` at the K-th-from-last assistant message. Everything
    before the split point gets summarized into a single summary message;
    everything from the split onward is preserved verbatim. Returns:

        [new_summary, ...to_keep]    if a split was possible
        messages                     if there are <= K asst turns (no-op)

    The no-op return value is intentionally the same list reference so the
    caller can detect "nothing happened" via `result is messages`.

    The on-disk layout becomes:  [preserved_old, new_summary, ...to_keep]
    where `preserved_old` is whatever the caller had on disk before the new
    summary. The LLM sees only `[new_summary, ...to_keep]` (via `_llm_context`).

    INVARIANT: assistant-turn counting works because the internal
    `role="assistant"` messages correspond 1:1 to model calls. If a future
    change ever rolls multiple asst messages into one, this logic will
    under-count. Also note: the preceding user message is naturally included
    in `to_keep` only if it was sent *during* the last K asst turns — older
    user messages end up in the summary.
    """
    if not messages or len(messages) < 3:
        return messages

    # Count assistant messages (= model-call cycles in our internal model).
    # Each cycle is `asst_msg [+ tool_result...]` and counts as 1.
    asst_indices = [i for i, m in enumerate(messages) if m.role == "assistant"]
    if len(asst_indices) <= keep_last_assistant_turns:
        return messages

    cutoff = asst_indices[-keep_last_assistant_turns]
    to_summarize = messages[:cutoff]
    to_keep = messages[cutoff:]

    if not to_summarize:
        return messages

    try:
        agent = CompactAgent()
        summary = await agent.run(to_summarize, model_name, model_adapter, lang=lang)
    except Exception:
        return _simple_compact_split(to_summarize, to_keep)

    if not summary.strip():
        return _simple_compact_split(to_summarize, to_keep)

    summary_msg = ChatMessage(
        role="user",
        content=summary,
        _compaction_summary=True,
    )
    return [summary_msg] + to_keep


def _simple_compact_split(
    to_summarize: List[ChatMessage],
    to_keep: List[ChatMessage],
) -> List[ChatMessage]:
    """Fallback for compact_messages: truncate each pre-cutoff message to 200 chars,
    preserve the post-cutoff tail verbatim."""
    summary_parts = []
    for m in to_summarize:
        text = _text_of(m.content)
        if m.role == "user":
            summary_parts.append(f"User: {text[:200]}")
        elif m.role == "assistant":
            content = text[:200]
            if content:
                summary_parts.append(f"Assistant: {content}")

    summary = "[Earlier conversation summary]\n" + "\n".join(summary_parts)
    return [ChatMessage(
        role="user",
        content=summary,
        _compaction_summary=True,
    )] + to_keep


def compose_post_compact_on_disk(
    current_on_disk: List[Any],
    last_summary_idx: int,
    cutoff_in_llm_visible: int,
    new_working: List[Any],
) -> List[Any]:
    """Compose the new on-disk list after a successful compact.

    The on-disk layout is:  [preserved_old, new_summary, ...to_keep]
    where:
      - `preserved_old`  = records in `current_on_disk` that come *before* the
                            first message of `to_keep` (i.e. everything that was
                            already on disk and is not part of the new tail).
                            This naturally accumulates across multiple compacts
                            so the user can still scroll back to ancient history.
      - `new_summary`    = `new_working[0]` (the freshly produced summary).
      - `to_keep`        = `new_working[1:]` (the last K asst-turn cycles verbatim).

    The caller is responsible for ensuring `new_working` is in the same form
    as `current_on_disk` (both dicts, or both ChatMessage) — the helper just
    concatenates.

    `last_summary_idx` is the position of the last summary in
    `current_on_disk`, or -1 if none. `cutoff_in_llm_visible` is the
    K-th-from-last user message position in the LLM-visible view (= position
    in the on-disk array where `to_keep` starts, relative to the start of the
    LLM-visible portion).

    On the very first compact, `last_summary_idx == -1` and `to_keep` starts
    at `cutoff_in_llm_visible` in the on-disk array (= same position, since
    the LLM-visible view starts at the beginning when no summary exists).
    """
    if cutoff_in_llm_visible < 0:
        return list(current_on_disk) + list(new_working)
    to_keep_start = (last_summary_idx + cutoff_in_llm_visible) if last_summary_idx >= 0 else cutoff_in_llm_visible
    preserved_old = list(current_on_disk[:to_keep_start])
    return preserved_old + list(new_working)


def find_last_summary_idx(records: List[Any]) -> int:
    """Return the position of the last summary in `records`, or -1 if none.

    Accepts both dicts (FileStorage format) and ChatMessage instances.
    """
    for i in range(len(records) - 1, -1, -1):
        r = records[i]
        if isinstance(r, dict):
            if r.get("_compaction_summary"):
                return i
        else:
            if getattr(r, "_compaction_summary", False):
                return i
    return -1


def find_cutoff_in_llm_visible(working: List[ChatMessage], keep_last_assistant_turns: int) -> int:
    """Return the cutoff position in `working` that keeps the last K asst
    turns verbatim.

    "Assistant turn" = one model call (one asst message + 0+ tool_results).
    Counting by assistant turns (not user messages) is important because one
    user message can expand to many model calls when the agent loops with
    tools — K=3 asst turns stays a tight window regardless of how many
    tool calls happened in the most recent user turn.

    The cutoff is the K-th-from-last asst message position. We do NOT walk
    back to include the user prompt of the first kept asst: the prompt is
    in the summary, and walking back would bloat the kept window with
    earlier cycles from the same user turn (which defeats the purpose of
    the tighter asst-turn unit). The first kept asst will look "orphan" in
    the simple alternating case (user / asst / user / asst / ...), but
    it's a self-contained model response and the summary anchors the user
    intent.

    Returns -1 if there are not enough assistant turns to split (no-op case).
    """
    asst_indices = [i for i, m in enumerate(working) if m.role == "assistant"]
    if len(asst_indices) <= keep_last_assistant_turns:
        return -1
    return asst_indices[-keep_last_assistant_turns]