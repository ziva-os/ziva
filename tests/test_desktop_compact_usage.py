"""Regression test: after /compact, the persisted last_usage shrinks to reflect
the new working-set size, so the UI context ring updates."""
import asyncio
from pathlib import Path

from aiohttp.test_utils import TestClient, TestServer

from ziva_runtime.runtime import Runtime
from ziva_runtime.shared_types import ChatResult
from ziva_runtime.storage.file_storage import FileStorage
from ziva_runtime.transports.desktop_api.server import DesktopAPIServer


class StubAdapter:
    """Stubbed adapter that returns a short summary regardless of input."""

    async def chat(self, messages, model, system_prompt=None, tools=None):
        return ChatResult(
            role="assistant",
            content="summary text",
            model=model,
            usage={"prompt_tokens": 50, "completion_tokens": 3, "total_tokens": 53},
            finish_reason="stop",
        )


def test_compact_refreshes_last_usage():
    async def _run():
        root = Path(__file__).resolve().parents[1]
        rt = Runtime.create(
            workspace_root=root,
            model_adapter=StubAdapter(),
            session_override={"memory": {"context_window_tokens": 1000}},
        )
        api = DesktopAPIServer(rt)
        server = TestServer(api.app)
        client = TestClient(server)
        await client.start_server()
        try:
            sid_resp = await client.post("/sessions", json={})
            sid = (await sid_resp.json())["id"]

            # Seed long history directly via storage so compaction has something to crunch.
            big = "x" * 2000
            for i in range(6):
                FileStorage.append_message(rt.project_id, sid, {
                    "role": "user" if i % 2 == 0 else "assistant",
                    "content": big,
                    "tool_call_id": None,
                    "name": None,
                    "tool_calls": [],
                })

            # Simulate the pre-compact usage that update_session_usage would have left behind.
            FileStorage.update_session(rt.project_id, sid, {
                "id": sid,
                "last_usage": {"prompt_tokens": 9999, "completion_tokens": 0, "total_tokens": 9999},
            })
            pre = (await (await client.get(f"/sessions/{sid}/messages")).json())["last_usage"]
            assert pre["prompt_tokens"] == 9999

            # Trigger compact.
            compact_resp = await client.post(f"/sessions/{sid}/compact")
            assert compact_resp.status == 200
            payload = await compact_resp.json()
            assert payload["success"] is True
            assert payload["last_usage"]["prompt_tokens"] < 9999, (
                "compact should report a smaller working-set token count"
            )

            # Verify the persisted value matches and the GET messages endpoint surfaces it.
            after = (await (await client.get(f"/sessions/{sid}/messages")).json())["last_usage"]
            assert after["prompt_tokens"] == payload["last_usage"]["prompt_tokens"]
            assert after["prompt_tokens"] < 9999

            # Regression: compact replaces the LLM context with `[summary]`
            # (no recent tail — codex CLI / claude code semantics) while
            # keeping the originals on disk stamped with `_compacted=True`
            # for the UI's collapse bar expand affordance.
            #
            # The default GET /messages endpoint returns ONLY the summary
            # message (post-compact UI view). The runtime's LLM context is
            # built from its own _session_history which holds just the
            # summary — the compacted originals are filtered out.
            after_messages = (await (await client.get(f"/sessions/{sid}/messages")).json())["messages"]
            summary_count = sum(1 for m in after_messages if m.get("_compaction_summary"))
            assert summary_count == 1, (
                f"expected exactly 1 _compaction_summary message, got {summary_count}"
            )
            # Filtered view = [summary] = 1. The 6 original messages are
            # still on disk but hidden by the filter — they can be revealed
            # via include_dropped=true for the UI's expand affordance.
            assert len(after_messages) == 1, (
                f"expected 1 filtered message after compact (just the summary), got {len(after_messages)}"
            )
            # And the full (include_dropped) view should contain the summary
            # plus the 6 compacted originals: 1 + 6 = 7.
            full = (await (await client.get(f"/sessions/{sid}/messages?include_dropped=true")).json())["messages"]
            assert len(full) == 7, (
                f"expected 7 total messages on disk after compact (1 summary + 6 originals), got {len(full)}"
            )
            compacted_count = sum(1 for m in full if m.get("_compacted"))
            assert compacted_count == 6, (
                f"expected 6 _compacted messages on disk, got {compacted_count}"
            )
        finally:
            await client.close()

    asyncio.run(_run())
