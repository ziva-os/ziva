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
                "with an agent_id; the result will be available via the subagent_completed event. "
                "You can reference a predefined agent from the config with the 'agent' parameter; "
                "predefined agents provide default instructions, tool whitelist, and background mode."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "agent": {
                        "type": "string",
                        "description": "Name of a predefined agent from config (e.g. 'explore', 'plan'). If provided, its instructions, tools, and background defaults are used.",
                    },
                    "task": {
                        "type": "string",
                        "description": "Clear, specific description of what the sub-agent should accomplish",
                    },
                    "instructions": {
                        "type": "string",
                        "description": "Optional extra instructions for the sub-agent (constraints, focus areas, style). Overrides the predefined agent's instructions when agent is set.",
                    },
                    "tools": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional whitelist of tool names the sub-agent can use. Overrides the predefined agent's tools when agent is set. If omitted and no agent is set, all tools except spawn_agent are available.",
                    },
                    "background": {
                        "type": "boolean",
                        "description": "If true, run the sub-agent in the background and return immediately with an agent_id. Overrides the predefined agent's background default when agent is set.",
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

        # Resolve predefined agent definition from config, allowing call-time overrides.
        config = getattr(runtime, "config", {}) or {}
        agents = config.get("agents", {})
        agent_name = input_data.get("agent", "").strip()
        agent_def = agents.get(agent_name) if agent_name else None
        if agent_name and agent_def is None:
            available = ", ".join(agents.keys()) if agents else "none"
            return ToolResult(
                text=f"Error: unknown_agent\nUnknown agent '{agent_name}'. Available: {available}",
                error=True,
            )

        instructions = input_data.get("instructions", "").strip()
        if not instructions and agent_def:
            instructions = agent_def.get("instructions", "").strip()

        tool_whitelist = input_data.get("tools")
        if tool_whitelist is None and agent_def:
            tool_whitelist = agent_def.get("tools")

        background = input_data.get("background")
        if background is None and agent_def:
            background = bool(agent_def.get("background", False))
        background = bool(background)

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
        import time

        agent_id = f"bg_{call_id}"

        runtime._background_agents[agent_id] = {
            "agent_id": agent_id,
            "call_id": call_id,
            "session_id": session_id,
            "task_desc": task[:200],
            "status": "running",
            "result": None,
            "error": None,
            "tools_used": 0,
            "task": None,
            "finished_at": 0,
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
            # Hold the concurrency permit for the entire run so the
            # configured cap (spawn.max_concurrency, default 20) bounds
            # simultaneously-active background agents. Spawning is not
            # blocked — only the work inside _run is gated.
            async with runtime._agent_concurrency:
                result_content = ""
                tool_count = 0
                tool_names: list[str] = []
                try:
                    async for event in runtime._run_model_tool_loop(
                        child_messages, session_id, child_ctx,
                    ):
                        if runtime._background_agents.get(agent_id, {}).get("status") == "cancelled":
                            break
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

                except asyncio.CancelledError:
                    runtime._background_agents[agent_id]["status"] = "cancelled"
                    runtime._background_agents[agent_id]["finished_at"] = time.time()
                    if runtime.event_bus:
                        await runtime.event_bus.publish(session_id, {
                            "type": "subagent_end",
                            "call_id": call_id,
                            "agent_id": agent_id,
                            "task": task[:200],
                            "status": "cancelled",
                            "result_length": len(result_content),
                            "background": True,
                        })
                    raise
                except Exception as exc:
                    runtime._background_agents[agent_id]["status"] = "failed"
                    runtime._background_agents[agent_id]["error"] = str(exc)
                    runtime._background_agents[agent_id]["result"] = result_content[:2000]
                    runtime._background_agents[agent_id]["finished_at"] = time.time()
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

                if runtime._background_agents.get(agent_id, {}).get("status") == "cancelled":
                    return

                runtime._background_agents[agent_id]["status"] = "completed"
                runtime._background_agents[agent_id]["result"] = result_content
                runtime._background_agents[agent_id]["tools_used"] = tool_count
                runtime._background_agents[agent_id]["finished_at"] = time.time()

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

        # Store the task reference on the tracker so cancel_agent can
        # interrupt it and the GC doesn't collect a never-awaited task.
        bg_task = asyncio.create_task(_run())
        runtime._background_agents[agent_id]["task"] = bg_task
        bg_task.add_done_callback(lambda _: runtime._prune_background_agents())

        return ToolResult(
            text=f"Background agent started (id: {agent_id}). It will run independently and results will be delivered via events.",
            metadata={"agent_id": agent_id, "status": "running", "background": True},
        )
