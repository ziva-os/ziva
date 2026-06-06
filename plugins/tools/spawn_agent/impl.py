from __future__ import annotations

import uuid
from typing import Any, Dict

from ziva_runtime.shared_types import ChatMessage, RuntimeContext, ToolResult

# Tools that sub-agents are NOT allowed to use
BLOCKED_TOOLS = {"spawn_agent"}


class SpawnAgentTool:
    """Spawn a sub-agent to handle a specific task independently."""

    def spec(self) -> Dict[str, Any]:
        return {
            "name": "spawn_agent",
            "description": (
                "Spawn a sub-agent to handle a specific task. The sub-agent runs in its own "
                "context with access to most tools (cannot spawn further sub-agents). "
                "Use this for delegating focused work (e.g., searching, coding, analyzing) "
                "to keep the main context clean. The sub-agent's result is returned to you."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "task": {
                        "type": "string",
                        "description": "Clear, specific description of what the sub-agent should accomplish",
                    },
                    "instructions": {
                        "type": "string",
                        "description": "Optional extra instructions for the sub-agent (constraints, focus areas, style)",
                    },
                    "tools": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional whitelist of tool names the sub-agent can use. If omitted, all tools except spawn_agent are available.",
                    },
                },
                "required": ["task"],
            },
        }

    async def run(self, input_data: Dict[str, Any], ctx: RuntimeContext) -> ToolResult:
        # Prevent recursive spawning
        if ctx.metadata.get("_subagent"):
            return ToolResult(text="Error: recursive_forbidden\nSub-agents cannot spawn further sub-agents", error=True)

        runtime = ctx.metadata.get("_runtime")
        if not runtime:
            return ToolResult(text="Error: spawn_agent_unavailable\nRuntime not accessible", error=True)

        task = input_data.get("task", "").strip()
        if not task:
            return ToolResult(text="Error: missing_task\ntask is required", error=True)

        instructions = input_data.get("instructions", "").strip()
        tool_whitelist = input_data.get("tools")  # optional list of allowed tool names

        # Build child agent messages
        child_messages: list[ChatMessage] = []
        if instructions:
            child_messages.append(ChatMessage(role="system", content=instructions))
        child_messages.append(ChatMessage(role="user", content=task))

        call_id = uuid.uuid4().hex[:12]

        # Create child context with subagent flag and tool restrictions
        child_meta: dict[str, Any] = {
            "_runtime": runtime,
            "_subagent": True,
            "_subagent_call_id": call_id,
        }
        if tool_whitelist is not None:
            child_meta["_allowed_tools"] = set(tool_whitelist) - BLOCKED_TOOLS

        child_ctx = RuntimeContext(
            session_id=ctx.session_id,
            config=ctx.config,
            metadata=child_meta,
        )

        # Emit subagent start event
        if runtime.event_bus:
            await runtime.event_bus.publish(ctx.session_id, {
                "type": "subagent_start",
                "task": task[:200],
            })

        # Run sub-agent loop
        result_content = ""
        tool_count = 0
        tool_names: list[str] = []
        try:
            async for event in runtime._run_model_tool_loop(
                child_messages, ctx.session_id, child_ctx,
            ):
                t = event.get("type")
                if t == "model_response":
                    result_content = event.get("content", "")
                elif t == "tool_start":
                    tool_count += 1
                    tn = event.get("tool")
                    if tn:
                        tool_names.append(str(tn))

                # Re-emit events with subagent flag for frontend
                if runtime.event_bus:
                    event_copy = dict(event)
                    event_copy["_subagent"] = True
                    await runtime.event_bus.publish(ctx.session_id, event_copy)

        except Exception as exc:
            partial = result_content[:500] if result_content else ""
            return ToolResult(
                text=f"Error: subagent_failed\n{exc}\nPartial: {partial}",
                error=True,
                metadata={"partial_result": result_content},
            )

        # Emit subagent end event
        if runtime.event_bus:
            await runtime.event_bus.publish(ctx.session_id, {
                "type": "subagent_end",
                "task": task[:200],
                "tools_used": tool_count,
                "result_length": len(result_content),
            })

        return ToolResult(
            text=f"Agent completed ({tool_count} tools used)\n\n{result_content}",
            metadata={"tools_used": tool_count, "tools": tool_names, "result": result_content},
        )
