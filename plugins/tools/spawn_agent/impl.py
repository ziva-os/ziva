from __future__ import annotations

import asyncio
import uuid
from typing import Any, Dict

from ziva_runtime.shared_types import ChatMessage, RuntimeContext, ToolResult

BLOCKED_TOOLS = {"spawn_agent", "get_agent_result", "cancel_agent"}


class SpawnAgentTool:
    """Spawn a sub-agent to handle a specific task independently."""

    def spec(self) -> Dict[str, Any]:
        return {
            "name": "spawn_agent",
            "description": (
                "Spawn a sub-agent to handle a specific task. The sub-agent runs in its own "
                "context with access to most tools (cannot spawn further sub-agents). "
                "Use this for delegating focused work (e.g., searching, coding, analyzing) "
                "to keep the main context clean. "
                "When background=true, the agent runs asynchronously and this returns immediately "
                "with an agent_id; the result will be available via the subagent_completed event."
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
                    "background": {
                        "type": "boolean",
                        "description": "If true, run the sub-agent in the background and return immediately with an agent_id. Results are delivered via events.",
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
        tool_whitelist = input_data.get("tools")
        background = input_data.get("background", False)

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

        if background:
            return await self._run_background(runtime, task, call_id, child_messages, child_ctx, ctx.session_id)

        # ── Foreground (blocking) mode ──
        return await self._run_foreground(runtime, task, call_id, child_messages, child_ctx, ctx.session_id)

    async def _run_foreground(self, runtime, task, call_id, child_messages, child_ctx, session_id) -> ToolResult:
        """Synchronous sub-agent: blocks until completion."""
        if runtime.event_bus:
            await runtime.event_bus.publish(session_id, {
                "type": "subagent_start",
                "call_id": call_id,
                "task": task[:200],
                "background": False,
            })

        result_content = ""
        tool_count = 0
        tool_names: list[str] = []
        try:
            async for event in runtime._run_model_tool_loop(
                child_messages, session_id, child_ctx,
            ):
                t = event.get("type")
                if t == "model_response":
                    result_content = event.get("content", "")
                elif t == "tool_start":
                    tool_count += 1
                    tn = event.get("tool")
                    if tn:
                        tool_names.append(str(tn))

                if runtime.event_bus:
                    event_copy = dict(event)
                    event_copy["_subagent"] = True
                    event_copy["_subagent_call_id"] = call_id
                    await runtime.event_bus.publish(session_id, event_copy)

        except Exception as exc:
            partial = result_content[:500] if result_content else ""
            return ToolResult(
                text=f"Error: subagent_failed\n{exc}\nPartial: {partial}",
                error=True,
                metadata={"partial_result": result_content},
            )

        if runtime.event_bus:
            await runtime.event_bus.publish(session_id, {
                "type": "subagent_end",
                "call_id": call_id,
                "task": task[:200],
                "tools_used": tool_count,
                "result_length": len(result_content),
                "background": False,
            })

        return ToolResult(
            text=f"Agent completed ({tool_count} tools used)\n\n{result_content}",
            metadata={"tools_used": tool_count, "tools": tool_names, "result": result_content},
        )

    async def _run_background(self, runtime, task, call_id, child_messages, child_ctx, session_id) -> ToolResult:
        """Background sub-agent: returns immediately, runs via asyncio.create_task."""
        agent_id = f"bg_{call_id}"

        # Register in runtime tracker
        runtime._background_agents[agent_id] = {
            "agent_id": agent_id,
            "call_id": call_id,
            "session_id": session_id,
            "task_desc": task[:200],
            "status": "running",
            "result": None,
            "error": None,
            "tools_used": 0,
        }

        if runtime.event_bus:
            await runtime.event_bus.publish(session_id, {
                "type": "subagent_start",
                "call_id": call_id,
                "agent_id": agent_id,
                "task": task[:200],
                "background": True,
            })

        async def _run():
            result_content = ""
            tool_count = 0
            tool_names: list[str] = []
            try:
                async for event in runtime._run_model_tool_loop(
                    child_messages, session_id, child_ctx,
                ):
                    t = event.get("type")
                    if t == "model_response":
                        result_content = event.get("content", "")
                    elif t == "tool_start":
                        tool_count += 1
                        tn = event.get("tool")
                        if tn:
                            tool_names.append(str(tn))

                    if runtime.event_bus:
                        event_copy = dict(event)
                        event_copy["_subagent"] = True
                        event_copy["_subagent_call_id"] = call_id
                        event_copy["_agent_id"] = agent_id
                        await runtime.event_bus.publish(session_id, event_copy)

            except Exception as exc:
                runtime._background_agents[agent_id]["status"] = "failed"
                runtime._background_agents[agent_id]["error"] = str(exc)
                runtime._background_agents[agent_id]["result"] = result_content[:2000]
                if runtime.event_bus:
                    await runtime.event_bus.publish(session_id, {
                        "type": "subagent_end",
                        "call_id": call_id,
                        "agent_id": agent_id,
                        "task": task[:200],
                        "status": "failed",
                        "error": str(exc),
                        "result_length": len(result_content),
                        "background": True,
                    })
                return

            runtime._background_agents[agent_id]["status"] = "completed"
            runtime._background_agents[agent_id]["result"] = result_content
            runtime._background_agents[agent_id]["tools_used"] = tool_count

            if runtime.event_bus:
                await runtime.event_bus.publish(session_id, {
                    "type": "subagent_end",
                    "call_id": call_id,
                    "agent_id": agent_id,
                    "task": task[:200],
                    "status": "completed",
                    "tools_used": tool_count,
                    "tools": tool_names,
                    "result_length": len(result_content),
                    "result_preview": result_content[:500],
                    "background": True,
                })

        asyncio.create_task(_run())

        return ToolResult(
            text=f"Background agent started (id: {agent_id}). It will run independently and results will be delivered via events.",
            metadata={"agent_id": agent_id, "status": "running", "background": True},
        )
