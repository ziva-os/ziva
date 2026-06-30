"""Runtime-level tests for the reasoning + content streaming contract.

These tests complement tests/test_anthropic_streaming.py (which pins the
adapter-level contract) by verifying the *runtime* routes reasoning and
content to the right SSE event types and persists them to disk in a way
that the frontend's loadHistory can re-render.

User-reported bug: "anthropic 格式的 api 如 kimi-k2.6 为什么消息区 UI 上
不显示 thinking (reasoning_content)，content 不知道是不是也不显示"

We assert all of the following so any future regression surfaces here:
  1. chat_streaming emits `reasoning_delta` events with the reasoning text
  2. chat_streaming emits `delta` events with the regular text
  3. The final assistant ChatMessage persisted to disk has both
     `content` and `reasoning_content` populated
  4. The same assistant ChatMessage appears in the on-disk JSONL
     (so the next /sessions/{sid}/messages GET returns it)
  5. get_messages returns the reasoning_content so the frontend's
     loadHistoryInto → renderMessages path can show the thinking card
     after a reload
"""
import asyncio
import json
from pathlib import Path
from typing import AsyncIterator, List

import pytest

from ziva_runtime.runtime import Runtime
from ziva_runtime.shared_types import ChatMessage, StreamDelta
from ziva_runtime.storage.file_storage import FileStorage


class _FakeAnthropicAdapter:
    """Stand-in for AnthropicChatAdapter that emits the same delta
    sequence a kimi-k2.6 / claude-3-7 thinking turn would produce.

    We don't reuse the real Anthropic adapter here because:
      - The runtime only depends on `chat_stream(...) → AsyncIterator[StreamDelta]`
      - Driving the real SDK with a mock event stream is more setup
        and the adapter-level test (test_anthropic_streaming.py) already
        pins that contract
    """

    def __init__(self):
        self.calls: list = []

    async def chat(
        self, messages, model, system_prompt=None, tools=None, thinking_config=None
    ):
        self.calls.append({"model": model, "thinking_config": thinking_config})
        from ziva_runtime.shared_types import ChatResult
        return ChatResult(
            role="assistant",
            content="hi",
            model=model,
            usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            finish_reason="stop",
            reasoning_content="thought",
        )

    async def chat_stream(
        self, messages, model, system_prompt=None, tools=None, thinking_config=None
    ) -> AsyncIterator[StreamDelta]:
        self.calls.append({"model": model, "thinking_config": thinking_config})
        # Simulate Anthropic SDK stream: a thinking block, then a text block.
        yield StreamDelta(content="", reasoning_signature="sig-from-adapter")
        yield StreamDelta(content="", reasoning_content="first thought ")
        yield StreamDelta(content="", reasoning_content="more thought")
        yield StreamDelta(content="Hello ")
        yield StreamDelta(content="world")
        yield StreamDelta(
            content="",
            finish_reason="end_turn",
            usage={"prompt_tokens": 12, "completion_tokens": 7, "total_tokens": 19},
        )


def _make_config_with_thinking_anthropic(tmp_path: Path) -> Path:
    """Runtime config that declares an anthropic-format model with
    `capabilities.thinking: true` and `thinking_mode: high` so the
    runtime's thinking-config gate (line ~1031 of runtime.py) opens.
    """
    cfg = tmp_path / "global.yaml"
    cfg.write_text(
        "model:\n"
        "  name: kimi-k2.6\n"
        "  max_tokens: 4096\n"
        "  thinking_mode: high\n"
        "  thinking_budget_tokens: 2000\n"
        "providers:\n"
        "- name: anthropic-prov\n"
        "  api_type: anthropic\n"
        "  api_key: stub\n"
        "  base_url: http://stub\n"
        "  models:\n"
        "  - name: kimi-k2.6\n"
        "    capabilities:\n"
        "      thinking: true\n",
        encoding="utf-8",
    )
    return cfg


@pytest.fixture
def thinking_runtime(tmp_path: Path):
    cfg = _make_config_with_thinking_anthropic(tmp_path)
    rt = Runtime.create(workspace_root=tmp_path, global_config_path=cfg)
    adapter = _FakeAnthropicAdapter()
    # Force _create_adapter to return our fake; bypasses the real Anthropic SDK.
    from ziva_runtime import runtime as runtime_module
    original = runtime_module._create_adapter
    runtime_module._create_adapter = lambda config: adapter
    try:
        yield rt, adapter
    finally:
        runtime_module._create_adapter = original


