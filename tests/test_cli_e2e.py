import asyncio
import builtins
import io
import sys

from ziva.app import cli
from ziva.shared_types import ChatResult


class FakeRuntime:
    def __init__(self):
        self.event_bus = type("EB", (), {"history": lambda self, sid: []})()

    async def chat(self, messages, session_id=None):
        return ChatResult(role="assistant", content=f"echo:{messages[0].content}", model="m", usage={}, finish_reason="stop")

    async def chat_with_events(self, messages, session_id=None):
        result = await self.chat(messages, session_id=session_id)
        return session_id or "s", result, [{"type": "model_response", "content": result.content}]


def test_cli_run_command_with_fake_runtime(monkeypatch):
    def fake_runtime_for_workspace(_path: str, session_override=None):
        return FakeRuntime()

    monkeypatch.setattr(cli, "_runtime_for_workspace", fake_runtime_for_workspace)

    captured = io.StringIO()
    original = sys.stdout
    sys.stdout = captured
    try:
        rc = asyncio.run(cli.run_async(["run", "hello"]))
    finally:
        sys.stdout = original

    assert rc == 0
    assert "echo:hello" in captured.getvalue()


def test_cli_run_no_stream(monkeypatch):
    def fake_runtime_for_workspace(_path: str, session_override=None):
        return FakeRuntime()

    printed = []
    def fake_print(*args, **kwargs):
        printed.append(" ".join(str(a) for a in args))

    monkeypatch.setattr(cli, "_runtime_for_workspace", fake_runtime_for_workspace)
    monkeypatch.setattr(builtins, "print", fake_print)
    rc = asyncio.run(cli.run_async(["run", "--no-stream", "hello"]))
    assert rc == 0
    assert any("echo:hello" in line for line in printed)
