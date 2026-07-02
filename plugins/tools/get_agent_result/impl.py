from __future__ import annotations

import asyncio
from typing import Any, Dict

from ziva.shared_types import RuntimeContext, ToolResult


class GetAgentResultTool:
    """Retrieve the result of a background sub-agent (like Claude Code's TaskOutput)."""

    def spec(self) -> Dict[str, Any]:
        return {
            "name": "get_agent_result",
            "description": (
                "Retrieve the result of a background sub-agent. "
                "Use block=true to wait for completion if the agent is still running. "
                "Returns status, result text, and tools used."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "agent_id": {
                        "type": "string",
                        "description": "The agent_id returned by spawn_agent when background=true",
                    },
                    "block": {
                        "type": "boolean",
                        "description": "If true, wait for the agent to finish before returning (default: false)",
                    },
                    "timeout": {
                        "type": "integer",
                        "description": "Max milliseconds to wait when block=true (default: 30000, max: 600000)",
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

        block = input_data.get("block", False)
        timeout_ms = min(input_data.get("timeout", 30000), 600000)

        if block and agent["status"] == "running":
            # Wait for the agent to finish
            try:
                async def _waiter():
                    while agent["status"] == "running":
                        await asyncio.sleep(0.5)

                await asyncio.wait_for(_waiter(), timeout=timeout_ms / 1000.0)
            except asyncio.TimeoutError:
                return ToolResult(
                    text=f"Agent '{agent_id}' is still running after {timeout_ms}ms timeout.\n"
                         f"Status: running\nTask: {agent.get('task_desc', '')}",
                    metadata={"agent_id": agent_id, "status": "running", "timed_out": True},
                )

        return self._format_result(agent_id, agent)

    def _format_result(self, agent_id: str, agent: dict) -> ToolResult:
        status = agent["status"]
        result_text = agent.get("result") or ""
        error = agent.get("error")
        tools_used = agent.get("tools_used", 0)

        if status == "running":
            text = f"Agent '{agent_id}' is still running.\nTask: {agent.get('task_desc', '')}"
        elif status == "failed":
            text = f"Agent '{agent_id}' failed: {error}\nPartial result: {result_text[:2000]}"
        elif status == "cancelled":
            text = f"Agent '{agent_id}' was cancelled.\nPartial result: {result_text[:2000]}"
        else:
            text = f"Agent '{agent_id}' completed ({tools_used} tools used).\n\n{result_text}"

        return ToolResult(
            text=text,
            metadata={
                "agent_id": agent_id,
                "status": status,
                "tools_used": tools_used,
                "error": error,
            },
        )
