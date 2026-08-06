from __future__ import annotations

import asyncio
import uuid
from typing import Any, Dict

from ziva.shared_types import ChatMessage, RuntimeContext, SessionState, ToolResult

BLOCKED_TOOLS = {"spawn_agent", "get_agent_result", "cancel_agent"}

# Config-defined agent tool lists may use aliases that differ from the
# registered tool names in this runtime. Map common aliases to real names.
_TOOL_ALIASES = {
    "search_files": "grep",
    "list_directory": "list",
    "glob_files": "glob",
    "read_file": "read_file",
    "write_file": "write_file",
    "edit_file": "edit_file",
    "shell": "shell",
}


def _summarize_tools(tool_names: list[str]) -> dict[str, int]:
    """Group tool calls by name, e.g. ['grep','grep','read_file'] -> {'grep':3,'read_file':1}.

    Shown to the user (in the UI) grouped by tool name rather than in call
    order, so a long run reads as "grep ×3, read_file ×5" instead of a flat
    list.
    """
    summary: dict[str, int] = {}
    for n in tool_names:
        summary[n] = summary.get(n, 0) + 1
    return summary


def _child_turn(runtime, parent_session_id: str):
    """Build a session-aware (model_cfg, model_adapter) pair for a child run.

    The child session is brand new (no model_name on disk), but its parent
    may have pinned a model via PATCH /sessions. Without this helper the
    sub-agent silently falls back to the runtime's global `config["model"]`,
    so a parent pinned to model B still spawns children running model A.

    Returns:
        (model_cfg, model_adapter): both ready to pass to
        ``runtime._run_model_tool_loop(... model_cfg=..., model_adapter=...)``.

    Resolution order matches chat() / chat_streaming():
        1. Parent session's pinned model_name (if any)
        2. Runtime global config["model"] (the active default)
    """
    from ziva.runtime import _create_adapter

    parent = runtime._get_session(parent_session_id)
    model_cfg = dict(runtime.config.get("model", {}))
    if parent.model_name:
        model_cfg["name"] = parent.model_name
    turn_config = dict(runtime.config)
    turn_config["model"] = model_cfg
    return model_cfg, _create_adapter(turn_config)


