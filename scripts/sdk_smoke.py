#!/usr/bin/env python3
"""End-to-end smoke test for the ziva SDK.

Runs without a real API key and without touching ~/.ziva: it swaps the
runtime's model adapter for an echo-style fake, then drives `ziva.Agent`
through chat() and stream() backed by InMemoryStorage.

    PYTHONPATH=src python scripts/sdk_smoke.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

# Make `src/` importable when run from a checkout (no install required).
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import ziva
import ziva.runtime as runtime_module
from ziva.shared_types import ChatResult, StreamDelta


class _FakeAdapter:
    async def chat(self, messages, model, system_prompt=None, tools=None, thinking_config=None):
        return ChatResult(role="assistant", content="echo: " + messages[-1].content if hasattr(messages[-1], "content") else "echo", model=model, usage={}, finish_reason="stop")

    async def chat_stream(self, messages, model, system_prompt=None, tools=None, thinking_config=None):
        user_text = messages[-1].content if messages else ""
        if isinstance(user_text, list):
            user_text = " ".join(getattr(b, "text", str(b)) for b in user_text)
        yield StreamDelta(content=f"echo: {user_text}", finish_reason="stop")


async def main() -> int:
    runtime_module._create_adapter = lambda config: _FakeAdapter()

    # 1. Build an agent with NO filesystem (in-memory storage) and NO plugins.
    agent = ziva.Agent(
        model="gpt-4.1",
        api_key="sk-not-used",
        storage=ziva.InMemoryStorage(),
        load_default_plugins=False,
    )
    print(f"[smoke] Agent built; storage={agent.runtime.storage.__class__.__name__}")

    sid = agent.new_session()

    # 2. chat()
    result = await agent.chat("hello world", session_id=sid)
    print(f"[smoke] chat() -> {result.content!r}")

    # 3. stream()
    print("[smoke] stream() events:")
    async for ev in agent.stream("streaming works", session_id=sid):
        print(f"         {ev.get('type'):>16}  {ev.get('content', '')[:40]!r}")

    # 4. The conversation persisted to InMemoryStorage.
    msgs = list(agent.runtime.storage.get_messages(agent.runtime.project_id, sid))
    print(f"[smoke] persisted messages: {[m['role'] for m in msgs]}")

    assert "echo: hello world" == result.content
    assert {"user", "assistant"} <= {m["role"] for m in msgs}
    print("[smoke] OK — SDK chat/stream/storage all functional.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
