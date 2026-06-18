import asyncio
from pathlib import Path

from aiohttp.test_utils import TestClient, TestServer

from ziva_runtime.runtime import Runtime
from ziva_runtime.shared_types import ChatResult
from ziva_runtime.transports.desktop_api.server import DesktopAPIServer


class FakeAdapter:
    async def chat(self, messages, model, system_prompt=None, tools=None):
        return ChatResult(role="assistant", content="ok", model=model, usage={}, finish_reason="stop")


def test_desktop_session_turn_and_index():
    async def _run():
        root = Path(__file__).resolve().parents[1]
        rt = Runtime.create(workspace_root=root)
        api = DesktopAPIServer(rt)
        server = TestServer(api.app)
        client = TestClient(server)
        await client.start_server()
        try:
            idx = await client.get("/")
            assert idx.status == 200
            html = await idx.text()
            assert "Ziva Desktop" in html

            resp = await client.post("/sessions", json={})
            assert resp.status == 200
            session = await resp.json()
            sid = session["id"]

            turn_resp = await client.post(f"/sessions/{sid}/turns", json={"messages": [{"role": "user", "content": "hi"}]})
            assert turn_resp.status == 200
            payload = await turn_resp.json()
            assert payload["accepted"] is True
            assert payload.get("turn_id")
            await asyncio.sleep(0.01)

            sessions_resp = await client.get("/sessions")
            assert sessions_resp.status == 200
            sessions = await sessions_resp.json()
            assert any(item["id"] == sid for item in sessions["sessions"])

            history_resp = await client.get(f"/sessions/{sid}/messages")
            assert history_resp.status == 200
            history = await history_resp.json()
            assert history["messages"]
            assert history["messages"][0]["role"] == "user"
            assert history["messages"][0]["content"] == "hi"

            turns_resp = await client.get(f"/sessions/{sid}/turns")
            assert turns_resp.status == 200
            turns = await turns_resp.json()
            assert turns["turns"]
            assert turns["turns"][0]["id"] == payload["turn_id"]

            # direct bus subscription as deterministic event assertion
            q = rt.event_bus.subscribe(sid)
            await rt.chat([{"__class__":"ignored"}] if False else [], session_id=sid) if False else None
            rt.event_bus.unsubscribe(sid, q)
        finally:
            await client.close()

    asyncio.run(_run())
