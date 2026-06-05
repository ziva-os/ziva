from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, AsyncIterator, Dict, Iterable, List
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ziva_runtime.adapters.openai_agents.provider import ModelAdapter, OpenAIAgentsAdapter
from ziva_runtime.capabilities.events import EventBus
from ziva_runtime.capabilities.registries import CapabilityRegistry
from ziva_runtime.config.instructions import load_layered_instructions
from ziva_runtime.config.loader import load_effective_config
from ziva_runtime.permissions import (
    DeniedError,
    PermissionManager,
    from_config,
    get_permission_manager,
    RejectedError,
)
from ziva_runtime.plugins.loader import load_plugins
from ziva_runtime.session.compaction import compact_messages, is_overflow, _llm_context
from ziva_runtime.shared_types import ApprovalRequest, ApprovalPolicy, CancellationToken, ChatMessage, ChatResult, RuntimeContext, ToolCall, ToolCallItem
from ziva_runtime.storage.file_storage import FileStorage, _project_hash


def _detect_timezone() -> str:
    tz = os.environ.get("TZ")
    if tz:
        return tz.lstrip(":")

    try:
        resolved = Path("/etc/localtime").resolve()
        parts = resolved.parts
        if "zoneinfo" in parts:
            idx = parts.index("zoneinfo")
            zone = "/".join(parts[idx + 1:])
            if zone:
                return zone
    except OSError:
        pass

    local_now = datetime.now().astimezone()
    return local_now.tzname() or "local"


def _current_date_for_timezone(timezone: str) -> str:
    try:
        return datetime.now(ZoneInfo(timezone)).date().isoformat()
    except ZoneInfoNotFoundError:
        return datetime.now().astimezone().date().isoformat()


# Skill categorization — used by the sidebar Skills page to group
# skills in the browsing UI. The mapping is purely keyword-based and
# intentionally coarse: a few top-level buckets are easier to scan
# than a long flat list, and the search box handles fine-grained
# filtering. The first matching rule wins; "Other" is the fallback.
_SKILL_CATEGORY_RULES: List[tuple] = [
    ("规划/工作流", ("plan", "task_plan", "findings", "progress", "brainstorm", "workflow", "manus", "session-catchup", "会话")),
    ("浏览器/网页", ("browser", "web page", "navigate", "snapshot", "devtools", "dom", "click", "fill form", "browser automation")),
    ("视频/动画", ("video", "视频", "动画", "漫画", "短剧", "drama", "seedance", "即梦", "stitch", "manga")),
    ("金融/投资", ("stock", "crypto", "finance", "financial", "portfolio", "yahoo", "trading")),
    ("数据/搜索", ("search", "search the web", "data source", "datasource", "数据", "网络搜索")),
    ("MCP/集成", ("mcp", "model context protocol", "plugin", "clawdhub", "tool integration")),
    ("开发/工程", ("code", "coding", "build", "website", "frontend", "backend", "debug", "test", "engineer", "stitch-loop")),
    ("设计/UI", ("design", "ui", "ux", "interface", "visual", "styling", "theme")),
]


def _categorize_skill(name: str, description: str) -> str:
    """Best-effort categorization for a skill based on its name + description.

    The runtime scans SKILL.md frontmatter to build a compact index, and
    callers (the desktop UI's Skills page) need a single `category`
    string per entry so they can group skills into collapsible sections
    and offer category filters. The match is purely substring-based —
    it's intentionally lenient, because the cost of a wrong bucket
    (a skill landing in "Other") is much lower than the cost of an
    unindexed skill that the user has to hunt for.
    """
    haystack = f"{name} {description}".lower()
    for category, keywords in _SKILL_CATEGORY_RULES:
        for kw in keywords:
            if kw in haystack:
                return category
    return "其他"


