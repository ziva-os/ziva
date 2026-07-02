"""Regression test: get_agent_result must not be cut off by the
tool-executor timeout.

get_agent_result(agent_id=..., block=true, timeout=600000) is the
canonical way to wait for a background sub-agent. The caller controls
the wait length via the `timeout` argument (default 30s, capped at
600s = 10 minutes). A 120s default executor timeout races that and
converts a still-running agent into a bogus
``Error: timeout\nTool 'get_agent_result' timed out after 120s``
tool_result — which is exactly the failure mode the user reported
(see the screenshot: they passed timeout=600000 and the executor
still cut them off at 120s).

This test pins down the fix at ``runtime.py:_execute_tool``:
get_agent_result now sits in the same no-timeout whitelist as
spawn_agent / ask_user, so a tight ``default`` executor timeout
can't terminate a longer wait. The tool itself enforces its own
per-call bound (the `timeout` arg, capped at 600000ms).

We exercise the executor directly with a hand-rolled stub rather
than the real plugin — the goal is to verify the runtime's
timeout policy, not the plugin's internals.
"""

import asyncio
from pathlib import Path
from typing import Any, Dict

from ziva.capabilities.registries import CapabilityRegistry
from ziva.runtime import Runtime
from ziva.shared_types import (
    RuntimeContext,
    ToolCall,
    ToolResult,
)


class FakeGetAgentResultTool:
    """Stand-in get_agent_result that blocks on a per-call future.

    Mirrors the contract of the real ``get_agent_result`` impl when
    ``block=true``: ``run()`` awaits a long-lived future that the
    runtime (or the cancel endpoint) is responsible for releasing.
    The ``timeout`` argument is honored internally by the tool — it
    is NOT the executor's job to enforce it.
    """

    def __init__(self) -> None:
        self.released = False
        self.release_event = asyncio.Event()

    def spec(self) -> Dict[str, Any]:
        return {
            "name": "get_agent_result",
            "description": "test stub",
            "input_schema": {
                "type": "object",
                "properties": {
                    "agent_id": {"type": "string"},
                    "block": {"type": "boolean"},
                    "timeout": {"type": "integer"},
                },
                "required": ["agent_id"],
            },
        }

    async def run(self, input_data: Dict[str, Any], ctx: RuntimeContext) -> ToolResult:
        # Honor a 6-second internal cap; if the executor enforces a
        # tighter timeout (the bug we're guarding against) the call
        # surfaces a synthetic "Error: timeout" before we get here.
        await asyncio.wait_for(self.release_event.wait(), timeout=6.0)
        return ToolResult(text=f"Agent {input_data.get('agent_id')} completed")


def _build_runtime(adapter=None, default_timeout: int = 1) -> Runtime:
    """Construct a Runtime that uses our FakeGetAgentResultTool.

    Bypasses the real plugin loader by registering the stub tool
    directly. The rest of the plugin tree is not relevant for this
    test, so we leave it empty.
    """
    root = Path(__file__).resolve().parents[1]
    rt = Runtime.create(
        workspace_root=root,
        session_override={
            "tool": {
                "allow": [],
                "deny": [],
                # Tight 1s default — any wait > 1s that gets cut off
                # proves the fix regressed.
                "timeouts": {"default": default_timeout},
                "max_rounds": 0,
            },
            "approval": {"policy": "full-auto"},
        },
    )
    rt.registry.register(
        capability_id="tool.get_agent_result",
        kind="tool",
        instance=FakeGetAgentResultTool(),
        manifest={
            "version": "0.0.0",
            "permissions": {"tool": ["get_agent_result"]},
            "enabled_by_default": True,
            "path": "test",
        },
    )
    return rt


def test_get_agent_result_outlives_default_timeout():
    """A 3s get_agent_result wait must survive a 1s default executor timeout."""

    async def _run():
        rt = _build_runtime(default_timeout=1)
        sid = "get-agent-result-3s-wait"
        ctx = RuntimeContext(
            session_id=sid,
            config=rt.config,
            metadata={"_runtime": rt},
        )

        exec_task = asyncio.create_task(
            rt._execute_tool(
                ToolCall(
                    name="get_agent_result",
                    arguments={"agent_id": "bg_test", "block": True, "timeout": 600000},
                ),
                ctx,
            )
        )

        # Wait well past the 1s default timeout. If the executor
        # applies the timeout to get_agent_result, exec_task will
        # resolve with an "Error: timeout" ToolResult before we
        # get here.
        await asyncio.sleep(3.0)
        assert not exec_task.done(), (
            "get_agent_result was preempted by the 1s executor timeout; "
            "the tool should keep waiting until its own 600s cap."
        )

        # Release the future, mirroring agent completion.
        fake = rt.registry.list_kind("tool")
        for tool_rec in fake:
            if tool_rec.instance.spec().get("name") == "get_agent_result":
                tool_rec.instance.release_event.set()
                break

        result = await asyncio.wait_for(exec_task, timeout=2.0)
        assert result.error is False, f"unexpected error result: {result.text!r}"
        assert "Error: timeout" not in result.text
        assert "completed" in result.text

    asyncio.run(_run())


def test_non_whitelisted_tool_still_respects_timeout():
    """Sanity check: ordinary tools ARE still subject to the timeout.

    A short sleep above the default timeout must surface as an
    "Error: timeout" ToolResult. This guards against an over-broad
    fix that just disables wait_for entirely.
    """

    class SleepyTool:
        def spec(self) -> Dict[str, Any]:
            return {
                "name": "sleepy",
                "description": "test stub that sleeps",
                "input_schema": {"type": "object", "properties": {}},
            }

        async def run(self, input_data: Dict[str, Any], ctx: RuntimeContext) -> ToolResult:
            await asyncio.sleep(3.0)
            return ToolResult(text="woke up")

    async def _run():
        rt = _build_runtime(default_timeout=1)
        rt.registry.register(
            capability_id="tool.sleepy",
            kind="tool",
            instance=SleepyTool(),
            manifest={
                "version": "0.0.0",
                "permissions": {"tool": ["sleepy"]},
                "enabled_by_default": True,
                "path": "test",
            },
        )
        sid = "sleepy-tool-test"
        ctx = RuntimeContext(session_id=sid, config=rt.config, metadata={})
        result = await rt._execute_tool(
            ToolCall(name="sleepy", arguments={}), ctx
        )
        assert result.error is True
        assert "timed out" in result.text

    asyncio.run(_run())
