import asyncio
import json
from pathlib import Path

from ziva_runtime.runtime import Runtime
from ziva_runtime.shared_types import ChatMessage, ChatResult, StreamDelta, ToolCallItem


def test_compaction_estimates_tokens():
    from ziva_runtime.session.compaction import estimate_tokens

    msgs = [
        ChatMessage(role="user", content="Hello world"),
        ChatMessage(role="assistant", content="Hi there!"),
    ]
    tokens = estimate_tokens(msgs)
    assert tokens > 0
    # Each message has ~10 overhead + content estimate
    assert tokens < 100


def test_compaction_prunes_tool_messages():
    """Prune keeps the tool message structure but collapses the output content
    to a placeholder, so the UI can still render the tool call but the LLM
    doesn't see the full payload on the next turn.
    """
    from ziva_runtime.session.compaction import prune

    msgs = [
        ChatMessage(role="user", content="first"),
        ChatMessage(role="assistant", content="a1"),
        ChatMessage(role="tool", content="tool output 1", tool_call_id="tc1"),
        ChatMessage(role="assistant", content="a2"),
        ChatMessage(role="user", content="second"),
        ChatMessage(role="assistant", content="a3"),
    ]
    pruned = prune(msgs, keep_last=1)
    # Tool message structure is preserved (we still need to render it)
    tool_msgs = [m for m in pruned if m.role == "tool"]
    assert len(tool_msgs) == 1
    # But its content has been replaced with a placeholder that names the
    # tool, so the model still knows which tool returned the (now-gone) data.
    # This tool msg has no `name` set, so the placeholder uses the
    # "unknown" fallback.
    assert tool_msgs[0].content == "[old tool result pruned — tool: unknown]"
    # User messages preserved
    user_msgs = [m for m in pruned if m.role == "user"]
    assert len(user_msgs) == 2


def test_compaction_noop_when_too_few_user_turns():
    """If the message list has <= keep_last_assistant_turns asst messages,
    compact_messages returns the same list (no-op)."""
    from ziva_runtime.session.compaction import compact_messages

    class MockAdapter:
        async def chat(self, messages, model, system_prompt=None, tools=None):
            raise AssertionError("should not be called for no-op")

    msgs = [
        ChatMessage(role="user", content="u1"),
        ChatMessage(role="assistant", content="a1"),
        ChatMessage(role="user", content="u2"),
        ChatMessage(role="assistant", content="a2"),
    ]
    async def _run():
        result = await compact_messages(
            msgs, context_window=100, model_name="m", model_adapter=MockAdapter(),
            keep_last_assistant_turns=3,
        )
        # No-op: same list reference, no summary created.
        assert result is msgs

    asyncio.run(_run())


def test_compaction_keeps_recent_assistant_turns():
    """Compact splits at the K-th-from-last asst message, keeping the last K
    asst cycles verbatim. No walk-back to the user prompt — the prompt
    is in the summary and walking back would bloat the kept window when
    one user message produced many cycles. In the simple alternating
    case, the first kept asst is "orphan" (its user prompt is in the
    summary), but the model can still infer context from the summary.
    """
    from ziva_runtime.session.compaction import compact_messages

    class MockAdapter:
        async def chat(self, messages, model, system_prompt=None, tools=None):
            return ChatResult(role="assistant", content="Mock summary.", model=model)

    # 4 user turns, each a clean 1-user / 1-asst pair (no tool calls).
    msgs = [
        ChatMessage(role="user", content="u1"),
        ChatMessage(role="assistant", content="a1"),
        ChatMessage(role="user", content="u2"),
        ChatMessage(role="assistant", content="a2"),
        ChatMessage(role="user", content="u3"),
        ChatMessage(role="assistant", content="a3"),
        ChatMessage(role="user", content="u4"),
        ChatMessage(role="assistant", content="a4"),
    ]
    async def _run():
        result = await compact_messages(
            msgs, context_window=100, model_name="m", model_adapter=MockAdapter(),
            keep_last_assistant_turns=2,
        )
        # asst_indices = [1, 3, 5, 7]; K=2 → cutoff = 5 (a3). NO walk-back.
        # to_summarize = [u1, a1, u2, a2, u3]   (u3 is in summary)
        # to_keep      = [a3, u4, a4]            (3 msgs: last 2 asst + 1 user)
        # result       = [summary, a3, u4, a4]    (4 items)
        assert len(result) == 4
        assert result[0]._compaction_summary is True
        assert "summary" in result[0].content.lower()
        assert result[1].content == "a3"
        assert result[2].content == "u4"
        assert result[3].content == "a4"

    asyncio.run(_run())


