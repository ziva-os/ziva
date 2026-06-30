"""Regression test matching the user's actual ~/.ziva/config.yaml.

Reproduces the exact setup from the user's environment (paraphrased to
strip the real API keys) and asserts that thinking is correctly enabled
for kimi-k2.6 after the capability-default fix.

Before fix: thinking_config was None → no thinking blocks came back.
After fix: thinking_config is set → adapter sends `thinking=` to the
Anthropic-format endpoint → user's thinking card now renders.
"""
import asyncio
from pathlib import Path

from ziva_runtime.runtime import Runtime
from ziva_runtime.shared_types import ChatMessage, StreamDelta


# Mirrors the user's ~/.ziva/config.yaml structure: default model
# "Kimi-K2.6" with global thinking_mode: high, anthropic provider
# "Kimi" whose model entries do NOT carry a capabilities block.
_USER_LIKE_CONFIG = """
model:
  name: Kimi-K2.6
  max_tokens: 64000
  thinking_mode: high
  thinking_budget_tokens: 4000
providers:
- name: MiniMax
  api_type: openai_compatible
  api_key: stub
  base_url: http://stub
  models:
  - name: MiniMax-M3
    supports_image: true
  - name: MiniMax-M2.7
- name: Kimi
  api_type: anthropic
  api_key: stub
  base_url: http://stub
  models:
  - name: kimi-k2.6
    supports_image: true
  - name: kimi-k2.7-code
    supports_image: true
prompt:
  profile: default
"""


class _ProbeAdapter:
    """Record what thinking_config the runtime sends, then yield some
    synthetic text so the turn completes."""
    instances: list = []

    def __init__(self, name: str = "probe") -> None:
        self.name = name
        self.last_thinking_config = None
        _ProbeAdapter.instances.append(self)

    async def chat(self, messages, model, system_prompt=None, tools=None, thinking_config=None):
        from ziva_runtime.shared_types import ChatResult
        self.last_thinking_config = thinking_config
        return ChatResult(role="assistant", content="ok", model=model, usage={}, finish_reason="stop")

    async def chat_stream(self, messages, model, system_prompt=None, tools=None, thinking_config=None):
        self.last_thinking_config = thinking_config
        # Realistic Anthropic thinking block + text response.
        yield StreamDelta(content="", reasoning_signature="sig-from-kimi")
        yield StreamDelta(content="", reasoning_content="chain of thought")
        yield StreamDelta(content="hello")
        yield StreamDelta(content="", finish_reason="end_turn", usage={})


def test_user_config_enables_thinking_for_kimi_k26():
    """Drives the exact bug the user hit. Reproduces the on-disk config
    layout (no `capabilities` block on the kimi models) and asserts
    that the runtime now passes a populated `thinking_config` through
    to the Anthropic adapter."""
    from ziva_runtime import runtime as runtime_module

    _ProbeAdapter.instances.clear()

    # Layout the user's config under a tmp path so Runtime.create picks
    # it up via global_config_path.
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        cfg_path = Path(tmp) / "global.yaml"
        cfg_path.write_text(_USER_LIKE_CONFIG, encoding="utf-8")

        original = runtime_module._create_adapter
        runtime_module._create_adapter = lambda config: _ProbeAdapter(
            name=config.get("model", {}).get("name", ""),
        )
        try:
            rt = Runtime.create(workspace_root=Path(tmp), global_config_path=cfg_path)

            async def _run():
                await rt.chat(
                    [ChatMessage(role="user", content="hi")], session_id="sid"
                )
            asyncio.run(_run())

            assert _ProbeAdapter.instances, "adapter was never constructed"
            probe = _ProbeAdapter.instances[0]
            assert probe.name == "Kimi-K2.6", (
                f"expected default model name to be used, got {probe.name!r}"
            )
            assert probe.last_thinking_config is not None, (
                "thinking_config should be passed through to the Anthropic "
                "adapter for kimi-k2.6 when the user has thinking_mode: high "
                "set globally. This was the user's reported regression."
            )
            tc = probe.last_thinking_config
            assert tc.get("type") == "enabled"
            assert tc.get("budget_tokens") == 4000
            assert tc.get("mode") == "high"
        finally:
            runtime_module._create_adapter = original


def test_user_config_switches_session_to_kimi_k27_code_keeps_thinking():
    """User's second model is `kimi-k2.7-code` (also no capabilities
    block). When the session is pinned to that model (the "kimi-k2.7-code
    reverts to default model after restart" scenario from issue #3),
    thinking should still be enabled."""
    from ziva_runtime import runtime as runtime_module
    from ziva_runtime.storage.file_storage import FileStorage

    _ProbeAdapter.instances.clear()

    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        cfg_path = Path(tmp) / "global.yaml"
        cfg_path.write_text(_USER_LIKE_CONFIG, encoding="utf-8")

        original = runtime_module._create_adapter
        runtime_module._create_adapter = lambda config: _ProbeAdapter(
            name=config.get("model", {}).get("name", ""),
        )
        try:
            rt = Runtime.create(workspace_root=Path(tmp), global_config_path=cfg_path)

            # Pin a fresh session to kimi-k2.7-code (as if the user
            # picked it in the dropdown).
            FileStorage.create_session(rt.project_id, {"id": "sid-77", "time": {}})
            FileStorage.update_session(
                rt.project_id, "sid-77", {"model_name": "kimi-k2.7-code"}
            )

            async def _run():
                await rt.chat(
                    [ChatMessage(role="user", content="hi")], session_id="sid-77"
                )
            asyncio.run(_run())

            assert _ProbeAdapter.instances, "adapter was never constructed"
            probe = _ProbeAdapter.instances[0]
            assert probe.name == "kimi-k2.7-code"
            assert probe.last_thinking_config is not None, (
                "kimi-k2.7-code has no capabilities block in the user's "
                "config; thinking should still be enabled because the "
                "global thinking_mode: high + no explicit caps.thinking=False"
                " leaves capability lookup permissive."
            )
            assert probe.last_thinking_config.get("type") == "enabled"
        finally:
            runtime_module._create_adapter = original
