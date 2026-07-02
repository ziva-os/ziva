import asyncio
import tempfile
from pathlib import Path
from unittest.mock import patch

from aiohttp import web
from aiohttp.test_utils import AioHTTPTestCase, unittest_run_loop

from ziva import runtime as runtime_mod
from ziva.runtime import Runtime
from ziva.shared_types import ChatMessage, ChatResult, StreamDelta
from ziva.storage.file_storage import FileStorage
from ziva.transports.desktop_api.server import DesktopAPIServer


class FakeAdapter:
    async def chat(self, messages, model, system_prompt=None, tools=None):
        return ChatResult(role="assistant", content="response", model=model, usage={}, finish_reason="stop")

    async def chat_stream(self, messages, model, system_prompt=None, tools=None, thinking_config=None):
        yield StreamDelta(content="response")
        yield StreamDelta(finish_reason="stop", usage={})


# _create_adapter is the function both `Runtime.chat()` and the
# server's `/compact` / `_run_automation_once` paths reach for to
# build a fresh adapter per call. Tests that don't want to hit a
# real provider patch this single entry point — production code
# stays untouched.
def _fake_create_adapter(config=None):
    return FakeAdapter()


def _make_server() -> DesktopAPIServer:
    # Use a temp workspace so automation/session state does not leak into the
    # real project directory (e.g. .ziva/sessions/<project>/automations.json).
    root = Path(tempfile.mkdtemp())
    # Patch the symbol the runtime module exposes (server.py imports
    # _create_adapter from here too, so a single patch covers both
    # call sites).
    with patch.object(runtime_mod, "_create_adapter", _fake_create_adapter):
        rt = Runtime.create(workspace_root=root)
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
        await self.client.delete(f"/automations/{data['id']}")

    @unittest_run_loop
    async def test_list_automations(self):
        created = await self.client.post("/automations", json={"name": "a1", "prompt": "hi", "interval_seconds": 60})
        aid = (await created.json())["id"]
        resp = await self.client.get("/automations")
        assert resp.status == 200
        data = await resp.json()
        assert len(data["automations"]) >= 1
        assert "next_run" in data["automations"][0]
        await self.client.delete(f"/automations/{aid}")

    @unittest_run_loop
    async def test_update_automation(self):
        resp = await self.client.post("/automations", json={"name": "edit", "prompt": "x", "interval_seconds": 60})
        aid = (await resp.json())["id"]

        resp = await self.client.patch(f"/automations/{aid}", json={"enabled": False, "interval_seconds": 120})
        assert resp.status == 200
        data = await resp.json()
        assert data["automation"]["enabled"] is False
        assert data["automation"]["interval_seconds"] == 120

        await self.client.delete(f"/automations/{aid}")

    @unittest_run_loop
    async def test_run_automation_now(self):
        resp = await self.client.post("/automations", json={"name": "run", "prompt": "x", "interval_seconds": 60})
        aid = (await resp.json())["id"]

        resp = await self.client.post(f"/automations/{aid}/run")
        assert resp.status == 200
        data = await resp.json()
        assert data["accepted"] is True
        assert data["automation"]["id"] == aid

        await self.client.delete(f"/automations/{aid}")

    @unittest_run_loop
    async def test_persisted_automation_is_loaded(self):
        with tempfile.TemporaryDirectory() as td:
            rt = Runtime.create(workspace_root=Path(td))
            FileStorage.upsert_automation(rt.project_id, {
                "id": "persisted",
                "name": "persisted task",
                "prompt": "hello",
                "interval_seconds": 600,
                "session_id": "persisted-session",
                "enabled": False,
            })

            server = DesktopAPIServer(rt)
            server._load_persisted_automations()

            assert "persisted" in server.automations
            assert server.automations["persisted"].prompt == "hello"
            FileStorage.delete_automation(rt.project_id, "persisted")

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
