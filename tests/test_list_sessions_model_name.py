"""Regression test: list_sessions must surface the per-session model_name.

Why this matters
----------------
The frontend's sidebar (refreshSessions → store.set({sessions})) feeds the
composer dropdown. `hydrateComposer` reads ``sessions[].model_name`` to
pick the initial model for the composer; if the field is missing, it
falls back to the runtime config's default model (e.g. ``MiniMax-M3``).

That fallback was the symptom behind "kimi-k2.7-code reverts to the
default model after restart": the *file* on disk still had
``model_name: "kimi-k2.7-code"`` (PATCH /sessions wrote it correctly),
but the ``GET /sessions`` endpoint only returned ``id / time / workspace
/ name`` — model_name was silently dropped during listing, so the UI
never saw it.

The fix: ``list_sessions`` in desktop_api/server.py now also returns
``model_name``. These tests pin the contract.
"""
import asyncio
from pathlib import Path

from aiohttp.test_utils import TestClient, TestServer

from ziva.runtime import Runtime
from ziva.transports.desktop_api.server import DesktopAPIServer


async def _bootstrap():
    """Start a DesktopAPIServer with a clean in-memory Runtime."""
    root = Path(__file__).resolve().parents[1]
    rt = Runtime.create(workspace_root=root)
    api = DesktopAPIServer(rt)
    server = TestServer(api.app)
    client = TestClient(server)
    await client.start_server()
    return rt, client


def test_list_sessions_includes_model_name():
    """A session with a pinned model_name shows up in /sessions listing."""
    async def _run():
        rt, client = await _bootstrap()
        try:
            # Create a session and pin it to a non-default model.
            resp = await client.post("/sessions", json={})
            sid = (await resp.json())["id"]
            patch = await client.patch(
                f"/sessions/{sid}", json={"model_name": "kimi-k2.7-code"}
            )
            assert patch.status == 200

            # /sessions must include the model_name we just pinned.
            resp = await client.get("/sessions")
            assert resp.status == 200
            items = (await resp.json())["sessions"]
            match = [s for s in items if s["id"] == sid]
            assert match, f"session {sid} missing from /sessions"
            assert match[0].get("model_name") == "kimi-k2.7-code", (
                f"expected model_name on listed session, got {match[0]!r}"
            )
        finally:
            await client.close()

    asyncio.run(_run())


def test_list_sessions_omits_model_name_when_not_set():
    """A session without a pinned model_name surfaces as None, not the
    runtime default. The frontend's hydrateComposer does its own
    `|| config.model` fallback, so we should NOT pre-fill it server-side
    (that would mask misconfigurations and surprise cross-workspace
    sessions that legitimately live in a different default).
    """
    async def _run():
        rt, client = await _bootstrap()
        try:
            resp = await client.post("/sessions", json={})
            sid = (await resp.json())["id"]
            # No PATCH — model_name never set.

            resp = await client.get("/sessions")
            assert resp.status == 200
            items = (await resp.json())["sessions"]
            match = [s for s in items if s["id"] == sid]
            assert match
            # model_name should be falsy (None) — NOT the runtime default.
            assert not match[0].get("model_name"), (
                f"expected no model_name, got {match[0]!r}"
            )
        finally:
            await client.close()

    asyncio.run(_run())


def test_list_sessions_persists_model_name_across_restart():
    """End-to-end: the model_name set on session 1 is still there in
    session 2's listing after a fresh Runtime is constructed (simulating
    an app restart). This is the actual user-reported regression.
    """
    async def _run():
        root = Path(__file__).resolve().parents[1]

        # First boot: pick the model, then drop the in-memory state.
        rt, client = await _bootstrap()
        try:
            resp = await client.post("/sessions", json={})
            sid = (await resp.json())["id"]
            await client.patch(
                f"/sessions/{sid}", json={"model_name": "kimi-k2.7-code"}
            )
        finally:
            await client.close()

        # Second boot: a *brand new* Runtime reads the session files
        # from disk. The /sessions listing must still see the model_name.
        rt2 = Runtime.create(workspace_root=root)
        api2 = DesktopAPIServer(rt2)
        server2 = TestServer(api2.app)
        client2 = TestClient(server2)
        await client2.start_server()
        try:
            resp = await client2.get("/sessions")
            assert resp.status == 200
            items = (await resp.json())["sessions"]
            match = [s for s in items if s["id"] == sid]
            assert match, f"session {sid} not visible after restart"
            assert match[0].get("model_name") == "kimi-k2.7-code", (
                f"model_name lost across restart: {match[0]!r}"
            )
        finally:
            await client2.close()

    asyncio.run(_run())
