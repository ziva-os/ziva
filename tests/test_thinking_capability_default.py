"""Tests for the thinking-capability decision logic.

Why this file exists
--------------------
User-reported regression: setting `thinking_mode: high` globally in the
runtime config did not enable thinking for kimi-k2.6, because the model
entry had no `capabilities.thinking` flag and the previous code treated
"missing" as "no thinking". The fix flips `capabilities.thinking` to a
*ceiling* declaration rather than a *switch*:

  - caps.thinking = False  → model declares "I do not support thinking";
                             overrides user intent in either direction.
  - caps.thinking = True   → model declares "I support thinking"; this
                             is permissive (lets the user toggle on),
                             not coercive (does NOT force-enable when
                             the user has turned the global switch off).
  - caps.thinking missing  → no opinion; defer entirely to the user.

These tests pin that contract:

  caps.thinking = False                  → blocked, even if user wants it
  caps.thinking = True + user_enabled    → enabled
  caps.thinking = True + user_disabled   → blocked  (cap does NOT override user off)
  caps.thinking = None + user_opted_in   → enabled (was broken — the regression)
  caps.thinking = None + user_disabled   → blocked
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import AsyncIterator, List

import pytest

from ziva_runtime.runtime import Runtime
from ziva_runtime.shared_types import ChatMessage, StreamDelta


class _ThinkingProbe:
    """Adapter that records the `thinking_config` it was called with so
    we can assert what the runtime actually decided — not just whether a
    call happened."""

    instances: list = []

    def __init__(self, name: str = "probe") -> None:
        self.name = name
        self.last_thinking_config = None
        _ThinkingProbe.instances.append(self)

    async def chat(self, messages, model, system_prompt=None, tools=None, thinking_config=None):
        from ziva_runtime.shared_types import ChatResult
        self.last_thinking_config = thinking_config
        return ChatResult(role="assistant", content="ok", model=model, usage={}, finish_reason="stop")

    async def chat_stream(self, messages, model, system_prompt=None, tools=None, thinking_config=None):
        self.last_thinking_config = thinking_config
        yield StreamDelta(content="ok", finish_reason="stop")


def _write_cfg(tmp_path: Path, *, default_model: str, model_entries: list, global_thinking_mode: str | None) -> Path:
    """Compose a minimal config yaml with the given model list and
    optional global thinking_mode. Each model entry is a dict that will
    be placed under providers[0].models verbatim, so the test can omit
    or include `capabilities.thinking` per the scenario under test.
    """
    parts = ["model:"]
    parts.append(f"  name: {default_model}")
    parts.append("  max_tokens: 4096")
    if global_thinking_mode is not None:
        parts.append(f"  thinking_mode: {global_thinking_mode}")
        parts.append("  thinking_budget_tokens: 2000")
    parts.append("providers:")
    parts.append("- name: test-prov")
    parts.append("  api_type: anthropic")
    parts.append("  api_key: stub")
    parts.append("  base_url: http://stub")
    parts.append("  models:")
    for m in model_entries:
        if "capabilities" in m:
            caps = m["capabilities"]
            parts.append(f"  - name: {m['name']}")
            parts.append("    capabilities:")
            for k, v in caps.items():
                parts.append(f"      {k}: {str(v).lower()}")
        else:
            parts.append(f"  - name: {m['name']}")
    cfg = tmp_path / "global.yaml"
    cfg.write_text("\n".join(parts) + "\n", encoding="utf-8")
    return cfg


@pytest.fixture(autouse=True)
def _patch_create_adapter():
    """Force _create_adapter to return _ThinkingProbe so we can read off
    the thinking_config the runtime actually decided to send."""
    from ziva_runtime import runtime as runtime_module

    _ThinkingProbe.instances.clear()
    original = runtime_module._create_adapter

    def fake(config):
        return _ThinkingProbe(name=config.get("model", {}).get("name", ""))

    runtime_module._create_adapter = fake
    yield
    runtime_module._create_adapter = original


# ---------------------------------------------------------------------------
# The regression: missing capabilities + thinking_mode=high → enabled
# ---------------------------------------------------------------------------

def test_missing_caps_with_thinking_mode_high_enables_thinking(tmp_path):
    """This is the kimi-k2.6 case directly. No `capabilities` block on
    the model, but the user has `thinking_mode: high` set. The runtime
    MUST treat that as intent and pass a thinking_config through.
    Previously it passed `None`, so no thinking blocks came back.
    """
    cfg = _write_cfg(
        tmp_path,
        default_model="kimi-k2.6",
        model_entries=[{"name": "kimi-k2.6"}],   # <-- no capabilities
        global_thinking_mode="high",
    )
    rt = Runtime.create(workspace_root=tmp_path, global_config_path=cfg)

    async def _run():
        await rt.chat([ChatMessage(role="user", content="hi")], session_id="sid-regression")

    asyncio.run(_run())

    assert len(_ThinkingProbe.instances) == 1, "adapter was not built"
    probe = _ThinkingProbe.instances[0]
    assert probe.last_thinking_config is not None, (
        "thinking_config should be set when thinking_mode is enabled and "
        "the model has no capabilities.thinking flag. Previously None → "
        "no thinking blocks returned by anthropic-format APIs."
    )
    assert probe.last_thinking_config.get("type") == "enabled"
    assert probe.last_thinking_config.get("budget_tokens") == 2000
    assert probe.last_thinking_config.get("mode") == "high"


# ---------------------------------------------------------------------------
# Pin the rest of the decision matrix so a future refactor doesn't
# silently flip a behavior.
# ---------------------------------------------------------------------------

def test_explicit_caps_thinking_false_blocks_even_when_user_enabled(tmp_path):
    """User wants thinking but the provider/model has explicitly opted
    out. Honoring the explicit opt-out lets users override a global
    thinking_mode: high by declaring a particular model as
    non-thinking. This stays the same as before.
    """
    cfg = _write_cfg(
        tmp_path,
        default_model="plain-model",
        model_entries=[
            {"name": "plain-model", "capabilities": {"thinking": False}},
        ],
        global_thinking_mode="high",
    )
    rt = Runtime.create(workspace_root=tmp_path, global_config_path=cfg)

    async def _run():
        await rt.chat([ChatMessage(role="user", content="hi")], session_id="sid")

    asyncio.run(_run())
    assert _ThinkingProbe.instances[0].last_thinking_config is None


def test_explicit_caps_thinking_true_respects_user_disabled(tmp_path):
    """A model that says "I support thinking" is permissive, not
    coercive. If the user has globally disabled thinking, that switch
    must still win — the only cap that overrides user intent is an
    explicit `capabilities.thinking: false` (handled by the test above).
    """
    cfg = _write_cfg(
        tmp_path,
        default_model="deep-thinker",
        model_entries=[
            {"name": "deep-thinker", "capabilities": {"thinking": True}},
        ],
        global_thinking_mode="disabled",
    )
    rt = Runtime.create(workspace_root=tmp_path, global_config_path=cfg)

    async def _run():
        await rt.chat([ChatMessage(role="user", content="hi")], session_id="sid")

    asyncio.run(_run())
    assert _ThinkingProbe.instances[0].last_thinking_config is None, (
        "capabilities.thinking: true must NOT override an explicit user "
        "opt-out; only capabilities.thinking: false can do that."
    )


def test_explicit_caps_thinking_true_with_user_enabled_enables(tmp_path):
    """The companion case to the test above: when caps=True and the user
    has thinking_mode set, thinking should be enabled. This is the
    everyday happy path for a model that explicitly advertises support.
    """
    cfg = _write_cfg(
        tmp_path,
        default_model="deep-thinker",
        model_entries=[
            {"name": "deep-thinker", "capabilities": {"thinking": True}},
        ],
        global_thinking_mode="high",
    )
    rt = Runtime.create(workspace_root=tmp_path, global_config_path=cfg)

    async def _run():
        await rt.chat([ChatMessage(role="user", content="hi")], session_id="sid")

    asyncio.run(_run())
    probe = _ThinkingProbe.instances[0]
    assert probe.last_thinking_config is not None
    assert probe.last_thinking_config.get("type") == "enabled"
    assert probe.last_thinking_config.get("mode") == "high"


def test_missing_caps_with_thinking_mode_disabled_blocks_thinking(tmp_path):
    """When the user has explicitly disabled thinking globally AND the
    model has no opinion (missing capabilities), don't enable it. This
    matches user intent: "I do not want thinking, don't second-guess me."
    """
    cfg = _write_cfg(
        tmp_path,
        default_model="kimi-k2.6",
        model_entries=[{"name": "kimi-k2.6"}],   # no capabilities
        global_thinking_mode="disabled",
    )
    rt = Runtime.create(workspace_root=tmp_path, global_config_path=cfg)

    async def _run():
        await rt.chat([ChatMessage(role="user", content="hi")], session_id="sid")

    asyncio.run(_run())
    assert _ThinkingProbe.instances[0].last_thinking_config is None


def test_missing_caps_with_thinking_mode_unset_blocks_thinking(tmp_path):
    """No `thinking_mode` in config at all → no thinking. This is the
    default state of a fresh install; we shouldn't surprise the user
    by auto-enabling thinking when they haven't asked.
    """
    cfg = _write_cfg(
        tmp_path,
        default_model="kimi-k2.6",
        model_entries=[{"name": "kimi-k2.6"}],   # no capabilities
        global_thinking_mode=None,
    )
    rt = Runtime.create(workspace_root=tmp_path, global_config_path=cfg)

    async def _run():
        await rt.chat([ChatMessage(role="user", content="hi")], session_id="sid")

    asyncio.run(_run())
    assert _ThinkingProbe.instances[0].last_thinking_config is None


# ---------------------------------------------------------------------------
# Per-turn model override: a session pinned to a non-thinking model
# must NOT get thinking blocks even if the runtime default is enabled.
# ---------------------------------------------------------------------------

def test_per_session_thinking_opt_out_is_honored(tmp_path):
    """User flips `thinking_mode: high` globally, but pin a session to a
    model with `capabilities.thinking: false`. The session-level pin
    must override the runtime default (matches the "Capability lookup
    is parameterized on this turn's model name" comment in runtime.py).
    """
    from ziva_runtime.storage.file_storage import FileStorage

    cfg = _write_cfg(
        tmp_path,
        default_model="kimi-k2.6",
        model_entries=[
            {"name": "kimi-k2.6"},   # no capabilities — would inherit enabled
            {"name": "vanilla", "capabilities": {"thinking": False}},
        ],
        global_thinking_mode="high",
    )
    rt = Runtime.create(workspace_root=tmp_path, global_config_path=cfg)

    async def _run():
        # Pin a session to the "vanilla" model (explicit opt-out).
        FileStorage.create_session(rt.project_id, {"id": "sid", "time": {}})
        FileStorage.update_session(rt.project_id, "sid", {"model_name": "vanilla"})
        await rt.chat([ChatMessage(role="user", content="hi")], session_id="sid")

    asyncio.run(_run())
    probe = _ThinkingProbe.instances[0]
    assert probe.name == "vanilla"
    assert probe.last_thinking_config is None, (
        "explicit capabilities.thinking: false on the per-session model "
        "must override the global thinking_mode: high"
    )
