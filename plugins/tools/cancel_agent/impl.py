from __future__ import annotations

import asyncio
from typing import Any, Dict

from ziva.shared_types import RuntimeContext, ToolResult


class CancelAgentTool:
    """Cancel a running background sub-agent (like Claude Code's TaskStop)."""

    def spec(self) -> Dict[str, Any]:
        return {
            "name": "cancel_agent",
            "description": (
                "Cancel a running background sub-agent. "
                "The agent will be marked as cancelled and stop producing further output."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "agent_id": {
                        "type": "string",
                        "description": "The agent_id of the background agent to cancel",
                    },
                },
                "required": ["agent_id"],
            },
        }

    async def run(self, input_data: Dict[str, Any], ctx: RuntimeContext) -> ToolResult:
        runtime = ctx.metadata.get("_runtime")
        if not runtime:
            return ToolResult(text="Error: runtime_unavailable", error=True)

        agent_id = input_data.get("agent_id", "").strip()
        if not agent_id:
            return ToolResult(text="Error: missing_agent_id\nagent_id is required", error=True)

        agent = runtime._background_agents.get(agent_id)
        if not agent:
            return ToolResult(text=f"Error: agent_not_found\nNo agent with id '{agent_id}'", error=True)

        if agent["status"] != "running":
            return ToolResult(
                text=f"Agent '{agent_id}' is not running (current status: {agent['status']}). Cannot cancel.",
                metadata={"agent_id": agent_id, "status": agent["status"]},
            )

        # Flag first so the inner loop can break at the next checkpoint
        # even if task.cancel() can't tear down a stuck SDK call.
        agent["status"] = "cancelled"

        # Actually interrupt the asyncio task. CancelledError is a
        # BaseException subclass (Python 3.8+), so the `except Exception`
        # block in spawn_agent._run won't swallow it — the dedicated
        # `except asyncio.CancelledError` branch handles it.
        task = agent.get("task")
        if task and not task.done():
            task.cancel()
            try:
                await asyncio.wait_for(task, timeout=5.0)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass

        # Emit cancellation event. The inner task may already have
        # emitted one on CancelledError; clients deduplicate by agent_id.
        if runtime.event_bus:
            await runtime.event_bus.publish(agent["session_id"], {
                "type": "subagent_end",
                "call_id": agent.get("call_id"),
                "agent_id": agent_id,
                "task": agent.get("task_desc", ""),
                "status": "cancelled",
                "background": True,
            })

        return ToolResult(
            text=f"Agent '{agent_id}' has been cancelled.",
            metadata={"agent_id": agent_id, "status": "cancelled"},
        )
