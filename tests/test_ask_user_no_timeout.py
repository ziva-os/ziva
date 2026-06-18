"""Regression test: ask_user must not be cut off by the tool-executor timeout.

The ask_user tool blocks on a per-session future that the UI (or the
``cancel_turn`` endpoint) resolves. Its whole purpose is to keep the
model round open until the user actually replies. A 120s default
executor timeout races the UI and converts a still-pending question
into a bogus ``Error: timeout`` tool_result, which the model then
treats as a user reply and writes a new answer to.

This test pins down the fix at ``runtime.py:_execute_tool``: ask_user
sits in the same no-timeout whitelist as invoke_subagent / spawn_agent,
so a tight ``default`` executor timeout can't terminate a longer wait.

We exercise the executor directly with a hand-rolled AskUserTool rather
than the real plugin — the goal is to verify the runtime's timeout
policy, not the plugin's internals (which have their own coverage).
"""

import asyncio
from pathlib import Path
from typing import Any, Dict

from ziva_runtime.capabilities.registries import CapabilityRegistry
from ziva_runtime.runtime import Runtime
from ziva_runtime.shared_types import (
    RuntimeContext,
    ToolCall,
    ToolResult,
)

class FakeAskUserTool:
    """A stand-in ask_user tool that blocks on a per-call future.

    Mirrors the contract of the real ``ask_user`` impl: the
    ``run()`` coroutine awaits ``runtime.await_user_answer(...)`` and
    only returns once ``runtime.set_user_answer(...)`` releases it.
    This is the property the executor's ``wait_for`` must NOT preempt.
    """

    def spec(self) -> Dict[str, Any]:
        return {
            "name": "ask_user",
            "description": "test stub",
            "input_schema": {
                "type": "object",
                "properties": {"question": {"type": "string"}},
                "required": ["question"],
            },
        }

    async def run(self, input_data: Dict[str, Any], ctx: RuntimeContext) -> ToolResult:
        runtime = ctx.metadata.get("_runtime")
        call_id = (ctx.metadata or {}).get("_tool_call_id", "")
        raw = await runtime.await_user_answer(
            session_id=ctx.session_id, call_id=call_id
        )
        if isinstance(raw, dict) and raw.get("status") == "answered":
            return ToolResult(
                text=f"User answered: {raw.get('answer', '')}",
                metadata=raw,
            )
        return ToolResult(text=str(raw), metadata={"raw": raw})

def _build_runtime(adapter=None, default_timeout: int = 1) -> Runtime:
    """Construct a Runtime that uses our FakeAskUserTool.

    Bypasses the real plugin loader by registering the stub tool
    directly. The rest of the plugin tree (echo, read_file, etc.) is
    not relevant for this test, so we leave it empty.
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
        capability_id="tool.ask_user",
        kind="tool",
        instance=FakeAskUserTool(),
        manifest={
            "version": "0.0.0",
            "permissions": {"tool": ["ask_user"]},
            "enabled_by_default": True,
            "path": "test",
        },
    )
    return rt

def test_ask_user_outlives_default_timeout():
    """A 3s ask_user wait must survive a 1s default executor timeout."""

    async def _run():
        rt = _build_runtime(default_timeout=1)
        sid = "ask-user-3s-wait"
        ctx = RuntimeContext(
            session_id=sid,
            config=rt.config,
            metadata={"_runtime": rt, "_tool_call_id": "call_1"},
        )

        exec_task = asyncio.create_task(
            rt._execute_tool(
                ToolCall(
                    name="ask_user",
                    arguments={"question": "still there?"},
                ),
                ctx,
            )
        )

        # Wait well past the 1s default timeout. If the executor
        # applies the timeout to ask_user, exec_task will resolve
        # with an "Error: timeout" ToolResult before we get here.
        await asyncio.sleep(3.0)
        assert not exec_task.done(), (
            "ask_user was preempted by the 1s executor timeout; "
            "the tool should keep waiting for set_user_answer."
        )

        # Release the future, mirroring /questions/reply.
        released = rt.set_user_answer(sid, "yes", call_id="call_1")
        assert released, "set_user_answer should unblock ask_user"

        result = await asyncio.wait_for(exec_task, timeout=2.0)
        assert result.error is False, f"unexpected error result: {result.text!r}"
        assert "Error: timeout" not in result.text
        assert "User answered: yes" in result.text

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
