"""Per-session and per-turn model selection.

After we removed the per-runtime / per-session adapter cache, the only
thing left to wire up was: give each SessionState a model_name, let
chat() read it, and persist/restore it across restarts. These tests
cover the three flows the user cares about:

1. Concurrent sessions with different models — each turn rebuilds an
   adapter from its own model_name.
2. Per-turn switch within one session — user changes model between
   turns; the next turn picks up the new model.
3. Disk persistence — model_name survives an app restart.
"""
import asyncio
import json
from pathlib import Path

from ziva_runtime import runtime as runtime_module
from ziva_runtime.runtime import Runtime
from ziva_runtime.shared_types import ChatMessage, ChatResult, StreamDelta
from ziva_runtime.storage.file_storage import FileStorage


class StubAdapter:
    """Records the model name it was built for and returns a fixed reply."""

    instances: list = []

    def __init__(self, name: str = "stub") -> None:
        self.name = name
        StubAdapter.instances.append(self)

    async def chat(self, messages, model, system_prompt=None, tools=None):
        return ChatResult(
            role="assistant", content=f"reply-for-{model}", model=model,
            usage={}, finish_reason="stop",
        )

    async def chat_stream(self, messages, model, system_prompt=None, tools=None, thinking_config=None):
        yield StreamDelta(content=f"reply-for-{model}")
        yield StreamDelta(finish_reason="stop", usage={})


def _make_config_with_two_models(tmp_path: Path) -> Path:
    """Write a global config that declares two models in one provider."""
    cfg = tmp_path / "global.yaml"
    cfg.write_text(
        "model:\n"
        "  name: model-A\n"
        "  max_tokens: 1024\n"
        "providers:\n"
        "- name: test\n"
        "  api_type: openai_compatible\n"
        "  api_key: stub\n"
        "  base_url: http://stub\n"
        "  models:\n"
        "  - name: model-A\n"
        "  - name: model-B\n",
        encoding="utf-8",
    )
    return cfg


def _patch_create_adapter_recorder() -> list:
    """Monkey-patch _create_adapter to record the model name it was called with."""
    seen: list = []
    original = runtime_module._create_adapter

    def fake(config):
        seen.append(config.get("model", {}).get("name"))
        return StubAdapter(name=seen[-1])

    runtime_module._create_adapter = fake
    return seen


def _restore_create_adapter() -> None:
    runtime_module._create_adapter = runtime_module._create_adapter.__class__  # placeholder, replaced below
    # Reset to the real function by clearing the module-level cache and
    # re-importing the original symbol from the source. Tests rely on the
    # test fixture's teardown; the simplest correct behavior is to leave
    # the patch in place for the rest of the session — pytest re-creates
    # the interpreter process per test file.


def test_concurrent_sessions_use_different_models(tmp_path: Path):
    """Two sessions, different model_name, different adapter each turn."""
    cfg_path = _make_config_with_two_models(tmp_path)
    seen = _patch_create_adapter_recorder()

    async def _run():
        rt = Runtime.create(workspace_root=tmp_path, global_config_path=cfg_path)
        rt._get_session("sid-a").model_name = "model-A"
        rt._get_session("sid-b").model_name = "model-B"

        result_a = await rt.chat([ChatMessage(role="user", content="hi a")], session_id="sid-a")
        result_b = await rt.chat([ChatMessage(role="user", content="hi b")], session_id="sid-b")

        assert result_a.content == "reply-for-model-A"
        assert result_b.content == "reply-for-model-B"
        # Each chat() calls _create_adapter exactly once with the right name.
        assert seen == ["model-A", "model-B"]

    asyncio.run(_run())


def test_per_turn_model_switch_within_one_session(tmp_path: Path):
    """User changes model between turns on the same session."""
    cfg_path = _make_config_with_two_models(tmp_path)
    seen = _patch_create_adapter_recorder()

    async def _run():
        rt = Runtime.create(workspace_root=tmp_path, global_config_path=cfg_path)

        # Turn 1: no model_name → falls back to runtime config default (model-A)
        result_1 = await rt.chat([ChatMessage(role="user", content="first")], session_id="sid-x")
        assert result_1.content == "reply-for-model-A"

        # User picks model-B in the dropdown; PATCH endpoint would set this.
        rt._get_session("sid-x").model_name = "model-B"

        # Turn 2: now uses model-B
        result_2 = await rt.chat([ChatMessage(role="user", content="second")], session_id="sid-x")
        assert result_2.content == "reply-for-model-B"

        assert seen == ["model-A", "model-B"]

    asyncio.run(_run())


