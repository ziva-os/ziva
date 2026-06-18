import asyncio
from pathlib import Path

from aiohttp.test_utils import TestClient, TestServer

from ziva_runtime.runtime import Runtime
from ziva_runtime.shared_types import ChatMessage, ChatResult, StreamDelta
from ziva_runtime.transports.desktop_api.server import DesktopAPIServer


class SlowAdapter:
    """Adapter that yields slowly so we can interleave other work."""

    def __init__(self, name="default"):
        self.name = name

    async def chat(self, messages, model, system_prompt=None, tools=None):
        return ChatResult(role="assistant", content="ok", model=model, usage={}, finish_reason="stop")

    async def chat_stream(self, messages, model, system_prompt=None, tools=None, thinking_config=None):
        for i in range(20):
            await asyncio.sleep(0.05)
            yield StreamDelta(content=f"chunk-{i}")
        yield StreamDelta(finish_reason="stop", usage={})


def test_switch_session_does_not_kill_background():
    async def _run():
        root = Path(__file__).resolve().parents[1]
        adapter = SlowAdapter("test")
        rt = Runtime.create(workspace_root=root)
        api = DesktopAPIServer(rt)
        server = TestServer(api.app)
        client = TestClient(server)
        await client.start_server()

        # Monkey-patch _create_adapter to always return our test adapter
        from ziva_runtime import runtime as runtime_module
        runtime_module._create_adapter = lambda config: adapter

        try:
            # Create session A
            resp = await client.post("/sessions", json={})
            assert resp.status == 200
            sid_a = (await resp.json())["id"]

            # Start a turn in session A
            resp = await client.post(f"/sessions/{sid_a}/turns", json={"messages": [{"role": "user", "content": "hello A"}]})
            assert resp.status == 200
            turn_a = await resp.json()
            assert turn_a["accepted"] is True

            # Let A start running
            await asyncio.sleep(0.1)

            # Switch model (simulate frontend)
            resp = await client.patch("/config", json={"model": {"name": "Kimi-K2.6"}})
            assert resp.status == 200

            # Create session B
            resp = await client.post("/sessions", json={})
            assert resp.status == 200
            sid_b = (await resp.json())["id"]

            # Update session B's model_name
            resp = await client.patch(f"/sessions/{sid_b}", json={"model_name": "Kimi-K2.6"})
            assert resp.status == 200

            # Switch to session B (simulate frontend calling updateSession for old sid)
            resp = await client.patch(f"/sessions/{sid_a}", json={"model_name": "Kimi-K2.6"})
            assert resp.status == 200

            # Let both run a bit
            await asyncio.sleep(0.2)

            # Switch back to session A
            resp = await client.get(f"/sessions/{sid_a}/turns")
            assert resp.status == 200
            turns_a = (await resp.json())["turns"]
            print(f"Session A turns after switch: {turns_a}")

            # Session A should still be running
            assert any(t["status"] == "running" for t in turns_a), f"Session A was killed! turns={turns_a}"

            # Wait for A to finish
            for _ in range(50):
                resp = await client.get(f"/sessions/{sid_a}/turns")
                turns_a = (await resp.json())["turns"]
                if all(t["status"] != "running" for t in turns_a):
                    break
                await asyncio.sleep(0.05)

            print(f"Session A final turns: {turns_a}")
            assert any(t["status"] == "done" for t in turns_a), f"Session A did not complete normally! turns={turns_a}"

        finally:
            await client.close()

    asyncio.run(_run())