class SpawnAgentTool:
    """Spawn a sub-agent to handle a specific task independently."""

    def spec(self) -> Dict[str, Any]:
        return {
            "name": "spawn_agent",
            "description": (
                "Spawn a sub-agent to handle a specific task independently. The sub-agent runs in its own "
                "isolated session (its messages do not pollute this conversation) and returns only its "
                "final result. 'agent' is REQUIRED and must be one of the fixed types: "
                "explore (read-only investigation) / plan (produce an implementation plan) / "
                "general-purpose (read/write/run, full tool access except spawning further sub-agents). "
                "When background=true, the agent runs asynchronously and this returns immediately with "
                "an agent_id; the result is delivered later and persisted so it survives restart."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "agent": {
                        "type": "string",
                        "enum": ["explore", "plan", "general-purpose"],
                        "description": "REQUIRED, one of the fixed agent types: 'explore' (read-only investigation), 'plan' (produce an implementation plan), 'general-purpose' (read/write/run, full tool access except spawning further sub-agents).",
                    },
                    "task": {
                        "type": "string",
                        "description": "Clear, specific description of what the sub-agent should accomplish",
                    },
                    "background": {
                        "type": "boolean",
                        "description": "If true, run the sub-agent in the background and return immediately with an agent_id (result delivered via events; use get_agent_result to fetch). Defaults to the agent type's background setting.",
                    },
                },
                "required": ["agent", "task"],
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
        if not agent_name:
            return ToolResult(
                text=f"Error: missing_agent\n'agent' is required. Available: {', '.join(agents.keys())}",
                error=True,
            )
        if agent_name not in agents:
            return ToolResult(
                text=f"Error: unknown_agent\nUnknown agent '{agent_name}'. Available: {', '.join(agents.keys())}",
                error=True,
            )
        agent_def = agents[agent_name]

        # ── Three-state permission control (allow / deny / inherit) ──
        # For each dimension (tools / skills / hooks):
        #   - "tools" key present  → allow mode (whitelist)
        #   - "deny_tools" present → deny mode (all minus selected)
        #   - neither present      → inherit all (omit key entirely)
        instructions = (agent_def.get("instructions", "") or "").strip()
        background = bool(input_data.get("background", agent_def.get("background", False)))

        # ---- tools ----
        all_tool_names = {
            rec.instance.spec()["name"]
            for rec in runtime.registry.list_kind("tool")
        }
        tools_val = agent_def.get("tools")
        deny_tools_val = agent_def.get("deny_tools")
        if tools_val is not None:
            # Allow-list (may be empty [] = "allow zero tools").
            resolved = {_TOOL_ALIASES.get(t, t) for t in tools_val}
            _allowed_tools = resolved - BLOCKED_TOOLS
        elif deny_tools_val is not None:
            resolved_denied = {_TOOL_ALIASES.get(t, t) for t in deny_tools_val}
            _allowed_tools = all_tool_names - resolved_denied - BLOCKED_TOOLS
        else:
            _allowed_tools = None  # inherit all

        # ---- skills ----
        skills_val = agent_def.get("skills")
        deny_skills_val = agent_def.get("deny_skills")
        if skills_val is not None:
            # Allow-list (may be empty [] = "allow zero skills").
            _allowed_skills = set(skills_val)
        elif deny_skills_val is not None:
            _all_skill_ids = {rec.id for rec in runtime.registry.list_kind("skill")}
            _all_skill_ids |= {rec.id.replace("skill.", "") for rec in runtime.registry.list_kind("skill")}
            _allowed_skills = _all_skill_ids - set(deny_skills_val)
        else:
            _allowed_skills = None

        # ---- hooks (by registered hook id) ----
        # Sub-agents select which *specific* hooks may run, rather
        # than filtering by lifecycle event. ``hooks: [...]`` /
        # ``deny_hooks: [...]`` carry hook ids (e.g. ``hook.image_guard``);
        # the runtime matches each registered hook's ``id`` against these
        # sets. deny-mode blocks individual hooks (not whole event phases)
        # so the user can e.g. keep ``plan_reminder`` but block
        # ``image_guard`` on a specific sub-agent.
        #
        # ``None`` / key-absent = inherit (run all hooks).
        # ``[]``  = allow zero hooks (run none).
        _allowed_hooks: set[str] | None
        _denied_hooks: set[str] | None
        hooks_val = agent_def.get("hooks")
        deny_hooks_val = agent_def.get("deny_hooks")
        if hooks_val is not None:
            _allowed_hooks = set(hooks_val)
            _denied_hooks = None
        elif deny_hooks_val is not None:
            _allowed_hooks = None
            _denied_hooks = set(deny_hooks_val)
        else:
            _allowed_hooks = None
            _denied_hooks = None

        # Build child agent messages. The sub-agent has its own system
        # prompt construction taken directly from the agent configuration
        # in settings. Only the agent's own instructions are used; the
        # main agent's layered AGENTS.md instructions are NOT loaded.
        child_messages: list[ChatMessage] = [ChatMessage(role="user", content=task)]
        if instructions:
            child_messages.insert(
                0, ChatMessage(role="system", content=instructions)
            )

        call_id = uuid.uuid4().hex[:12]

        # Create an isolated child session: the sub-agent's messages live in
        # their own JSONL (not the parent's), so the parent conversation
        # stays clean and the sub-agent conversation persists separately.
        # The parent keeps only this spawn_agent tool_call + a result summary.
        child_sid = uuid.uuid4().hex
        pid = runtime._resolve_project_id(ctx.session_id)
        from ziva.storage.file_storage import FileStorage
        import time as _time
        _now = int(_time.time() * 1000)
        FileStorage.create_session(pid, {
            "id": child_sid,
            "time": {"created": _now, "updated": _now},
            "is_subagent": True,
            "parent_session_id": ctx.session_id,
            "subagent_call_id": call_id,
            "agent_type": agent_name,
        })
        # Register a child SessionState under the parent's project_id so
        # _resolve_project_id(child_sid) routes disk calls to the right pid.
        runtime._sessions[child_sid] = SessionState(project_id=pid)

        child_meta: dict[str, Any] = {
            "_runtime": runtime,
            "_subagent": True,
            "_subagent_call_id": call_id,
            "_spawn_tool_call_id": ctx.metadata.get("_tool_call_id"),
            # Inherit the parent turn's workspace snapshot so the sub-agent's
            # file tools run in the parent session's workspace. child_meta is a
            # fresh dict (does NOT spread ctx.metadata), so this must be
            # explicit — otherwise the child silently falls back to os.getcwd().
            "_workspace_root": ctx.metadata.get("_workspace_root"),
        }
        if _allowed_tools is not None:
            child_meta["_allowed_tools"] = _allowed_tools
        if _allowed_skills is not None:
            child_meta["_allowed_skills"] = _allowed_skills
        if _allowed_hooks is not None:
            child_meta["_allowed_hooks"] = _allowed_hooks
        if _denied_hooks is not None:
            child_meta["_denied_hooks"] = _denied_hooks

        child_ctx = RuntimeContext(
            session_id=child_sid,
            config=ctx.config,
            metadata=child_meta,
        )

        if background:
            return await self._run_background(runtime, task, call_id, child_messages, child_ctx, ctx.session_id, child_sid)

        # ── Foreground (blocking) mode ──
        return await self._run_foreground(runtime, task, call_id, child_messages, child_ctx, ctx.session_id, child_sid)

    async def _run_foreground(self, runtime, task, call_id, child_messages, child_ctx, session_id, child_sid) -> ToolResult:
        """Synchronous sub-agent: blocks until completion.

        session_id is the PARENT session (event publish target, so the UI
        sees the sub-agent card in the parent stream). child_ctx.session_id
        is the child session where the sub-agent's messages are persisted.
        """
        if runtime.event_bus:
            await runtime.event_bus.publish(session_id, {
                "type": "subagent_start",
                "call_id": call_id,
                "task": task[:200],
                "background": False,
                "subagent_session_id": child_sid,
            })

        # Sub-agent must inherit the PARENT session's pinned model — not
        # the runtime's global default — otherwise switching model A→B
        # on the parent and then spawning a sub-agent would silently run
        # the sub-agent on A.
        model_cfg, model_adapter = _child_turn(runtime, session_id)

        result_content = ""
        tool_count = 0
        tool_names: list[str] = []
        try:
            async for event in runtime._run_model_tool_loop(
                child_messages, child_ctx.session_id, child_ctx,
                model_cfg=model_cfg, model_adapter=model_adapter,
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
                "tools_summary": _summarize_tools(tool_names),
                "result_length": len(result_content),
                "background": False,
                "subagent_session_id": child_sid,
            })

        # Strip <think> tags: the parent session only needs the child's
        # final answer, not its chain-of-thought. Some providers embed
        # thinking in content instead of the reasoning_content field.
        from ziva.adapters._think_parser import strip_think_tags
        clean_result = strip_think_tags(result_content)

        return ToolResult(
            text=clean_result,
            metadata={"tools_used": tool_count, "tools_summary": _summarize_tools(tool_names), "result": clean_result, "subagent_session_id": child_sid},
        )

    async def _run_background(self, runtime, task, call_id, child_messages, child_ctx, session_id, child_sid) -> ToolResult:
        """Background sub-agent: returns immediately, runs via asyncio.create_task."""
        import time

        agent_id = f"bg_{call_id}"

        runtime._background_agents[agent_id] = {
            "agent_id": agent_id,
            "call_id": call_id,
            "session_id": session_id,
            "child_session_id": child_sid,
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
                "subagent_session_id": child_sid,
            })

        async def _run():
            # Hold the concurrency permit for the entire run so the
            # configured cap (spawn.max_concurrency, default 20) bounds
            # simultaneously-active background agents. Spawning is not
            # blocked — only the work inside _run is gated.
            async with runtime._agent_concurrency:
                # Same parent-model-inheritance as _run_foreground —
                # background sub-agents must use the parent's pinned
                # model too. See the long-form note in _child_turn.
                model_cfg, model_adapter = _child_turn(runtime, session_id)
                result_content = ""
                tool_count = 0
                tool_names: list[str] = []
                try:
                    async for event in runtime._run_model_tool_loop(
                        child_messages, child_ctx.session_id, child_ctx,
                        model_cfg=model_cfg, model_adapter=model_adapter,
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

                # NOTE: We intentionally do NOT rewrite the parent
                # session's spawn_agent tool message here. Background
                # agents return their result via get_agent_result, not
                # via the spawn_agent tool card. Rewriting the card
                # would pollute the parent's chat with the child's full
                # output (including <think> tags) and make the tool
                # card's content change asynchronously after it was
                # already shown to the user.

                if runtime.event_bus:
                    await runtime.event_bus.publish(session_id, {
                        "type": "subagent_end",
                        "call_id": call_id,
                        "agent_id": agent_id,
                        "task": task[:200],
                        "status": "completed",
                        "tools_used": tool_count,
                        "tools_summary": _summarize_tools(tool_names),
                        "result_length": len(result_content),
                        "result_preview": result_content[:500],
                        "background": True,
                        "subagent_session_id": child_sid,
                    })

        # Store the task reference on the tracker so cancel_agent can
        # interrupt it and the GC doesn't collect a never-awaited task.
        bg_task = asyncio.create_task(_run())
        runtime._background_agents[agent_id]["task"] = bg_task
        bg_task.add_done_callback(lambda _: runtime._prune_background_agents())

        return ToolResult(
            text=f"Background agent started (id: {agent_id}). It will run independently and results will be delivered via events.",
            metadata={"agent_id": agent_id, "status": "running", "background": True, "subagent_session_id": child_sid},
        )