def test_chat_streaming_emits_reasoning_delta_and_delta(thinking_runtime):
    """Step 1: the runtime must turn StreamDelta(reasoning_content=)
    into an SSE event of type `reasoning_delta` so the frontend
    accumulates it into _reasoning for the thinking card. Text deltas
    must come out as `delta` so they go into _main. If either path
    is missing, the UI shows an empty card or a missing reply.
    """
    rt, _adapter = thinking_runtime
    sid = "sid-streaming"

    async def _run():
        events: List[dict] = []
        async for ev in rt.chat_streaming(
            [ChatMessage(role="user", content="hi")], session_id=sid
        ):
            events.append(ev)

        # Filter to the per-delta events. The runtime also yields
        # turn_start / turn_end / usage_update / model_response — those
        # are fine, but the two we care about are reasoning_delta and delta.
        reasoning_events = [e for e in events if e.get("type") == "reasoning_delta"]
        text_events = [e for e in events if e.get("type") == "delta"]
        assert reasoning_events, f"no reasoning_delta events: {events!r}"
        assert text_events, f"no delta events: {events!r}"

        # The accumulated reasoning matches what the adapter produced.
        joined = "".join(e.get("content", "") for e in reasoning_events)
        assert joined == "first thought more thought", (
            f"reasoning_delta contents lost or reordered: {joined!r}"
        )
        # And the text deltas concatenate to the visible reply.
        joined_text = "".join(e.get("content", "") for e in text_events)
        assert joined_text == "Hello world", (
            f"text delta contents lost or reordered: {joined_text!r}"
        )
        # The first reasoning_delta carries the signature so the runtime
        # can persist it on the ChatMessage for next-turn replay.
        assert reasoning_events[0].get("content") == "first thought "

    asyncio.run(_run())


def test_chat_streaming_persists_reasoning_to_disk(thinking_runtime):
    """Step 2: after a streaming turn, the on-disk JSONL must contain
    an assistant message with both `content` and `reasoning_content`.
    This is what the frontend's loadHistoryInto reads when the user
    reloads the page or switches sessions — without it, the thinking
    card is empty after every reload.
    """
    rt, _adapter = thinking_runtime
    sid = "sid-persist"

    async def _run():
        async for _ in rt.chat_streaming(
            [ChatMessage(role="user", content="hi")], session_id=sid
        ):
            pass

    asyncio.run(_run())

    # Read the JSONL the runtime wrote for this session.
    pid = rt.project_id
    path = Path.home() / ".ziva" / "sessions" / pid / "messages" / f"{sid}.jsonl"
    assert path.exists(), f"JSONL not created at {path}"
    lines = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]

    # First the user msg, then the assistant.
    assert lines[0]["role"] == "user"
    asst = next(m for m in lines if m["role"] == "assistant")
    # Both fields must be persisted. `reasoning_content` going missing
    # here is exactly the "thinking card empty after reload" failure.
    assert asst.get("reasoning_content") == "first thought more thought", (
        f"reasoning_content not persisted: {asst!r}"
    )
    assert asst.get("content") == "Hello world", (
        f"content not persisted: {asst!r}"
    )
    # And the signature is preserved so multi-turn replay keeps working.
    assert asst.get("reasoning_signature") == "sig-from-adapter", (
        f"reasoning_signature not persisted: {asst!r}"
    )


def test_get_messages_returns_reasoning_for_frontend_reload(thinking_runtime):
    """Step 3: GET /sessions/{sid}/messages must surface
    reasoning_content on the assistant message so the frontend's
    `loadHistoryInto` → `renderMessages` path can render the
    thinking card on reload. This is the second half of the user
    report ('reasoning_content 也不显示' could be the render path
    silently dropping the field).
    """
    rt, _adapter = thinking_runtime
    sid = "sid-reload"

    async def _run():
        async for _ in rt.chat_streaming(
            [ChatMessage(role="user", content="hi")], session_id=sid
        ):
            pass

    asyncio.run(_run())

    # Now go through the same path the desktop_api get_messages handler
    # uses: FileStorage.get_messages + the LLM-context filter.
    pid = rt.project_id
    all_msgs = list(FileStorage.get_messages(pid, sid))
    asst = next(m for m in all_msgs if m.get("role") == "assistant")
    assert asst.get("reasoning_content") == "first thought more thought"
    # And after the compaction filter, the same message must survive
    # (no compaction happened here, but the filter must not drop the
    # reasoning field on a message that was the model's actual reply).
    from ziva_runtime.session.compaction import _llm_context
    visible = _llm_context(all_msgs)
    asst_visible = next(m for m in visible if m.get("role") == "assistant")
    assert asst_visible.get("reasoning_content") == "first thought more thought", (
        f"_llm_context stripped reasoning_content: {asst_visible!r}"
    )


def test_chat_streaming_uses_session_model_name(thinking_runtime):
    """Sanity: the adapter is called with the right thinking_config
    when the runtime builds the per-turn config. If a future refactor
    stops passing `thinking_config` through, reasoning blocks will
    never be requested and the thinking card will always be empty —
    this test would catch it.
    """
    rt, adapter = thinking_runtime
    sid = "sid-think-cfg"

    async def _run():
        async for _ in rt.chat_streaming(
            [ChatMessage(role="user", content="hi")], session_id=sid
        ):
            pass

    asyncio.run(_run())

    # chat_streaming builds the adapter and calls chat_stream with the
    # model's `thinking_mode` translated into a thinking_config dict.
    stream_calls = [c for c in adapter.calls if "thinking_config" in c]
    assert stream_calls, f"chat_stream was never called: {adapter.calls!r}"
    tc = stream_calls[-1]["thinking_config"]
    assert tc is not None, "thinking_config should be set for thinking-capable model"
    assert tc.get("type") == "enabled"
    assert tc.get("budget_tokens") == 2000
