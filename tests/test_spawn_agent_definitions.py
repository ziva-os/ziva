from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock

from plugins.tools.spawn_agent.impl import SpawnAgentTool
from ziva_runtime.shared_types import RuntimeContext, ToolResult


def _make_runtime(agents=None):
    runtime = MagicMock()
    runtime.config = {"agents": agents or {}}
    runtime.event_bus = AsyncMock()
    return runtime


def _make_ctx(runtime):
    return RuntimeContext(
        session_id="sid_1",
        config={},
        metadata={"_runtime": runtime},
    )


@pytest.mark.asyncio
async def test_spawn_unknown_agent_returns_error():
    tool = SpawnAgentTool()
    runtime = _make_runtime({"explore": {"instructions": "explore"}})
    ctx = _make_ctx(runtime)
    result = await tool.run({"agent": "missing", "task": "find files"}, ctx)
    assert result.error is True
    assert "unknown_agent" in result.text
    assert "explore" in result.text


@pytest.mark.asyncio
async def test_spawn_agent_uses_definition_defaults():
    tool = SpawnAgentTool()
    runtime = _make_runtime({
        "explore": {
            "instructions": "You are explore.",
            "tools": ["read_file"],
            "background": True,
        }
    })
    ctx = _make_ctx(runtime)

    # Patch the background runner so we can inspect the constructed context.
    captured = {}
    original_bg = tool._run_background

    async def fake_bg(runtime_, task, call_id, child_messages, child_ctx, session_id):
        captured["child_messages"] = child_messages
        captured["child_ctx"] = child_ctx
        return ToolResult(text="started")

    tool._run_background = fake_bg

    result = await tool.run({"agent": "explore", "task": "find files"}, ctx)

    assert result.text == "started"
    assert captured["child_messages"][0].role == "system"
    assert "You are explore." in captured["child_messages"][0].content
    assert captured["child_messages"][1].role == "user"
    assert captured["child_messages"][1].content == "find files"
    assert captured["child_ctx"].metadata["_allowed_tools"] == {"read_file"}


@pytest.mark.asyncio
async def test_spawn_agent_call_time_overrides_definition():
    tool = SpawnAgentTool()
    runtime = _make_runtime({
        "plan": {
            "instructions": "You are plan.",
            "tools": ["read_file"],
            "background": False,
        }
    })
    ctx = _make_ctx(runtime)

    captured = {}
    original_fg = tool._run_foreground

    async def fake_fg(runtime_, task, call_id, child_messages, child_ctx, session_id):
        captured["child_messages"] = child_messages
        captured["child_ctx"] = child_ctx
        captured["background"] = False
        return ToolResult(text="done")

    tool._run_foreground = fake_fg

    result = await tool.run({
        "agent": "plan",
        "task": "plan feature",
        "instructions": "Override instructions.",
        "tools": ["read_file", "edit_file"],
        "background": False,
    }, ctx)

    assert captured["child_messages"][0].role == "system"
    assert "Override instructions." in captured["child_messages"][0].content
    assert captured["child_messages"][1].role == "user"
    assert captured["child_messages"][1].content == "plan feature"
    assert captured["child_ctx"].metadata["_allowed_tools"] == {"read_file", "edit_file"}


@pytest.mark.asyncio
async def test_spawn_agent_without_agent_uses_call_time_params():
    tool = SpawnAgentTool()
    runtime = _make_runtime({})
    ctx = _make_ctx(runtime)

    captured = {}

    async def fake_fg(runtime_, task, call_id, child_messages, child_ctx, session_id):
        captured["child_messages"] = child_messages
        captured["child_ctx"] = child_ctx
        return ToolResult(text="done")

    tool._run_foreground = fake_fg

    result = await tool.run({
        "task": "do work",
        "instructions": "Custom instr.",
        "tools": ["search"],
        "background": False,
    }, ctx)

    assert captured["child_messages"][0].role == "system"
    assert "Custom instr." in captured["child_messages"][0].content
    assert captured["child_messages"][1].role == "user"
    assert captured["child_messages"][1].content == "do work"
    assert captured["child_ctx"].metadata["_allowed_tools"] == {"search"}
