"""End-to-end: GET /sessions/{sid}/messages must include
reasoning_content on assistant messages.

This is the final hop in the "thinking card is empty" chain:

  chat_streaming → JSONL on disk
                 → FileStorage.get_messages (runtime)
                 → GET /sessions/{sid}/messages (server)
                 → frontend loadHistoryInto → renderMessages → thinking card

Each step is pinned by another test; this one pins the HTTP boundary
itself so a refactor of `server.get_messages` that drops the field
(e.g. by whitelisting a different set of message keys) fails here.
"""
import asyncio
import json
from pathlib import Path
from unittest.mock import patch

from aiohttp.test_utils import TestClient, TestServer

from ziva_runtime.runtime import Runtime
from ziva_runtime.shared_types import ChatMessage, StreamDelta
from ziva_runtime.storage.file_storage import FileStorage
from ziva_runtime.transports.desktop_api.server import DesktopAPIServer


class _FakeAnthropicAdapter:
    """Mirrors the test_anthropic_runtime_streaming one but minimal —
    this test only cares that reasoning_content ends up in the GET
    response, not the SSE event stream shape."""

    async def chat(self, messages, model, system_prompt=None, tools=None, thinking_config=None):
        from ziva_runtime.shared_types import ChatResult
        return ChatResult(role="assistant", content="ok", model=model, usage={}, finish_reason="stop")

    async def chat_stream(self, messages, model, system_prompt=None, tools=None, thinking_config=None):
        yield StreamDelta(content="", reasoning_signature="sig-http-test")
        yield StreamDelta(content="", reasoning_content="think 1 ")
        yield StreamDelta(content="", reasoning_content="think 2")
        yield StreamDelta(content="visible reply")
        yield StreamDelta(content="", finish_reason="end_turn", usage={})


def _make_config_with_thinking(tmp_path: Path) -> Path:
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


def test_get_messages_endpoint_surfaces_reasoning_content(tmp_path):
    async def _run():
        cfg = _make_config_with_thinking(tmp_path)
        rt = Runtime.create(workspace_root=tmp_path, global_config_path=cfg)

        # Patch the adapter factory to return our fake before any turn runs.
        from ziva_runtime import runtime as runtime_module
        adapter = _FakeAnthropicAdapter()
        runtime_module._create_adapter = lambda config: adapter

        api = DesktopAPIServer(rt)
        server = TestServer(api.app)
        client = TestClient(server)
        await client.start_server()
        try:
            # Create a session and run one thinking turn end-to-end.
            resp = await client.post("/sessions", json={})
            sid = (await resp.json())["id"]
            turn = await client.post(
                f"/sessions/{sid}/turns",
                json={"messages": [{"role": "user", "content": "hi"}]},
            )
            assert turn.status == 200

            # Wait for the turn to complete (poll a few times).
            for _ in range(50):
                turns = (await (await client.get(f"/sessions/{sid}/turns")).json())["turns"]
                if turns and all(t.get("status") != "running" for t in turns):
                    break
                await asyncio.sleep(0.05)

            # Now GET the messages. This is exactly what the frontend's
            # `loadHistoryInto` does on session switch.
            resp = await client.get(f"/sessions/{sid}/messages")
            assert resp.status == 200
            payload = await resp.json()
            assert payload["messages"], "no messages returned"
            asst = next(m for m in payload["messages"] if m.get("role") == "assistant")
            # The reasoning field must travel through the HTTP boundary.
            assert asst.get("reasoning_content") == "think 1 think 2", (
                f"reasoning_content missing from HTTP response: {asst!r}"
            )
            # And the regular content must still be there (this is the
            # "content 不知道是不是也不显示" half of the bug report).
            assert asst.get("content") == "visible reply", (
                f"content missing from HTTP response: {asst!r}"
            )
        finally:
            await client.close()

    asyncio.run(_run())