def test_compaction_counts_assistant_turns_not_user_messages():
    """The key behavior change: K=3 asst turns should keep 3 model-call
    cycles even when one user message produced many tool calls. With
    user-message counting, K=3 would keep 3 user messages (= much more
    when one user msg expanded to many cycles). The cutoff is the
    K-th-to-last asst position — we do NOT walk back to the user prompt
    of the first kept asst (that would bloat the window by pulling in
    earlier cycles from the same user turn)."""
    from ziva_runtime.session.compaction import compact_messages

    class MockAdapter:
        async def chat(self, messages, model, system_prompt=None, tools=None):
            return ChatResult(role="assistant", content="Mock summary.", model=model)

    # u1 prompts 5 model calls (a1..a5) with tool results between them.
    # Add an older turn (u0, a0) so compact has something to summarize.
    msgs = [
        ChatMessage(role="user", content="u0 — older turn"),
        ChatMessage(role="assistant", content="a0"),
        ChatMessage(role="user", content="u1 — long instruction with 5+ tool calls"),
        ChatMessage(role="assistant", content="", tool_calls=[ToolCallItem(id="t1", name="read_file", arguments={"p": "a"})]),
        ChatMessage(role="tool", content="output a", tool_call_id="t1", name="read_file"),
        ChatMessage(role="assistant", content="", tool_calls=[ToolCallItem(id="t2", name="read_file", arguments={"p": "b"})]),
        ChatMessage(role="tool", content="output b", tool_call_id="t2", name="read_file"),
        ChatMessage(role="assistant", content="", tool_calls=[ToolCallItem(id="t3", name="read_file", arguments={"p": "c"})]),
        ChatMessage(role="tool", content="output c", tool_call_id="t3", name="read_file"),
        ChatMessage(role="assistant", content="", tool_calls=[ToolCallItem(id="t4", name="read_file", arguments={"p": "d"})]),
        ChatMessage(role="tool", content="output d", tool_call_id="t4", name="read_file"),
        ChatMessage(role="assistant", content="done", tool_calls=[]),
    ]
    async def _run():
        result = await compact_messages(
            msgs, context_window=100, model_name="m", model_adapter=MockAdapter(),
            keep_last_assistant_turns=3,
        )
        # asst_indices = [1, 3, 5, 7, 9, 11]; K=3 → cutoff = 7 (a3).
        # NO walk-back. to_summarize = messages[:7] = [u0, a0, u1, a1, t1, a2, t2]
        # to_keep = [a3, t3, a4, t4, a5]   ← the last 3 asst cycles + their tool results
        # Each kept asst's tool_call/result pair is intact (a3/t3, a4/t4);
        # a5 is a final response. No orphan tool calls.
        # result = [summary, a3, t3, a4, t4, a5]  (6 items)
        assert len(result) == 6
        assert result[0]._compaction_summary is True
        assert "summary" in result[0].content.lower()
        # Last 3 asst cycles preserved verbatim
        assert result[1].role == "assistant"
        assert result[1].tool_calls[0].id == "t3"
        assert result[2].role == "tool"
        assert result[2].tool_call_id == "t3"
        assert result[3].role == "assistant"
        assert result[3].tool_calls[0].id == "t4"
        assert result[4].role == "tool"
        assert result[4].tool_call_id == "t4"
        assert result[5].role == "assistant"
        # Crucially, the older cycles a1, t1, a2, t2 (and the user
        # prompt u1) all went into the summary — the kept window
        # is exactly 3 model-call cycles.
        assert all(m.content != "u1 — long instruction with 5+ tool calls" for m in result[1:])

    asyncio.run(_run())