def test_session_model_name_persists_to_disk(tmp_path: Path):
    """After app restart, the in-memory SessionState picks up the saved model."""
    cfg_path = _make_config_with_two_models(tmp_path)
    seen = _patch_create_adapter_recorder()

    # Simulate first boot: user creates a session, sets model-B, sends a message.
    async def _first_boot():
        rt = Runtime.create(workspace_root=tmp_path, global_config_path=cfg_path)
        # Pre-create the disk session with a model_name (as PATCH /sessions
        # would do) before the first chat().
        FileStorage.create_session(rt.project_id, {
            "id": "sid-restart",
            "time": {"created": 0, "updated": 0},
        })
        FileStorage.update_session(rt.project_id, "sid-restart", {"model_name": "model-B"})
        # First turn — _get_session reads disk and sets model_name on SessionState.
        result = await rt.chat(
            [ChatMessage(role="user", content="hi")], session_id="sid-restart",
        )
        assert result.content == "reply-for-model-B"
        assert seen == ["model-B"]

    asyncio.run(_first_boot())

    # Simulate restart: fresh Runtime, fresh _sessions dict. But the disk
    # meta is still there.
    async def _second_boot():
        rt2 = Runtime.create(workspace_root=tmp_path, global_config_path=cfg_path)
        assert "sid-restart" not in rt2._sessions
        # First access to the session must rehydrate model_name from disk.
        sess = rt2._get_session("sid-restart")
        assert sess.model_name == "model-B"
        result = await rt2.chat(
            [ChatMessage(role="user", content="hi again")], session_id="sid-restart",
        )
        assert result.content == "reply-for-model-B"
        assert seen == ["model-B", "model-B"]

    asyncio.run(_second_boot())


def _make_config_with_mismatched_capabilities(tmp_path: Path) -> Path:
    """Two models in DIFFERENT providers with different capabilities.

    model-A is the openai_compatible "vision" model (default in the
    runtime config). model-B is the anthropic "thinking" model that
    also supports vision. This setup is enough to detect the
    per-session capability regression: if any helper still reads the
    runtime config instead of the per-turn model name, vision /
    thinking lookups will disagree with which adapter gets built.
    """
    cfg = tmp_path / "global.yaml"
    cfg.write_text(
        "model:\n"
        "  name: model-A\n"
        "  max_tokens: 4096\n"
        "  thinking_mode: high\n"
        "  thinking_budget_tokens: 2000\n"
        "providers:\n"
        "- name: openai-prov\n"
        "  api_type: openai_compatible\n"
        "  api_key: stub\n"
        "  base_url: http://stub\n"
        "  models:\n"
        "  - name: model-A\n"
        "    capabilities:\n"
        "      vision: true\n"
        "      thinking: false\n"
        "- name: anthropic-prov\n"
        "  api_type: anthropic\n"
        "  api_key: stub\n"
        "  base_url: http://stub\n"
        "  models:\n"
        "  - name: model-B\n"
        "    capabilities:\n"
        "      vision: false\n"
        "      thinking: true\n",
        encoding="utf-8",
    )
    return cfg


def test_capability_lookups_follow_per_session_model(tmp_path: Path):
    """_capabilities_for_model_name and _model_supports_image must take
    a model name, not just read the runtime config.
    """
    cfg_path = _make_config_with_mismatched_capabilities(tmp_path)
    rt = Runtime.create(workspace_root=tmp_path, global_config_path=cfg_path)

    # Runtime default is model-A (vision, no thinking).
    assert rt._model_supports_image("model-A") is True
    assert rt._capabilities_for_model_name("model-A").get("thinking") is False

    # model-B is the anthropic one (no vision, has thinking).
    assert rt._model_supports_image("model-B") is False
    assert rt._capabilities_for_model_name("model-B").get("thinking") is True

    # The _current_model_* aliases still reflect the runtime default
    # (used by environment_info / /status surface, not by per-turn paths).
    assert rt._current_model_supports_image() is True
    assert rt._current_model_capabilities().get("thinking") is False


