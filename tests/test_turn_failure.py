import asyncio
from pathlib import Path

from aiohttp.test_utils import TestClient, TestServer

from ziva_runtime.runtime import Runtime
from ziva_runtime.transports.desktop_api.server import DesktopAPIServer


class FailingAdapter:
    async def chat(self, messages, model, system_prompt=None, tools=None):
        raise RuntimeError("simulated failure")


def test_turn_failure_persisted():
    async def _run():
        root = Path(__file__).resolve().parents[1]
        rt = Runtime.create(workspace_root=root, model_adapter=FailingAdapter())
        api = DesktopAPIServer(rt)
        server = TestServer(api.app)
        client = TestClient(server)
        await client.start_server()
        try:
            resp = await client.post("/sessions", json={})
            sid = (await resp.json())["id"]

            turn_resp = await client.post(f"/sessions/{sid}/turns", json={"messages": [{"role": "user", "content": "fail"}]})
            assert turn_resp.status == 200
            await asyncio.sleep(0.05)

            turns_resp = await client.get(f"/sessions/{sid}/turns")
            turns = (await turns_resp.json())["turns"]
            assert turns
            assert turns[0]["status"] == "failed"
            assert "error" in turns[0]
        finally:
            await client.close()

    asyncio.run(_run())