def test_compaction_noop_when_too_few_assistant_turns():
    """If the message list has ≤ K asst messages, compact returns the
    same list (no-op) — there's nothing older to summarize."""
    from ziva_runtime.session.compaction import compact_messages

    class MockAdapter:
        async def chat(self, messages, model, system_prompt=None, tools=None):
            raise AssertionError("should not be called for no-op")

    # 2 asst messages only, K=3 → no-op
    msgs = [
        ChatMessage(role="user", content="u1"),
        ChatMessage(role="assistant", content="", tool_calls=[ToolCallItem(id="t1", name="f", arguments={})]),
        ChatMessage(role="tool", content="o1", tool_call_id="t1", name="f"),
        ChatMessage(role="assistant", content="done", tool_calls=[]),
    ]
    async def _run():
        result = await compact_messages(
            msgs, context_window=100, model_name="m", model_adapter=MockAdapter(),
            keep_last_assistant_turns=3,
        )
        # asst_indices = [1, 3]; len=2 ≤ K=3 → return msgs as-is, no LLM call
        assert result is msgs

    asyncio.run(_run())


def test_compaction_keeps_exactly_k_assistant_cycles_in_chain():
    """The tight-window guarantee: even if a single user turn produced
    many cycles, K=3 asst turns keeps exactly 3 cycles (not 3 user
    messages worth). The user prompt + earlier cycles go into the summary."""
    from ziva_runtime.session.compaction import compact_messages

    class MockAdapter:
        async def chat(self, messages, model, system_prompt=None, tools=None):
            return ChatResult(role="assistant", content="Mock summary.", model=model)

    # 1 user turn, 5 asst cycles (3 with tool calls + 1 final)
    msgs = [
        ChatMessage(role="user", content="u1"),
        ChatMessage(role="assistant", content="", tool_calls=[ToolCallItem(id="t1", name="f", arguments={"i": "a"})]),
        ChatMessage(role="tool", content="o1", tool_call_id="t1", name="f"),
        ChatMessage(role="assistant", content="", tool_calls=[ToolCallItem(id="t2", name="f", arguments={"i": "b"})]),
        ChatMessage(role="tool", content="o2", tool_call_id="t2", name="f"),
        ChatMessage(role="assistant", content="", tool_calls=[ToolCallItem(id="t3", name="f", arguments={"i": "c"})]),
        ChatMessage(role="tool", content="o3", tool_call_id="t3", name="f"),
        ChatMessage(role="assistant", content="", tool_calls=[ToolCallItem(id="t4", name="f", arguments={"i": "d"})]),
        ChatMessage(role="tool", content="o4", tool_call_id="t4", name="f"),
        ChatMessage(role="assistant", content="done", tool_calls=[]),
    ]
    async def _run():
        result = await compact_messages(
            msgs, context_window=100, model_name="m", model_adapter=MockAdapter(),
            keep_last_assistant_turns=3,
        )
        # asst_indices = [1, 3, 5, 7, 9]; K=3 → cutoff = 5 (a3). NO walk-back.
        # to_summarize = [u1, a1, o1, a2, o2]
        # to_keep = [a3, o3, a4, o4, a5]   ← exactly 3 asst cycles
        # result = [summary, a3, o3, a4, o4, a5]  (6 items)
        assert len(result) == 6
        assert result[0]._compaction_summary is True
        kept_asst_cycles = [m for m in result[1:] if m.role == "assistant"]
        assert len(kept_asst_cycles) == 3
        assert kept_asst_cycles[0].tool_calls[0].id == "t3"
        assert kept_asst_cycles[1].tool_calls[0].id == "t4"
        assert kept_asst_cycles[2].content == "done"
        # Crucial: the user prompt u1 is NOT in to_keep (it went to summary)
        assert not any(m.content == "u1" for m in result[1:])

    asyncio.run(_run())


