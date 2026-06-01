import asyncio
from pathlib import Path

from aiohttp import web
from aiohttp.test_utils import AioHTTPTestCase, unittest_run_loop

from ziva_runtime.runtime import Runtime
from ziva_runtime.shared_types import ChatMessage, ChatResult
from ziva_runtime.transports.desktop_api.server import DesktopAPIServer


class FakeAdapter:
    async def chat(self, messages, model, system_prompt=None, tools=None):
        return ChatResult(role="assistant", content="response", model=model, usage={}, finish_reason="stop")


def _make_server() -> DesktopAPIServer:
    root = Path(__file__).resolve().parents[1]
    rt = Runtime.create(workspace_root=root, model_adapter=FakeAdapter())
    return DesktopAPIServer(rt)


class TestDesktopToolsPlan(AioHTTPTestCase):
    async def get_application(self):
        return _make_server().app

    @unittest_run_loop
    async def test_tools_status(self):
        resp = await self.client.post("/sessions")
        assert resp.status == 200
        sid = (await resp.json())["id"]

        resp = await self.client.get(f"/sessions/{sid}/tools")
        assert resp.status == 200
        data = await resp.json()
        assert "tools" in data

    @unittest_run_loop
    async def test_plan_empty(self):
        resp = await self.client.post("/sessions")
        sid = (await resp.json())["id"]

        resp = await self.client.get(f"/sessions/{sid}/plan")
        assert resp.status == 200
        data = await resp.json()
        assert data["plan"] == []


class TestDesktopAutomations(AioHTTPTestCase):
    async def get_application(self):
        return _make_server().app

    @unittest_run_loop
    async def test_create_automation(self):
        resp = await self.client.post("/automations", json={"name": "test", "prompt": "hello", "interval_seconds": 300})
        assert resp.status == 200
        data = await resp.json()
        assert "id" in data
        assert "session_id" in data

    @unittest_run_loop
    async def test_list_automations(self):
        await self.client.post("/automations", json={"name": "a1", "prompt": "hi", "interval_seconds": 60})
        resp = await self.client.get("/automations")
        assert resp.status == 200
        data = await resp.json()
        assert len(data["automations"]) >= 1

    @unittest_run_loop
    async def test_delete_automation(self):
        resp = await self.client.post("/automations", json={"name": "del", "prompt": "x", "interval_seconds": 60})
        aid = (await resp.json())["id"]

        resp = await self.client.delete(f"/automations/{aid}")
        assert resp.status == 200
        data = await resp.json()
        assert data["deleted"] is True

    @unittest_run_loop
    async def test_delete_nonexistent(self):
        resp = await self.client.delete("/automations/nonexistent")
        assert resp.status == 404

    @unittest_run_loop
    async def test_automation_requires_prompt(self):
        resp = await self.client.post("/automations", json={"name": "empty"})
        assert resp.status == 400


class TestDesktopDiff(AioHTTPTestCase):
    async def get_application(self):
        return _make_server().app

    @unittest_run_loop
    async def test_diff_endpoint(self):
        resp = await self.client.post("/sessions")
        sid = (await resp.json())["id"]

        resp = await self.client.get(f"/sessions/{sid}/diff")
        assert resp.status == 200
        data = await resp.json()
        assert "diff" in data