@dataclass
class Runtime:
    config: Dict[str, Any]
    registry: CapabilityRegistry
    model_adapter: ModelAdapter
    event_bus: EventBus
    event_seq: Dict[str, int]
    workspace_root: Path
    _mcp_client: Any | None = None
    _mcp_connected: bool = False
    _session_history: Dict[str, List[ChatMessage]] = field(default_factory=dict)
    _project_id: str | None = None
    # Per-session futures for the ask_user tool, keyed by call_id so
    # that multiple ask_user calls can be pending in parallel. The tool
    # run() awaits the future set by the server's POST
    # /sessions/{sid}/questions/reply handler. The future resolves to
    # the user's answer (or None on cancel).
    _pending_questions: Dict[str, Dict[str, asyncio.Future]] = field(default_factory=dict, init=False, repr=False)

    @property
    def project_id(self) -> str:
        if self._project_id is None:
            self._project_id = _project_hash(self.workspace_root)
            # Initialize project metadata
            FileStorage.save_project(self._project_id, {
                "id": self._project_id,
                "path": str(self.workspace_root),
                "time": {
                    "created": int(time.time() * 1000),
                    "updated": int(time.time() * 1000),
                },
            })
        return self._project_id

    @classmethod
    def create(
        cls,
        *,
        workspace_root: Path,
        global_config_path: Path | None = None,
        workspace_config_path: Path | None = None,
        session_override: Dict[str, Any] | None = None,
        model_adapter: ModelAdapter | None = None,
    ) -> "Runtime":
        config = load_effective_config(global_config_path, workspace_config_path, session_override)
        registry = CapabilityRegistry()

        # Workspace plugins
        plugin_paths = [workspace_root / Path(p) for p in config.get("plugin", {}).get("paths", ["./plugins"])]
        load_plugins(plugin_paths, registry, config)

        # Skills live under their own `skill:` block. Scan
        # `skill.extra_paths` (which defaults to the well-known global
        # directories `~/.ziva/skills` and `~/.agents/skills` so
        # canonical skills like agent-browser work out of the box).
        # Users can add or remove roots via `~/.ziva/config.yaml`.
        extra_skill_paths = config.get("skill", {}).get("extra_paths", [])
        skill_index = []  # Only name + description + path for progressive loading
        for sp in extra_skill_paths:
            p = Path(sp).expanduser().resolve()
            if p.exists():
                # Try loading as plugin dir first
                load_plugins([p], registry, config)
                # Build compact skill index from SKILL.md frontmatter
                for skill_file in p.rglob("SKILL.md"):
                    raw = skill_file.read_text(encoding="utf-8").strip()
                    if not raw:
                        continue
                    name = skill_file.parent.name
                    desc = ""
                    # Extract description from YAML frontmatter
                    if raw.startswith("---"):
                        end = raw.find("---", 3)
                        if end > 0:
                            fm = raw[3:end]
                            for line in fm.splitlines():
                                if line.startswith("description:"):
                                    desc = line.split(":", 1)[1].strip().strip('"').strip("'")
                                    break
                            if not desc:
                                for line in fm.splitlines():
                                    if line.startswith("name:"):
                                        name = line.split(":", 1)[1].strip().strip('"').strip("'")
                    skill_index.append({
                        "name": name,
                        "description": desc[:200] if desc else "",
                        "path": str(skill_file),
                        "category": _categorize_skill(name, desc),
                    })

        config["_skill_index"] = skill_index

        adapter = model_adapter or OpenAIAgentsAdapter(
            base_url=config.get("model", {}).get("base_url") or None,
            api_key=config.get("model", {}).get("api_key") or None,
        )
        runtime = cls(
            config=config,
            registry=registry,
            model_adapter=adapter,
            event_bus=EventBus(),
            event_seq={},
            workspace_root=workspace_root,
            _mcp_client=None,
            _mcp_connected=False,
        )

        # Initialize PermissionManager with config
        perm_config = config.get("permissions", {})
        if perm_config:
            perm_manager = get_permission_manager()
            perm_manager.set_approved_rules(from_config(perm_config))

        return runtime

    async def chat(self, messages: Iterable[ChatMessage], session_id: str | None = None) -> ChatResult:
        sid = session_id or str(uuid.uuid4())
        new_messages = list(messages)
        ctx = RuntimeContext(session_id=sid, config=self.config)
        ctx.metadata["_runtime"] = self

        # Load session history from disk if not already loaded
        history = self._session_history.setdefault(sid, [])
        if not history:
            loaded = self._load_session_from_disk(sid)
            if loaded:
                history.extend(loaded)
            else:
                # Create new session
                FileStorage.create_session(self.project_id, {
                    "id": sid,
                    "time": {"created": int(time.time() * 1000), "updated": int(time.time() * 1000)},
                })

        # Append new user messages to session history
        history.extend(new_messages)
        for msg in new_messages:
            self._persist_message(sid, msg)

        await self._run_hooks("before_turn", {"messages": [m.__dict__ for m in new_messages]}, ctx)
        await self._emit(sid, {"type": "turn_start"})

        rendered_messages = self._apply_prompt(list(history), ctx)
        _last = rendered_messages[-1].content if rendered_messages else ""
        if isinstance(_last, list):
            _last = " ".join(p.get("text", "") for p in _last if isinstance(p, dict) and p.get("type") == "text")
        skill_output = await self._maybe_apply_skill(_last, ctx)
        if skill_output:
            rendered_messages.append(ChatMessage(role="system", content=f"Skill output: {skill_output}"))

        # Run unified streaming loop; events are emitted to event bus automatically
        final_content = ""
        final_usage = None
        final_finish_reason = "stop"
        cancelled = False
        async for event in self._run_model_tool_loop(rendered_messages, sid, ctx):
            if event.get("type") == "model_response":
                final_content = event.get("content", "")
                final_usage = event.get("usage")
                final_finish_reason = event.get("finish_reason", "stop")
            if event.get("type") == "cancelled":
                cancelled = True
                final_finish_reason = "cancelled"
                break

        if cancelled:
            result = ChatResult(
                role="assistant",
                content=final_content or "Turn cancelled by user.",
                model=self.config["model"]["name"],
                usage=final_usage,
                finish_reason="cancelled",
            )
            await self._emit(sid, {"type": "turn_cancelled"})
            return result

        # The loop already persisted the final assistant message; just construct ChatResult
        result = ChatResult(
            role="assistant",
            content=final_content,
            model=self.config["model"]["name"],
            usage=final_usage,
            finish_reason=final_finish_reason,
        )
        await self._store_memory(list(history), result, ctx)
        await self._run_hooks("after_turn", {"result": result.__dict__}, ctx)
        await self._emit(sid, {"type": "turn_end", "result": result.__dict__})
        return result

    async def chat_with_events(self, messages: Iterable[ChatMessage], session_id: str | None = None) -> tuple[str, ChatResult, List[Dict[str, Any]]]:
        sid = session_id or str(uuid.uuid4())
        self.event_bus.clear_history(sid)
        result = await self.chat(messages, session_id=sid)
        return sid, result, self.event_bus.history(sid)

    async def chat_streaming(
        self,
        messages: Iterable[ChatMessage],
        session_id: str | None = None,
        cancellation_token: CancellationToken | None = None,
    ) -> AsyncIterator[Dict[str, Any]]:
        """Yields events in real-time as the model streams tokens and tools execute."""

        sid = session_id or str(uuid.uuid4())
        new_messages = list(messages)
        ctx = RuntimeContext(session_id=sid, config=self.config)
        ctx.metadata["_runtime"] = self

        history = self._session_history.setdefault(sid, [])
        if not history:
            loaded = self._load_session_from_disk(sid)
            if loaded:
                history.extend(loaded)
            else:
                FileStorage.create_session(self.project_id, {
                    "id": sid,
                    "time": {"created": int(time.time() * 1000), "updated": int(time.time() * 1000)},
                })

        history.extend(new_messages)
        for msg in new_messages:
            self._persist_message(sid, msg)

        await self._run_hooks("before_turn", {"messages": [m.__dict__ for m in new_messages]}, ctx)
        yield {"type": "turn_start", "session_id": sid}

        rendered_messages = self._apply_prompt(list(history), ctx)
        _last = rendered_messages[-1].content if rendered_messages else ""
        if isinstance(_last, list):
            _last = " ".join(p.get("text", "") for p in _last if isinstance(p, dict) and p.get("type") == "text")
        skill_output = await self._maybe_apply_skill(_last, ctx)
        if skill_output:
            rendered_messages.append(ChatMessage(role="system", content=f"Skill output: {skill_output}"))

        try:
            async for event in self._run_model_tool_loop(rendered_messages, sid, ctx, cancellation_token):
                yield event
        except Exception as exc:
            yield {"type": "turn_error", "session_id": sid, "error": str(exc), "class": exc.__class__.__name__}
        finally:
            yield {"type": "turn_end", "session_id": sid}

    async def _run_model_tool_loop(
        self,
        messages: List[ChatMessage],
        session_id: str,
        ctx: RuntimeContext,
        cancellation_token: CancellationToken | None = None,
    ) -> AsyncIterator[Dict[str, Any]]:

        await self._connect_mcp_if_needed()
        model_cfg = self.config["model"]
        raw_max = self.config.get("tool", {}).get("max_rounds", 10)
        max_rounds = None if raw_max in (0, None, "0") else int(raw_max or 10)
        context_window = int(self.config.get("memory", {}).get("context_window_tokens", 200000) or 200000)
        working = list(messages)
        api_tools = self._build_tools_param(ctx)
        tool_call_history: Dict[str, int] = {}
        is_sub = ctx.metadata.get("_subagent", False) if ctx else False
        sub_call_id = ctx.metadata.get("_subagent_call_id") if ctx else None

        def _flag(payload: dict) -> dict:
            if is_sub:
                payload["_subagent"] = True
            return payload

        # Handle slash commands (e.g., /compact)
        _last_user_text = working[-1].content if working and working[-1].role == "user" else ""
        if isinstance(_last_user_text, list):
            _last_user_text = " ".join(p.get("text", "") for p in _last_user_text if isinstance(p, dict) and p.get("type") == "text")
        if _last_user_text.strip() == "/compact":
            summary_list, _compacted = await compact_messages(
                working, context_window, model_cfg["name"], self.model_adapter
            )
            # LLM context is now just the summary — the originals are kept
            # on disk (via the server's /compact endpoint) for the UI's
            # expand affordance, but should not bloat this chat()'s prompt.
            working = summary_list
            had_summary = any(m._compaction_summary for m in working)
            event = {"type": "context_compacted", "round": 0, "note": "Manual compact triggered by /compact"}
            yield _flag(event)
            await self._emit(session_id, event)
            # If compact_messages had nothing to compress (e.g. only one user
            # message, or the model returned empty), surface a clear noop
            # message instead of the misleading "Context has been compacted."
            if had_summary:
                content = "Context has been compacted."
            else:
                content = "Nothing to compact — context is already minimal."
            event = {"type": "model_response", "round": 0, "content": content, "usage": None, "finish_reason": "stop"}
            yield _flag(event)
            await self._emit(session_id, event)
            self._session_history.setdefault(session_id, []).append(ChatMessage(role="assistant", content=content))
            self._persist_message(session_id, ChatMessage(role="assistant", content=content), is_subagent=is_sub, sub_call_id=sub_call_id)
            return

        round_idx = 0
        while max_rounds is None or round_idx < max_rounds:
            round_idx += 1
            if cancellation_token and cancellation_token.is_cancelled:
                event = {"type": "cancelled", "round": round_idx}
                yield _flag(event)
                await self._emit(session_id, event)
                return

            if is_overflow(working, context_window):
                yield _flag({"type": "status", "content": "compact", "round": round_idx})
                await self._emit(session_id, {"type": "status", "content": "compact", "round": round_idx})
                summary_list, _compacted = await compact_messages(
                    working, context_window, model_cfg["name"], self.model_adapter
                )
                # Drop the compacted originals from the in-flight LLM
                # context; they're persisted to disk by the server's
                # /compact endpoint (the runtime doesn't write here).
                working = summary_list or working
                event = {"type": "context_compacted", "round": round_idx}
                yield _flag(event)
                await self._emit(session_id, event)

            round_start = time.perf_counter()
            base_prompt = self.config.get("prompt", {}).get("system_prompt") or ""
            instructions = load_layered_instructions(self.workspace_root)
            env_context = self._build_environment_context()
            parts = [p for p in [base_prompt, instructions] if p]
            parts.append(env_context)
            skill_index = self.config.get("_skill_index", [])
            if skill_index:
                skill_lines = ["# Available Skills (use `read_skill` tool to load full details)", ""]
                for s in skill_index:
                    if s["description"]:
                        skill_lines.append(f"- **{s['name']}**: {s['description']}")
                    else:
                        skill_lines.append(f"- **{s['name']}**")
                parts.append("\n".join(skill_lines))
            effective_prompt = "\n\n".join(parts)

            full_content = ""
            final_tool_calls: List[ToolCallItem] = []
            final_usage: Dict[str, int] | None = None
            final_finish_reason: str | None = None

            # Stream from model
            stream = self.model_adapter.chat_stream(
                working,
                model=model_cfg["name"],
                system_prompt=effective_prompt,
                tools=api_tools if api_tools else None,
            )
            async for delta in stream:
                if cancellation_token and cancellation_token.is_cancelled:
                    event = {"type": "cancelled", "round": round_idx}
                    yield _flag(event)
                    await self._emit(session_id, event)
                    return
                if delta.content:
                    full_content += delta.content
                    event = {"type": "delta", "content": delta.content, "round": round_idx}
                    yield _flag(event)
                    await self._emit(session_id, event)
                if delta.tool_calls:
                    final_tool_calls = delta.tool_calls
                if delta.usage:
                    final_usage = delta.usage
                if delta.finish_reason:
                    final_finish_reason = delta.finish_reason

            event = {
                "type": "model_response",
                "round": round_idx,
                "content": full_content,
                "usage": final_usage,
                "finish_reason": final_finish_reason,
            }
            yield _flag(event)
            await self._emit(session_id, event)

            if not final_tool_calls:
                latency_ms = int((time.perf_counter() - round_start) * 1000)
                event = {"type": "round_complete", "round": round_idx, "latency_ms": latency_ms, "usage": final_usage}
                yield _flag(event)
                await self._emit(session_id, event)
                self.update_session_usage(session_id, final_usage)
                # Persist final assistant message
                self._session_history.setdefault(session_id, []).append(
                    ChatMessage(role="assistant", content=full_content)
                )
                self._persist_message(session_id, ChatMessage(role="assistant", content=full_content), is_subagent=is_sub, sub_call_id=sub_call_id)
                return

            assistant_msg = ChatMessage(role="assistant", content=full_content, tool_calls=final_tool_calls)
            working.append(assistant_msg)
            self._session_history.setdefault(session_id, []).append(assistant_msg)
            self._persist_message(session_id, assistant_msg, is_subagent=is_sub, sub_call_id=sub_call_id)

            # Parallel tool execution with ordered event emission
            # Step 1: record call counts + emit tool_start for each call
            for tc in final_tool_calls:
                sig = f"{tc.name}:{json.dumps(tc.arguments, sort_keys=True)}"
                tool_call_history[sig] = tool_call_history.get(sig, 0) + 1
                event = {"type": "tool_start", "round": round_idx, "tool": tc.name, "arguments": tc.arguments, "call_id": tc.id}
                yield _flag(event)
                await self._emit(session_id, event)

            # Step 2: execute all tools in parallel
            async def _run_tool(tc: ToolCallItem) -> tuple[Any, bool, ToolCallItem]:
                sig = f"{tc.name}:{json.dumps(tc.arguments, sort_keys=True)}"
                count = tool_call_history.get(sig, 0)
                call_ctx = RuntimeContext(
                    session_id=ctx.session_id,
                    config=ctx.config,
                    metadata={**ctx.metadata, "_tool_call_id": tc.id},
                )
                tool_output = await self._execute_tool(ToolCall(name=tc.name, arguments=tc.arguments), call_ctx)
                is_not_found = isinstance(tool_output, dict) and tool_output.get("error") == "tool_not_found"
                if count >= 3 and not is_not_found:
                    warning = f"<reminder>'{tc.name}' has been called {count} times with the same arguments. Check prior results or try a different approach.</reminder>"
                    if isinstance(tool_output, dict):
                        tool_output["_doom_warning"] = warning
                    else:
                        tool_output = str(tool_output) + "\n\n" + warning
                return tool_output, is_not_found, tc

            tool_results = await asyncio.gather(*[_run_tool(tc) for tc in final_tool_calls])

            # Step 3: emit tool_end and process results in original order
            for tool_output, is_not_found, tc in tool_results:
                event = {
                    "type": "tool_end",
                    "round": round_idx,
                    "tool": tc.name,
                    "arguments": tc.arguments,
                    "output": tool_output,
                    "error_class": "tool_not_found" if is_not_found else None,
                    "call_id": tc.id,
                }
                yield _flag(event)
                await self._emit(session_id, event)
                if is_not_found:
                    event = {"type": "round_complete", "round": round_idx}
                    yield _flag(event)
                    await self._emit(session_id, event)
                    content = f"Tool '{tc.name}' is not available. Please check your configuration."
                    event = {"type": "model_response", "round": round_idx, "content": content, "usage": None, "finish_reason": "tool_not_found"}
                    yield _flag(event)
                    await self._emit(session_id, event)
                    self._session_history.setdefault(session_id, []).append(ChatMessage(role="assistant", content=content))
                    self._persist_message(session_id, ChatMessage(role="assistant", content=content), is_subagent=is_sub, sub_call_id=sub_call_id)
                    return
                # Check if tool returned an image — inject as multi-part content
                if (isinstance(tool_output, dict)
                        and tool_output.get("type") == "image"
                        and tool_output.get("image_url")):
                    # Tool message tells the LLM the file was read
                    summary = f"[Image file read: {tool_output.get('metadata', {}).get('path', 'unknown')}]"
                    tool_msg = ChatMessage(role="tool", content=summary, tool_call_id=tc.id, name=tc.name)
                    working.append(tool_msg)
                    self._session_history.setdefault(session_id, []).append(tool_msg)
                    self._persist_message(session_id, tool_msg, is_subagent=is_sub, sub_call_id=sub_call_id)
                    # Synthetic user message with image so the LLM can "see" it
                    image_parts: list = [
                        {"type": "text", "text": f"[Image from {tool_output.get('metadata', {}).get('path', 'file')}]"},
                        {"type": "image_url", "image_url": {"url": tool_output["image_url"]}},
                    ]
                    img_msg = ChatMessage(role="user", content=image_parts)
                    working.append(img_msg)
                    self._session_history.setdefault(session_id, []).append(img_msg)
                    self._persist_message(session_id, img_msg, is_subagent=is_sub, sub_call_id=sub_call_id)
                elif isinstance(tool_output, dict) and tool_output.get("status") == "cancelled":
                    # User interrupted this tool execution (Claude Code style)
                    result_content = f"<tool_use_error>The user interrupted this tool execution ({tc.name})</tool_use_error>"
                    tool_msg = ChatMessage(role="tool", content=result_content, tool_call_id=tc.id, name=tc.name)
                    working.append(tool_msg)
                    self._session_history.setdefault(session_id, []).append(tool_msg)
                    self._persist_message(session_id, tool_msg, is_subagent=is_sub, sub_call_id=sub_call_id)
                else:
                    result_content = json.dumps(tool_output, ensure_ascii=False) if isinstance(tool_output, dict) else str(tool_output)
                    tool_msg = ChatMessage(role="tool", content=result_content, tool_call_id=tc.id, name=tc.name)
                    working.append(tool_msg)
                    self._session_history.setdefault(session_id, []).append(tool_msg)
                    self._persist_message(session_id, tool_msg, is_subagent=is_sub, sub_call_id=sub_call_id)

            # If any tool was cancelled (user hit stop), abort the loop
            if any(isinstance(o, dict) and o.get("status") == "cancelled" for o, _, _ in tool_results):
                event = {"type": "cancelled", "round": round_idx}
                yield _flag(event)
                await self._emit(session_id, event)
                return

            latency_ms = int((time.perf_counter() - round_start) * 1000)
            event = {"type": "round_complete", "round": round_idx, "latency_ms": latency_ms, "usage": final_usage}
            yield _flag(event)
            await self._emit(session_id, event)
            self.update_session_usage(session_id, final_usage)

        # max_rounds reached
        content = "Tool execution reached max_rounds without final answer."
        event = {"type": "model_response", "round": round_idx, "content": content, "usage": None, "finish_reason": "max_rounds"}
        yield _flag(event)
        await self._emit(session_id, event)
        event = {"type": "round_complete", "round": round_idx}
        yield _flag(event)
        await self._emit(session_id, event)
        self._session_history.setdefault(session_id, []).append(ChatMessage(role="assistant", content=content))
        self._persist_message(session_id, ChatMessage(role="assistant", content=content), is_subagent=is_sub, sub_call_id=sub_call_id)

    async def _connect_mcp_if_needed(self) -> None:
        if self._mcp_connected:
            return

        from ziva_runtime.adapters.mcp.client import MCPClient, parse_mcp_config

        mcp_configs = parse_mcp_config(self.config)
        if not mcp_configs:
            self._mcp_connected = True
            return

        try:
            client = MCPClient(mcp_configs)
            tools = await client.connect_all()
            # Register MCP tools in the registry
            for tool in tools:
                self.registry.register(
                    capability_id=f"mcp.{tool._name}",
                    kind="tool",
                    instance=tool,
                    manifest={
                        "version": "0.0.1",
                        "permissions": {"tool": [tool._name]},
                        "enabled_by_default": True,
                        "path": "mcp",
                    },
                )
            self._mcp_client = client
            self._mcp_connected = True
        except Exception as e:
            print(f"MCP initialization failed: {e}")
            self._mcp_connected = True

    def _build_tools_param(self, ctx: RuntimeContext | None = None) -> list[dict]:
        """Build OpenAI-format tools list from registered tools, filtered by context."""
        is_subagent = ctx and ctx.metadata.get("_subagent")
        allowed_tools = ctx.metadata.get("_allowed_tools") if ctx else None
        tools = []
        for tool_rec in self.registry.list_kind("tool"):
            spec = tool_rec.instance.spec()
            name = spec["name"]
            # Sub-agents cannot see spawn_agent in their tool list
            if is_subagent and name == "spawn_agent":
                continue
            # If whitelist specified, only include those tools
            if allowed_tools is not None and name not in allowed_tools:
                continue
            tools.append({
                "type": "function",
                "function": {
                    "name": name,
                    "description": spec.get("description", ""),
                    "parameters": spec.get("input_schema", {"type": "object", "properties": {}}),
                },
            })
        return tools

    async def _emit(self, session_id: str, event: Dict[str, Any]) -> None:
        seq = self.event_seq.get(session_id, 0) + 1
        self.event_seq[session_id] = seq
        payload = {"session_id": session_id, "seq": seq, "ts": int(time.time() * 1000)}
        payload.update(event)
        await self.event_bus.publish(session_id, payload)

    async def await_user_answer(self, session_id: str, call_id: str = "") -> Dict[str, Any]:
        """Block the calling tool until the user replies via the UI.

        Used by the `ask_user` tool: instead of returning a fake
        "waiting" tool_result, the tool awaits this future so the model
        round stays open until the user actually answers. The future
        is set by `set_user_answer` (called from the server's
        `/sessions/{sid}/questions/reply` handler). If the turn is
        cancelled while waiting, returns a cancelled envelope so the
        LLM can react gracefully instead of hanging.
        """
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        self._pending_questions.setdefault(session_id, {})[call_id] = fut
        try:
            return await fut
        except asyncio.CancelledError:
            return {
                "status": "cancelled",
                "message": "User cancelled the turn before answering.",
            }
        finally:
            pending = self._pending_questions.get(session_id)
            if pending and pending.get(call_id) is fut:
                pending.pop(call_id, None)
                if not pending:
                    self._pending_questions.pop(session_id, None)

    def set_user_answer(self, session_id: str, answer: str | None, call_id: str = "") -> bool:
        """Resolve the pending question future for `session_id` + `call_id`.

        Returns True if a future was pending (and was set), False if
        there was no question waiting — the caller (the HTTP handler)
        can use that to 404 instead of claiming success.
        """
        pending = self._pending_questions.get(session_id)
        if not pending:
            return False
        fut = pending.get(call_id)
        if fut is None or fut.done():
            return False
        if answer is None:
            fut.cancel()
        else:
            fut.set_result({"status": "answered", "answer": answer})
        return True

    def cancel_all_questions(self, session_id: str) -> None:
        """Cancel all pending question futures for a session (called on turn cancel)."""
        pending = self._pending_questions.pop(session_id, None)
        if not pending:
            return
        for fut in pending.values():
            if not fut.done():
                fut.cancel()

    async def _execute_tool(self, call: ToolCall, ctx: RuntimeContext) -> Dict[str, Any]:
        # Check deny list (legacy, for backward compatibility)
        deny_list = self.config.get("tool", {}).get("deny", [])
        if call.name in deny_list:
            return {"error": "permission_denied", "message": f"Tool '{call.name}' is in the deny list"}

        # Check sub-agent tool restrictions
        is_subagent = ctx.metadata.get("_subagent")
        if is_subagent:
            # Sub-agents can never use spawn_agent
            if call.name == "spawn_agent":
                return {"error": "tool_blocked", "message": "Sub-agents cannot spawn further sub-agents"}
            # If whitelist specified, only those tools are allowed
            allowed_tools = ctx.metadata.get("_allowed_tools")
            if allowed_tools is not None and call.name not in allowed_tools:
                return {"error": "tool_blocked", "message": f"Tool '{call.name}' is not available in this sub-agent"}

        # Check approval policy
        approval_policy: ApprovalPolicy = self.config.get("approval", {}).get("policy", "suggest")
        request_id = str(uuid.uuid4())

        # Get tool permissions for permission checking
        tool_perms = []
        tool_rec = None
        for rec in self.registry.list_kind("tool"):
            if rec.instance.spec().get("name") == call.name:
                tool_rec = rec
                tool_perms = rec.manifest.get("permissions", {}).keys()
                break

        if approval_policy == "full-auto":
            # Auto-approve all tools - no permission check needed
            pass
        elif approval_policy == "auto-edit":
            # Auto-approve tools with fs permissions, deny shell tools
            if "shell" in tool_perms:
                return {"error": "permission_denied", "message": f"Tool '{call.name}' requires shell access which is denied in auto-edit mode"}
        elif approval_policy == "suggest":
            # Use PermissionManager for real approval flow
            perm_manager = get_permission_manager()

            # Build patterns and permission list from tool call
            patterns = []
            permissions = []
            metadata = {"tool": call.name, "arguments": call.arguments}

            # Extract file paths from arguments for fs tools
            if "fs:read" in tool_perms or "fs:write" in tool_perms:
                if "file_path" in call.arguments:
                    patterns.append(call.arguments["file_path"])
                if "path" in call.arguments:
                    patterns.append(call.arguments["path"])

            # Map tool permissions to permission manager permissions
            for perm in tool_perms:
                if perm == "fs:read":
                    permissions.append("fs:read")
                elif perm == "fs:write":
                    permissions.append("fs:write")
                elif perm == "shell:execute":
                    permissions.append("shell:execute")
                elif perm == "tool":
                    permissions.append(call.name)

            # Use "*" pattern if no specific paths extracted
            if not patterns:
                patterns = ["*"]

            # Build ruleset from config
            perm_config = self.config.get("permissions", {})
            ruleset = from_config(perm_config) if perm_config else []

            try:
                # Event callback to emit permission.asked event
                async def emit_permission_event(req_info: Dict[str, Any]) -> None:
                    await self._emit(
                        ctx.session_id,
                        {
                            "type": "permission_request",
                            "request": req_info,
                        },
                    )

                # Check permissions
                for perm in permissions:
                    await perm_manager.ask(
                        sessionID=ctx.session_id,
                        permission=perm,
                        patterns=patterns,
                        ruleset=ruleset,
                        metadata=metadata,
                        requestID=request_id,
                        tool={"name": call.name, "arguments": call.arguments},
                        event_callback=emit_permission_event,
                    )
            except RejectedError:
                return {"error": "permission_rejected", "message": f"Tool '{call.name}' was rejected by user"}
            except DeniedError as e:
                return {"error": "permission_denied", "message": str(e)}
            except Exception as e:
                # For other errors, return error but don't block execution
                return {"error": "permission_error", "message": str(e)}

        # Execute the tool
        tool_timeouts = self.config.get("tool", {}).get("timeouts", {})
        default_timeout = tool_timeouts.get("default", 120)
        for tool_rec in self.registry.list_kind("tool"):
            tool = tool_rec.instance
            spec = tool.spec()
            if spec.get("name") == call.name:
                await self._run_hooks("before_tool", {"tool": call.name, "arguments": call.arguments}, ctx)
                if call.name in ("invoke_subagent", "spawn_agent"):
                    timeout = None
                else:
                    timeout = tool_timeouts.get(call.name, default_timeout)
                try:
                    out = await asyncio.wait_for(tool.run(call.arguments, ctx), timeout=timeout)
                except asyncio.TimeoutError:
                    return {"error": "timeout", "message": f"Tool '{call.name}' timed out after {timeout}s"}
                await self._run_hooks("after_tool", {"tool": call.name, "output": out}, ctx)
                out = self._maybe_truncate_tool_output(out, call.name, ctx.session_id)
                return out if isinstance(out, dict) else {"result": out}
        return {"error": "tool_not_found", "message": f"Tool '{call.name}' is not available"}

    def _apply_prompt(self, messages: List[ChatMessage], ctx: RuntimeContext) -> List[ChatMessage]:
        prompts = self.registry.list_kind("prompt")
        if not prompts:
            return messages
        provider = prompts[0].instance
        variables = self.config.get("prompt", {}).get("variables", {})
        rendered = provider.render(messages[-1].content, variables, ctx)
        out = list(messages)
        out[-1] = ChatMessage(role=out[-1].role, content=rendered)
        return out

    def _build_environment_context(self) -> str:
        timezone = _detect_timezone()
        shell = os.environ.get("SHELL", "")
        model_cfg = self.config.get("model", {})
        model_name = model_cfg.get("name", "unknown")
        models_list = model_cfg.get("models", [])
        current_model = next((m for m in models_list if m.get("name") == model_name), {})
        supports_image = current_model.get("supports_image", False)
        lines = [
            "## Environment",
            f"cwd: {self.workspace_root}",
            f"shell: {Path(shell).name if shell else ''}",
            f"current_date: {_current_date_for_timezone(timezone)}",
            f"timezone: {timezone}",
            f"model: {model_name}",
            f"supports_image: {supports_image}",
        ]
        return "\n".join(lines)

    async def _maybe_apply_skill(self, input_text: str, ctx: RuntimeContext) -> str | None:
        for skill_rec in self.registry.list_kind("skill"):
            skill = skill_rec.instance
            if skill.match(input_text, ctx):
                result = await skill.execute({"input": input_text}, ctx)
                return str(result)
        return None

    async def _store_memory(self, messages: List[ChatMessage], result: ChatResult, ctx: RuntimeContext) -> None:
        mems = self.registry.list_kind("memory")
        if not mems:
            return
        store = mems[0].instance
        await store.put("last_turn", {"messages": [m.__dict__ for m in messages], "result": result.__dict__}, ctx)

    async def _run_hooks(self, lifecycle: str, payload: Dict[str, Any], ctx: RuntimeContext) -> None:
        for hook_rec in self.registry.list_kind("hook"):
            hook = hook_rec.instance
            event_name = getattr(hook, "event_name", "")
            if event_name == lifecycle:
                await hook.handle(payload, ctx)

    def list_tools(self) -> List[Dict[str, Any]]:
        specs = []
        for tool_rec in self.registry.list_kind("tool"):
            tool = tool_rec.instance
            specs.append(tool.spec())
        return specs

    # ============= Storage Methods =============

    def _load_session_from_disk(self, session_id: str) -> List[ChatMessage]:
        """Load messages from disk for a session.

        Returns the LLM-visible context: if a compaction summary exists,
        return just `[summary]` (no recent tail, no compacted originals).
        The originals remain on disk for the UI's expand affordance but
        should not bloat the next chat() call's prompt.
        """
        messages = []
        for msg_data in FileStorage.get_messages(self.project_id, session_id):
            messages.append(ChatMessage(
                role=msg_data.get("role", "user"),
                content=msg_data.get("content", ""),
                tool_call_id=msg_data.get("tool_call_id"),
                name=msg_data.get("name"),
                tool_calls=[
                    ToolCallItem(
                        id=tc.get("id", ""),
                        name=tc.get("name", ""),
                        arguments=tc.get("arguments", {}),
                    )
                    for tc in msg_data.get("tool_calls", [])
                ],
                _compaction_summary=msg_data.get("_compaction_summary", False),
                _compacted=msg_data.get("_compacted", False),
            ))
        return _llm_context(messages)

    def _persist_message(self, session_id: str, message: ChatMessage, is_subagent: bool = False, sub_call_id: str | None = None) -> None:
        """Persist a single message to disk."""
        record = {
            "role": message.role,
            "content": message.content,
            "tool_call_id": message.tool_call_id,
            "name": message.name,
            "tool_calls": [
                {"id": tc.id, "name": tc.name, "arguments": tc.arguments}
                for tc in message.tool_calls
            ],
        }
        if is_subagent:
            record["_subagent"] = True
        if sub_call_id:
            record["_subagent_call_id"] = sub_call_id
        if message._compaction_summary:
            record["_compaction_summary"] = True
        if message._compacted:
            record["_compacted"] = True
        FileStorage.append_message(self.project_id, session_id, record)
        FileStorage.update_session(self.project_id, session_id, {
            "id": session_id,
            "time": {"updated": int(time.time() * 1000)}
        })

    def update_session_usage(self, session_id: str, usage: Dict[str, int] | None) -> None:
        """Persist usage data to session metadata."""
        if usage and usage.get("prompt_tokens"):
            FileStorage.update_session(self.project_id, session_id, {
                "last_usage": usage,
            })

    def _maybe_truncate_tool_output(self, output: Any, tool_name: str, session_id: str) -> Any:
        """Truncate very long tool outputs and save full content to a file.

        Budget: 5% of the context window (approx 4 chars per token).
        """
        context_window = int(
            self.config.get("memory", {}).get("context_window_tokens", 200000) or 200000
        )
        max_tool_tokens = int(context_window * 0.05)
        # Approximate token->char conversion: 1 token ~ 4 chars for mixed content
        max_tool_output_chars = max_tool_tokens * 4

        # Don't truncate error outputs, skill instructions, or image data
        if isinstance(output, dict) and output.get("error"):
            return output
        if tool_name == "read_skill":
            return output
        if isinstance(output, dict) and output.get("type") == "image":
            return output

        out_str = json.dumps(output, ensure_ascii=False) if isinstance(output, dict) else str(output)
        if len(out_str) <= max_tool_output_chars:
            return output

        # Save full content to a file
        truncated = out_str[:max_tool_output_chars] + "\n... (truncated)"
        tmp_dir = Path(self.workspace_root) / "tmp"
        tmp_dir.mkdir(exist_ok=True)
        file_name = f"tool_output_{tool_name}_{int(time.time() * 1000)}.txt"
        file_path = tmp_dir / file_name

        try:
            file_path.write_text(out_str, encoding="utf-8")
        except Exception:
            pass

        note = (
            f"\n<reminder>Output truncated. Full content saved to {file_path}. Use read_file to retrieve it.</reminder>"
            if file_path.exists()
            else ""
        )
        return {
            "_truncated": True,
            "preview": truncated + note,
        }

    def list_sessions(self) -> List[dict]:
        """List all sessions for this project."""
        return FileStorage.list_sessions(self.project_id)

    def get_session(self, session_id: str) -> dict | None:
        """Get session metadata."""
        return FileStorage.get_session(self.project_id, session_id)

    def delete_session(self, session_id: str) -> None:
        """Delete a session and its messages."""
        FileStorage.delete_session(self.project_id, session_id)
        if session_id in self._session_history:
            del self._session_history[session_id]

    async def shutdown(self) -> None:
        """Gracefully shutdown runtime, disconnecting MCP servers."""
        if self._mcp_client:
            try:
                await self._mcp_client.cleanup()
            except Exception:
                pass
            self._mcp_connected = False