def test_compose_post_compact_on_disk_first_compact():
    """On the first compact, the on-disk layout after compact is
    [preserved_old, summary, ...to_keep] where preserved_old is everything
    before the K-th-from-last asst message in the (no-summary) on-disk."""
    from ziva_runtime.session.compaction import (
        compose_post_compact_on_disk, find_last_summary_idx, find_cutoff_in_llm_visible,
    )

    on_disk_records = [
        {"role": "user", "content": "u1"},
        {"role": "assistant", "content": "a1"},
        {"role": "user", "content": "u2"},
        {"role": "assistant", "content": "a2"},
        {"role": "user", "content": "u3"},
        {"role": "assistant", "content": "a3"},
        {"role": "user", "content": "u4"},
        {"role": "assistant", "content": "a4"},
    ]
    working_before = [
        ChatMessage(role="user", content="u1"),
        ChatMessage(role="assistant", content="a1"),
        ChatMessage(role="user", content="u2"),
        ChatMessage(role="assistant", content="a2"),
        ChatMessage(role="user", content="u3"),
        ChatMessage(role="assistant", content="a3"),
        ChatMessage(role="user", content="u4"),
        ChatMessage(role="assistant", content="a4"),
    ]
    # K=2 asst turns. asst_indices = [1, 3, 5, 7] → cutoff = 5 (a3). NO walk-back.
    # to_keep = [a3, u4, a4] (3 msgs)
    new_working_dicts = [
        {"role": "assistant", "content": "summary text", "_compaction_summary": True},
        {"role": "assistant", "content": "a3"},
        {"role": "user", "content": "u4"},
        {"role": "assistant", "content": "a4"},
    ]
    last_summary_idx = find_last_summary_idx(on_disk_records)
    cutoff = find_cutoff_in_llm_visible(working_before, keep_last_assistant_turns=2)
    result = compose_post_compact_on_disk(
        on_disk_records, last_summary_idx, cutoff, new_working_dicts
    )
    # K=2 → cutoff in working_before = 5 (a3, no walk-back).
    # to_keep starts at on-disk[5].
    # preserved_old = on_disk[:5] = [u1, a1, u2, a2, u3]
    # new_working appended: [summary, a3, u4, a4]
    assert last_summary_idx == -1
    assert cutoff == 5
    assert len(result) == 9
    assert result[0] == {"role": "user", "content": "u1"}
    assert result[4] == {"role": "user", "content": "u3"}
    assert result[5]["_compaction_summary"] is True
    assert result[6] == {"role": "assistant", "content": "a3"}
    assert result[7] == {"role": "user", "content": "u4"}
    assert result[8] == {"role": "assistant", "content": "a4"}