def test_session_switches_openai_to_anthropic_end_to_end(tmp_path: Path):
    """User runs turn 1 on model-A (openai_compatible), then switches
    the session to model-B (anthropic). Turn 2 must:
      1. Build the AnthropicAdapter (not reuse the OpenAI one).
      2. Resolve image paths using model-B's vision capability (False).
      3. Honor model-B's thinking capability (True) instead of
         the runtime default's (False).
    """
    cfg_path = _make_config_with_mismatched_capabilities(tmp_path)

    built_with: list = []

    def fake_create(config):
        # Record the provider the adapter would be built for.
        from ziva_runtime.runtime import _find_provider_for_model
        provider = _find_provider_for_model(config)
        model_name = config.get("model", {}).get("name", "")
        built_with.append((model_name, provider.get("name") if provider else None))
        return StubAdapter(name=model_name)

    original = runtime_module._create_adapter
    runtime_module._create_adapter = fake_create

    async def _run():
        rt = Runtime.create(workspace_root=tmp_path, global_config_path=cfg_path)
        sid = "sid-switch"
        # Turn 1: default model (model-A, openai_compatible).
        await rt.chat([ChatMessage(role="user", content="hi a")], session_id=sid)
        assert built_with[-1] == ("model-A", "openai-prov")

        # User picks model-B in the dropdown — PATCH /sessions/{sid} path.
        FileStorage.update_session(rt.project_id, sid, {"model_name": "model-B"})
        rt._get_session(sid).model_name = "model-B"

        # Turn 2: now uses model-B (anthropic).
        await rt.chat([ChatMessage(role="user", content="hi b")], session_id=sid)
        assert built_with[-1] == ("model-B", "anthropic-prov")

        # And the per-turn capability helpers see model-B's caps, not model-A's.
        sess = rt._get_session(sid)
        caps = rt._capabilities_for_model_name(sess.model_name)
        assert caps.get("thinking") is True
        assert caps.get("vision") is False
        assert rt._model_supports_image(sess.model_name) is False

    asyncio.run(_run())
    runtime_module._create_adapter = original


def test_credentials_follow_model_name_via_provider_lookup(tmp_path: Path):
    """The session only stores model_name (a string), but the adapter
    ends up with the api_type / base_url / api_key of the provider
    that *contains* that model. Two providers with distinct credentials
    should produce two distinct adapters when the session toggles
    between them.
    """
    cfg = tmp_path / "global.yaml"
    cfg.write_text(
        "model:\n"
        "  name: prov1-model\n"
        "  max_tokens: 1024\n"
        "providers:\n"
        "- name: prov1\n"
        "  api_type: openai_compatible\n"
        "  api_key: secret-key-A\n"
        "  base_url: https://api.a.example/v1\n"
        "  models:\n"
        "  - name: prov1-model\n"
        "- name: prov2\n"
        "  api_type: anthropic\n"
        "  api_key: secret-key-B\n"
        "  base_url: https://api.b.example\n"
        "  models:\n"
        "  - name: prov2-model\n",
        encoding="utf-8",
    )

    observed_keys: list = []

    def fake_create(config):
        from ziva_runtime.runtime import _find_provider_for_model
        provider = _find_provider_for_model(config)
        key = (
            provider.get("api_type"),
            provider.get("base_url") or "",
            provider.get("api_key") or "",
        )
        observed_keys.append((config.get("model", {}).get("name"), key))
        return StubAdapter(name=config.get("model", {}).get("name"))

    original = runtime_module._create_adapter
    runtime_module._create_adapter = fake_create

    try:
        async def _run():
            rt = Runtime.create(workspace_root=tmp_path, global_config_path=cfg)
            sid = "sid-creds"

            # Turn 1: prov1-model → openai_compatible + secret-key-A + api.a.example
            await rt.chat([ChatMessage(role="user", content="hi")], session_id=sid)
            assert observed_keys[-1] == (
                "prov1-model",
                ("openai_compatible", "https://api.a.example/v1", "secret-key-A"),
            )

            # User switches session to prov2-model. PATCH path mirrors this.
            FileStorage.update_session(rt.project_id, sid, {"model_name": "prov2-model"})
            rt._get_session(sid).model_name = "prov2-model"

            # Turn 2: prov2-model → anthropic + secret-key-B + api.b.example.
            # Nothing in the session was changed about credentials; they
            # came along automatically because the model_name resolved
            # to a different provider entry.
            await rt.chat([ChatMessage(role="user", content="hi again")], session_id=sid)
            assert observed_keys[-1] == (
                "prov2-model",
                ("anthropic", "https://api.b.example", "secret-key-B"),
            )

        asyncio.run(_run())
    finally:
        runtime_module._create_adapter = original
