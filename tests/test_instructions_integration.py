import asyncio
from ziva.runtime import Runtime
from ziva.shared_types import ChatMessage, ChatResult


class CaptureAdapter:
    def __init__(self):
        self.last_system_prompt = None
        self.last_messages = None

    async def chat(self, messages, model, system_prompt=None, tools=None):
        self.last_system_prompt = system_prompt
        self.last_messages = messages
        return ChatResult(role="assistant", content="ok", model=model, usage={}, finish_reason="stop")


def test_instructions_injected_into_prompt(tmp_path):
    agents = tmp_path / ".ziva" / "AGENTS.md"
    agents.parent.mkdir(parents=True, exist_ok=True)
    agents.write_text("Always use type hints")
    adapter = CaptureAdapter()
    rt = Runtime.create(workspace_root=tmp_path)
    asyncio.run(rt.chat([ChatMessage(role="user", content="hello")], session_id="s1"))
    assert adapter.last_system_prompt is not None
    assert "Always use type hints" in adapter.last_system_prompt


def test_environment_context_injected(tmp_path, monkeypatch):
    monkeypatch.setenv("SHELL", "/bin/zsh")
    monkeypatch.setenv("TZ", "Asia/Shanghai")
    adapter = CaptureAdapter()
    rt = Runtime.create(workspace_root=tmp_path)

    asyncio.run(rt.chat([ChatMessage(role="user", content="hello")], session_id="s2"))

    assert adapter.last_messages is not None
    env_msg = adapter.last_messages[0]
    assert env_msg.role == "user"
    assert "Environment context:" in env_msg.content
    assert f"cwd: {tmp_path}" in env_msg.content
    assert "shell: zsh" in env_msg.content
    assert "timezone: Asia/Shanghai" in env_msg.content
    assert "current_date:" in env_msg.content
    assert adapter.last_messages[1].content == "hello"
