"""Tests for the high-level ziva.Agent SDK API.

Covers: Agent construction with injectable storage, api_key provider wiring,
env-var fallback, anthropic base_url detection, chat() + stream() against a
fake adapter, register_tool(), and InMemoryStorage round-trip — all with no
~/.ziva on disk (storage=InMemoryStorage()).
"""

from __future__ import annotations

import asyncio

import pytest

import ziva
from ziva import Agent, InMemoryStorage
import ziva.runtime as runtime_module
from ziva.shared_types import ChatResult, StreamDelta, ToolResult


REPLY = "hello from the fake model"


class _FakeAdapter:
    """Minimal adapter matching the runtime loop's contract (chat_stream)."""

    async def chat(self, messages, model, system_prompt=None, tools=None, thinking_config=None):
        return ChatResult(role="assistant", content=REPLY, model=model, usage={}, finish_reason="stop")

    async def chat_stream(self, messages, model, system_prompt=None, tools=None, thinking_config=None):
        yield StreamDelta(content=REPLY, finish_reason="stop")


@pytest.fixture
def fake_adapter(monkeypatch):
    """Point the runtime's adapter factory at our in-memory fake."""
    monkeypatch.setattr(runtime_module, "_create_adapter", lambda config: _FakeAdapter())
    return _FakeAdapter()


def _agent(**kw):
    defaults = dict(model="gpt-4.1", api_key="sk-test", storage=InMemoryStorage(), load_default_plugins=False)
    defaults.update(kw)
    return Agent(**defaults)


# ---- construction & config wiring ----

def test_agent_constructs_with_inmemory_storage():
    a = _agent()
    assert a.runtime.storage.__class__.__name__ == "InMemoryStorage"
    assert a.config["model"]["name"] == "gpt-4.1"
    # api_key attached to the provider that owns gpt-4.1
    assert a.config["providers"][0]["api_key"] == "sk-test"


def test_agent_synthesizes_provider_for_unknown_model():
    a = _agent(model="some-custom-model", api_key="k", base_url="https://api.example.com/v1")
    provs = [p for p in a.config["providers"] if any(m["name"] == "some-custom-model" for m in p.get("models", []))]
    assert len(provs) == 1
    assert provs[0]["api_key"] == "k"
    assert provs[0]["base_url"] == "https://api.example.com/v1"
    assert provs[0]["api_type"] == "openai_compatible"


def test_agent_detects_anthropic_base_url(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    a = _agent(model="claude-test", base_url="https://api.anthropic.com")
    provs = [p for p in a.config["providers"] if p.get("api_type") == "anthropic"]
    assert len(provs) >= 1


def test_agent_falls_back_to_openai_env_api_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "env-key")
    a = Agent(model="gpt-4.1", storage=InMemoryStorage(), load_default_plugins=False)
    assert a.config["providers"][0]["api_key"] == "env-key"


def test_agent_accepts_explicit_config_dict():
    cfg = {
        "model": {"name": "gpt-4.1", "max_tokens": 8192, "thinking_mode": "disabled", "thinking_budget_tokens": 4000},
        "providers": [{"name": "OpenAI", "api_type": "openai_compatible", "api_key": "k", "base_url": "", "models": [{"name": "gpt-4.1"}]}],
        "prompt": {"profile": "default", "variables": {}},
        "tool": {"allow": [], "deny": [], "max_rounds": 0},
        "skill": {"enabled": [], "extra_paths": []},
        "hooks": {"before_turn": [], "after_turn": [], "before_tool": [], "after_tool": []},
        "memory": {"backend": "inmemory", "context_window_tokens": 200000},
        "plugin": {"paths": [], "trust": {"unsigned": "low"}},
        "approval": {"policy": "suggest", "allow_without_prompt": []},
        "sandbox": {"mode": "off", "writable_dirs": [], "blocked_commands": []},
        "mcp": {"enabled": False, "servers": [], "extra_skill_paths": []},
        "stt": {"model": "mlx-community/whisper-small-mlx"},
        "spawn": {"max_concurrency": 20, "max_history": 50},
        "agents": {},
    }
    a = Agent(config=cfg, storage=InMemoryStorage(), load_default_plugins=False)
    assert a.config["model"]["name"] == "gpt-4.1"


# ---- conversation ----

def test_agent_chat_returns_chat_result(fake_adapter):
    async def _run():
        a = _agent()
        sid = a.new_session()
        result = await a.chat("hi", session_id=sid)
        assert isinstance(result, ChatResult)
        assert result.content == REPLY
        return a, sid

    a, sid = asyncio.run(_run())
    # The turn must have persisted the exchange into the injected storage.
    msgs = list(a.runtime.storage.get_messages(a.runtime.project_id, sid))
    roles = [m.get("role") for m in msgs]
    assert "user" in roles and "assistant" in roles


def test_agent_stream_yields_events(fake_adapter):
    events: list = []

    async def _run():
        a = _agent()
        sid = a.new_session()
        async for ev in a.stream("hi", session_id=sid):
            events.append(ev.get("type"))

    asyncio.run(_run())
    assert "delta" in events or "model_response" in events
    assert "turn_end" in events or "round_complete" in events


# ---- tools ----

class _EchoTool:
    def spec(self):
        return {
            "name": "echo",
            "description": "echo",
            "input_schema": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
        }

    async def run(self, arguments, ctx=None):
        return ToolResult(text=str(arguments.get("text", "")))


def test_agent_register_tool():
    a = _agent()
    a.register_tool(_EchoTool())
    ids = [c.id for c in a.runtime.registry.all()]
    assert "tool.echo" in ids


# ---- InMemoryStorage round-trip ----

def test_inmemory_storage_roundtrip():
    s = InMemoryStorage()
    s.create_session("proj", {"id": "s1", "time": {"updated": 1}})
    s.append_message("proj", "s1", {"id": "m1", "role": "user", "content": "hi"})
    s.append_message("proj", "s1", {"id": "m2", "role": "assistant", "content": "yo"})
    msgs = list(s.get_messages("proj", "s1"))
    assert len(msgs) == 2 and msgs[0]["content"] == "hi"
    assert s.get_session("proj", "s1")["id"] == "s1"
    assert len(s.list_sessions("proj")) == 1
    s.delete_session("proj", "s1")
    assert s.get_session("proj", "s1") is None
    assert list(s.get_messages("proj", "s1")) == []


def test_public_api_exports():
    for sym in ["Agent", "Runtime", "FileStorage", "InMemoryStorage", "Storage",
                "PermissionManager", "CapabilityRegistry", "ChatMessage", "ChatResult",
                "ToolResult", "Tool", "Skill", "Hook", "MemoryStore", "PromptProvider"]:
        assert hasattr(ziva, sym), f"ziva.{sym} missing"