def test_compose_post_compact_on_disk_second_compact():
    """On the second compact, the previous summary is preserved in
    preserved_old alongside the original messages. New summary is
    inserted right before the new to_keep, leaving the older summary +
    intermediate messages intact."""
    from ziva_runtime.session.compaction import (
        compose_post_compact_on_disk, find_last_summary_idx, find_cutoff_in_llm_visible,
    )

    # After 1st compact:  [u1, a1, u2, a2, summary1, u3, a3, u4, a4, u5, a5]
    on_disk_records = [
        {"role": "user", "content": "u1"},
        {"role": "assistant", "content": "a1"},
        {"role": "user", "content": "u2"},
        {"role": "assistant", "content": "a2"},
        {"role": "assistant", "content": "summary1", "_compaction_summary": True},
        {"role": "user", "content": "u3"},
        {"role": "assistant", "content": "a3"},
        {"role": "user", "content": "u4"},
        {"role": "assistant", "content": "a4"},
        {"role": "user", "content": "u5"},
        {"role": "assistant", "content": "a5"},
    ]
    # working_before = LLM-visible = [summary1, u3, a3, u4, a4, u5, a5]
    working_before = [
        ChatMessage(role="assistant", content="summary1", _compaction_summary=True),
        ChatMessage(role="user", content="u3"),
        ChatMessage(role="assistant", content="a3"),
        ChatMessage(role="user", content="u4"),
        ChatMessage(role="assistant", content="a4"),
        ChatMessage(role="user", content="u5"),
        ChatMessage(role="assistant", content="a5"),
    ]
    # K=2 asst turns. asst_indices in working_before = [0 (summary1), 2, 4, 6] → cutoff = 4.
    # to_summarize = [summary1, u3, a3, u4]
    # to_keep = [a4, u5, a5]
    new_working_dicts = [
        {"role": "assistant", "content": "summary2", "_compaction_summary": True},
        {"role": "assistant", "content": "a4"},
        {"role": "user", "content": "u5"},
        {"role": "assistant", "content": "a5"},
    ]
    last_summary_idx = find_last_summary_idx(on_disk_records)
    cutoff = find_cutoff_in_llm_visible(working_before, keep_last_assistant_turns=2)
    result = compose_post_compact_on_disk(
        on_disk_records, last_summary_idx, cutoff, new_working_dicts
    )
    # last_summary_idx in on_disk_records = 4 (summary1)
    # cutoff in working_before = 4
    # to_keep starts at on_disk[4 + 4] = on_disk[8] = a4
    # preserved_old = on_disk[:8] = [u1, a1, u2, a2, summary1, u3, a3, u4]
    # new_working records appended: [summary2, a4, u5, a5]
    assert last_summary_idx == 4
    assert cutoff == 4
    assert len(result) == 12
    # The old summary1 is still in preserved_old
    assert result[4]["_compaction_summary"] is True
    assert result[4]["content"] == "summary1"
    # The new summary2 is inserted right after u4
    assert result[8]["_compaction_summary"] is True
    assert result[8]["content"] == "summary2"
    # to_keep follows
    assert result[9] == {"role": "assistant", "content": "a4"}
    assert result[10] == {"role": "user", "content": "u5"}
    assert result[11] == {"role": "assistant", "content": "a5"}


def test_compose_post_compact_on_disk_noop_when_too_few_assistant_turns():
    """If find_cutoff_in_llm_visible returns -1 (no split possible), the
    helper should just return current_on_disk + new_working as-is (no split)."""
    from ziva_runtime.session.compaction import (
        compose_post_compact_on_disk, find_last_summary_idx, find_cutoff_in_llm_visible,
    )

    on_disk = [{"role": "user", "content": "u1"}]
    working_before = [ChatMessage(role="user", content="u1")]
    new_working_dicts = [{"role": "user", "content": "u1"}]
    last_summary_idx = find_last_summary_idx(on_disk)
    cutoff = find_cutoff_in_llm_visible(working_before, keep_last_assistant_turns=3)
    # Not enough asst turns to split — cutoff == -1
    assert cutoff == -1
    result = compose_post_compact_on_disk(
        on_disk, last_summary_idx, cutoff, new_working_dicts
    )
    # Helper still concatenates, but caller shouldn't use this path; the
    # _apply_compact_to_disk wrapper short-circuits on cutoff == -1.
    assert result == on_disk + new_working_dicts


def test_doom_loop_detection():
    class DoomLoopAdapter:
        async def chat(self, messages, model, system_prompt=None, tools=None):
            return ChatResult(
                role="assistant",
                content="",
                model=model,
                usage={},
                finish_reason="tool_calls",
                tool_calls=[ToolCallItem(id="tc_1", name="echo", arguments={"text": "same"})],
            )

        async def chat_stream(self, messages, model, system_prompt=None, tools=None):
            yield StreamDelta(
                content="",
                tool_calls=[ToolCallItem(id="tc_1", name="echo", arguments={"text": "same"})],
                finish_reason="tool_calls",
            )

    async def _run():
        root = Path(__file__).resolve().parents[1]
        rt = Runtime.create(
            workspace_root=root,
            model_adapter=DoomLoopAdapter(),
            session_override={"tool": {"max_rounds": 10}},
        )
        result = await rt.chat([ChatMessage(role="user", content="loop")], session_id="doom-1")
        assert result.finish_reason == "doom_loop"
        assert "repeated" in result.content.lower()

    asyncio.run(_run())
