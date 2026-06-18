from __future__ import annotations

import asyncio
from pathlib import Path

from ziva_runtime.capabilities.registries import CapabilityRegistry
from ziva_runtime.config.loader import load_effective_config
from ziva_runtime.plugins.loader import load_plugins
from ziva_runtime.protocols.acp import ACPServer
from ziva_runtime.runtime import Runtime
from ziva_runtime.shared_types import ChatResult, ChatMessage


class FakeAdapter:
    def __init__(self):
        self.calls = 0

    async def chat(self, messages, model, system_prompt=None):
        self.calls += 1
        if self.calls == 1:
            return ChatResult(role="assistant", content='TOOL_CALL echo {"text":"smoke"}', model=model, usage={}, finish_reason="tool_call")
        return ChatResult(role="assistant", content="ok:done", model=model, usage={}, finish_reason="stop")


def test_config() -> None:
    base = Path("/tmp/ziva_smoke")
    base.mkdir(parents=True, exist_ok=True)
    global_cfg = base / "global.yaml"
    global_cfg.write_text("model:\n  provider: openai_agents\n  name: gpt-4.1\n", encoding="utf-8")
    cfg = load_effective_config(global_cfg, {"memory": {"backend": "sqlite"}})
    assert cfg["memory"]["backend"] == "sqlite"


def test_plugins() -> None:
    root = Path(__file__).resolve().parents[1]
    reg = CapabilityRegistry()
    manifests = load_plugins([root / "plugins"], reg)
    assert manifests
    assert any(r.kind == "tool" for r in reg.all())


async def test_acp() -> None:
    root = Path(__file__).resolve().parents[1]
    runtime = Runtime.create(workspace_root=root)
    server = ACPServer(runtime)
    rsp = await server.handle({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "chat",
        "params": {"messages": [{"role": "user", "content": "hello"}]},
    })
    assert rsp["result"]["message"]["content"] == "ok:done"


async def test_runtime_tool_loop() -> None:
    root = Path(__file__).resolve().parents[1]
    runtime = Runtime.create(workspace_root=root)
    rsp = await runtime.chat([ChatMessage(role="user", content="loop")], session_id="smoke-loop")
    assert rsp.content == "ok:done"


def main() -> None:
    test_config()
    test_plugins()
    asyncio.run(test_acp())
    asyncio.run(test_runtime_tool_loop())
    print("SMOKE_TEST_PASSED")


if __name__ == "__main__":
    main()
