from __future__ import annotations

import asyncio
import fcntl
import json
import logging
import mimetypes
import os
import pty
import shlex
import struct
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List

from aiohttp import web

from ziva.config.loader import _deep_merge
from ziva.runtime import Runtime
from ziva.shared_types import CancellationToken, ChatMessage, ChatResult, ToolCallItem
from ziva.storage.file_storage import FileStorage, _project_hash


logger = logging.getLogger(__name__)


@dataclass
class Automation:
    id: str
    name: str
    prompt: str
    interval_seconds: int
    session_id: str
    enabled: bool = True
    last_run: float | None = None
    last_result: str | None = None
    last_error: str | None = None
    next_run: float | None = None
    run_count: int = 0
    schedule_time: str | None = None  # HH:MM:SS format for daily runs
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Automation":
        return cls(
            id=str(data.get("id") or uuid.uuid4()),
            name=str(data.get("name") or "unnamed"),
            prompt=str(data.get("prompt") or ""),
            interval_seconds=max(1, int(data.get("interval_seconds") or 300)),
            session_id=str(data.get("session_id") or uuid.uuid4()),
            enabled=bool(data.get("enabled", True)),
            last_run=data.get("last_run"),
            last_result=data.get("last_result"),
            last_error=data.get("last_error"),
            next_run=data.get("next_run"),
            run_count=int(data.get("run_count") or 0),
            schedule_time=data.get("schedule_time"),
            created_at=float(data.get("created_at") or time.time()),
            updated_at=float(data.get("updated_at") or time.time()),
        )

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class SessionStore:
    runtime: Runtime
    _loaded_sessions: Dict[str, Dict] = field(default_factory=dict)

    def _pid(self, sid: str) -> str:
        """Resolve project_id from session context, falling back to active workspace."""
        session = self.runtime._sessions.get(sid)
        if session and session.project_id:
            return session.project_id
        return self.runtime.project_id

    def create(self) -> str:
        sid = str(uuid.uuid4())
        self._loaded_sessions[sid] = {"id": sid, "messages": [], "turns": []}
        # Initialize in file storage
        FileStorage.create_session(self.runtime.project_id, {
            "id": sid,
            "time": {"created": int(time.time() * 1000), "updated": int(time.time() * 1000)},
        })
        return sid

    def _ensure_loaded(self, sid: str) -> Dict:
        """Ensure session is loaded from storage."""
        if sid not in self._loaded_sessions:
            # Load from file storage
            session = FileStorage.get_session(self._pid(sid), sid)
            if session:
                messages = []
                for msg_data in FileStorage.get_messages(self._pid(sid), sid):
                    messages.append(msg_data)
                self._loaded_sessions[sid] = {
                    "id": sid,
                    "messages": messages,
                    "turns": [],
                }
            else:
                # Create new in-memory session
                self._loaded_sessions[sid] = {"id": sid, "messages": [], "turns": []}
        return self._loaded_sessions.get(sid, {})

    def get_session(self, sid: str) -> Dict | None:
        """Get session data, loading from storage if needed."""
        session = self._ensure_loaded(sid)
        if not session:
            return None
        return session

    def add_message(self, sid: str, role: str, content: str) -> None:
        """Add a message to session and persist to storage."""
        session = self._ensure_loaded(sid)
        session["messages"].append({"role": role, "content": content})
        # Persist to file storage
        FileStorage.append_message(self._pid(sid), sid, {"role": role, "content": content})

    def list_all(self) -> list[Dict]:
        """List all sessions from file storage."""
        return FileStorage.list_sessions(self.runtime.project_id)

    def exists(self, sid: str) -> bool:
        """Check if session exists."""
        return FileStorage.get_session(self._pid(sid), sid) is not None


class DesktopAPIServer:
    def __init__(self, runtime: Runtime) -> None:
        self.runtime = runtime
        self.runtime.automation_callback = self._reload_automations
        self.store = SessionStore(runtime=runtime)
        self.automations: Dict[str, Automation] = {}
        self._automation_tasks: Dict[str, asyncio.Task] = {}
        self._runner: web.AppRunner | None = None
        # Set when STT warmup finishes (or fails). The frontend can poll
        # /api/stt/status to render a "preparing voice input…" hint while
        # the model is loading. None == unknown / not started yet.
        self._stt_status: str = "idle"
        self._setup_app()

    def _pid_for(self, sid: str) -> str:
        """Resolve project_id from session context, falling back to active workspace.

        If the session is not currently loaded in memory (e.g. it was created
        before this process started or belongs to a recently-used workspace),
        search the known workspace directories on disk so cross-workspace
        uploads and message reads still route to the correct project.
        """
        session = self.runtime._sessions.get(sid)
        if session and session.project_id:
            return session.project_id

        for ws in [str(self.runtime.workspace_root), *self._read_recent_workspaces()]:
            if not ws:
                continue
            try:
                pid = _project_hash(Path(ws))
                if FileStorage.get_session(pid, sid):
                    return pid
            except Exception:
                continue

        return self.runtime.project_id

    # Bump the per-request body limit well above aiohttp's 1 MB
    # default so the attachment-upload endpoint can receive raw
    # image files (screenshots, photos). The /turns endpoint body
    # is KB-class because it only carries file paths now, so this
    # 25 MB ceiling is essentially only used by /attachments.
    def _setup_app(self) -> None:
        self.app = web.Application(client_max_size=25 * 1024 * 1024)
        self.app.router.add_get("/", self.index)
        self.app.router.add_get("/sessions", self.list_sessions)
        self.app.router.add_post("/sessions", self.create_session)
        self.app.router.add_get("/sessions/{sid}/messages", self.get_messages)
        self.app.router.add_get("/sessions/{sid}/turns", self.get_turns)
        self.app.router.add_post("/sessions/{sid}/turns", self.create_turn)
        self.app.router.add_post("/sessions/{sid}/compact", self.compact_session)
        self.app.router.add_post("/sessions/{sid}/prune", self.prune_session)
        self.app.router.add_post("/sessions/{sid}/attachments", self.upload_attachment)
        self.app.router.add_get("/attachments", self.serve_attachment)
        self.app.router.add_post("/sessions/{sid}/cancel", self.cancel_turn)
        self.app.router.add_get("/events", self.events_global)
        self.app.router.add_get("/sessions/{sid}/events", self.events)
        self.app.router.add_get("/sessions/{sid}/tools", self.get_tools_status)
        self.app.router.add_get("/sessions/{sid}/plan", self.get_plan)
        self.app.router.add_get("/sessions/{sid}/diff", self.get_diff)
        self.app.router.add_get("/sessions/{sid}/git-branches", self.get_git_branches)
        self.app.router.add_get("/api/workspace/git-branches", self.get_workspace_git_branches)
        self.app.router.add_post("/sessions/{sid}/git-checkout", self.git_checkout)
        self.app.router.add_post("/api/workspace/git-checkout", self.workspace_git_checkout)
        self.app.router.add_post("/sessions/{sid}/revert", self.revert_files)
        self.app.router.add_patch("/sessions/{sid}", self.update_session)
        self.app.router.add_post("/automations", self.create_automation)
        self.app.router.add_get("/automations", self.list_automations)
        self.app.router.add_patch("/automations/{aid}", self.update_automation)
        self.app.router.add_post("/automations/{aid}/run", self.run_automation_now)
        self.app.router.add_delete("/automations/{aid}", self.delete_automation)
        self.app.router.add_post("/api/permissions/{request_id}/reply", self.permission_reply)
        self.app.router.add_post("/sessions/{sid}/questions/reply", self.question_reply)
        self.app.router.add_delete("/sessions/{sid}", self.delete_session)
        self.app.router.add_get("/status", self.get_status)
        self.app.router.add_get("/mcp-status", self.get_mcp_status)
        self.app.router.add_get("/config", self.get_config)
        self.app.router.add_patch("/config", self.update_config)
        self.app.router.add_get("/config/yaml", self.get_config_yaml)
        self.app.router.add_put("/config/yaml", self.save_config_yaml)
        self.app.router.add_get("/config/json", self.get_config_json)
        self.app.router.add_put("/config/json", self.save_config_json)
        self.app.router.add_get("/skills", self.list_skills)
        self.app.router.add_get("/skills/file", self.read_skill_file)
        self.app.router.add_get("/api/system/choose-folder", self.choose_folder)
        self.app.router.add_get("/api/workspace/recent", self.get_recent_workspaces)
        self.app.router.add_post("/api/workspace/switch", self.switch_workspace)
        self.app.router.add_post("/api/workspace/remove", self.remove_workspace)
        # Panel endpoints: files tree, file read, terminal websocket, proxy
        self.app.router.add_get("/api/files/tree", self.files_tree)
        self.app.router.add_get("/api/files/read", self.files_read)
        self.app.router.add_get("/ws/terminal", self.terminal_ws)
        self.app.router.add_get("/api/proxy", self.proxy_url)
        self.app.router.add_post("/api/stt", self.speech_to_text)
        self.app.router.add_get("/api/stt/status", self.stt_status)
        self.app.router.add_get("/api/agents", self.list_background_agents)
        self.app.router.add_get("/api/agents/{agent_id}", self.get_background_agent)
        self.app.router.add_post("/api/agents/{agent_id}/cancel", self.cancel_background_agent)
        # Serve static assets from build output
        if getattr(sys, 'frozen', False):
            static_dir = Path(sys._MEIPASS) / "static"
        else:
            static_dir = Path(__file__).resolve().parent / "static"
        self.app.router.add_static("/assets", static_dir / "assets")
        self.app.on_startup.append(self._on_startup)
        self.app.on_cleanup.append(self._on_cleanup)

    async def _on_startup(self, _app: web.Application) -> None:
        self._load_persisted_automations()
        self._schedule_enabled_automations()

    async def _on_cleanup(self, _app: web.Application) -> None:
        await self._cancel_automation_tasks()

    def _reload_automations(self) -> None:
        """Called by tools when automations are modified via FileStorage."""
        # Force reload from disk
        self.automations.clear()
        self._load_persisted_automations()
        self._schedule_enabled_automations()

    def _load_persisted_automations(self) -> None:
        if self.automations:
            return
        for item in FileStorage.list_automations(self.runtime.project_id):
            automation = Automation.from_dict(item)
            if not automation.next_run and automation.enabled:
                automation.next_run = time.time() + automation.interval_seconds
            self.automations[automation.id] = automation

    def _persist_automation(self, automation: Automation) -> None:
        automation.updated_at = time.time()
        FileStorage.upsert_automation(self.runtime.project_id, automation.to_dict())

    def _automation_payload(self, automation: Automation) -> Dict[str, Any]:
        return automation.to_dict()

    def _next_run_timestamp(self, schedule_time: str | None, interval_seconds: int) -> float:
        """Compute the next run timestamp based on schedule_time or interval."""
        now = time.time()
        if not schedule_time:
            return now + interval_seconds
        try:
            hour, minute, second = map(int, schedule_time.split(":", 2))
            local = time.localtime(now)
            target = time.mktime((local.tm_year, local.tm_mon, local.tm_mday, hour, minute, second, 0, 0, -1))
            if target <= now:
                target += 86400  # schedule for tomorrow
            return target
        except (ValueError, TypeError):
            return now + interval_seconds

    def _schedule_enabled_automations(self) -> None:
        for automation in list(self.automations.values()):
            if automation.enabled and automation.id not in self._automation_tasks:
                self._schedule_automation(automation)

    def _schedule_automation(self, automation: Automation) -> None:
        existing = self._automation_tasks.pop(automation.id, None)
        if existing:
            existing.cancel()
        if not automation.enabled:
            return
        self._automation_tasks[automation.id] = asyncio.create_task(self._automation_runner(automation.id))

    async def _automation_runner(self, automation_id: str) -> None:
        while True:
            automation = self.automations.get(automation_id)
            if not automation or not automation.enabled:
                return
            now = time.time()
            if automation.next_run is None:
                automation.next_run = self._next_run_timestamp(automation.schedule_time, automation.interval_seconds)
                self._persist_automation(automation)
            await asyncio.sleep(max(0, automation.next_run - now))
            automation = self.automations.get(automation_id)
            if not automation or not automation.enabled:
                return
            await self._run_automation_once(automation, scheduled=True)

    async def _run_automation_once(self, automation: Automation, *, scheduled: bool) -> ChatResult | None:
        try:
            if not self.store.exists(automation.session_id):
                FileStorage.create_session(self._pid_for(automation.session_id), {
                    "id": automation.session_id,
                    "time": {"created": int(time.time() * 1000), "updated": int(time.time() * 1000)},
                })
            messages = [ChatMessage(role="user", content=automation.prompt)]
            result = await self.runtime.chat(messages, session_id=automation.session_id)
            automation.last_run = time.time()
            automation.last_result = result.content
            automation.last_error = None
            automation.run_count += 1
            automation.next_run = self._next_run_timestamp(automation.schedule_time, automation.interval_seconds) if automation.enabled else None
            self._persist_automation(automation)
            await self.runtime._emit(automation.session_id, {
                "type": "automation_run",
                "automation_id": automation.id,
                "name": automation.name,
                "scheduled": scheduled,
                "status": "done",
            })
            return result
        except Exception as exc:
            automation.last_run = time.time()
            automation.last_error = str(exc)
            automation.next_run = self._next_run_timestamp(automation.schedule_time, automation.interval_seconds) if automation.enabled else None
            self._persist_automation(automation)
            await self.runtime._emit(automation.session_id, {
                "type": "automation_run",
                "automation_id": automation.id,
                "name": automation.name,
                "scheduled": scheduled,
                "status": "failed",
                "error": str(exc),
                "class": exc.__class__.__name__,
            })
            return None

    async def _cancel_automation_tasks(self) -> None:
        tasks = list(self._automation_tasks.values())
        self._automation_tasks.clear()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def index(self, _request: web.Request) -> web.Response:
        if getattr(sys, 'frozen', False):
            html = (Path(sys._MEIPASS) / "static" / "index.html").read_text(encoding="utf-8")
        else:
            html = (Path(__file__).resolve().parent / "static" / "index.html").read_text(encoding="utf-8")
        return web.Response(text=html, content_type="text/html")

    async def create_session(self, request: web.Request) -> web.Response:
        # Optional initial model_name so the caller can pin the session
        # to a specific model at creation time (the alternative is to
        # create the session and then immediately PATCH /sessions/{sid}
        # with model_name; both paths persist to disk via FileStorage).
        model_name: str | None = None
        if request.body_exists:
            try:
                payload = await request.json()
            except Exception:
                payload = None
            if isinstance(payload, dict):
                raw = payload.get("model_name")
                if isinstance(raw, str) and raw:
                    model_name = raw
        sid = self.store.create()
        if model_name is not None:
            FileStorage.update_session(self.runtime.project_id, sid, {"model_name": model_name})
        return web.json_response({"id": sid, "model_name": model_name})

    @staticmethod
    def _read_recent_workspaces() -> List[str]:
        """Read the persisted recent-workspace list from disk."""
        recent_path = Path.home() / ".ziva" / "recent_workspaces.json"
        try:
            if recent_path.exists():
                data = json.loads(recent_path.read_text())
                if isinstance(data, list):
                    return [str(p) for p in data]
        except Exception:
            pass
        return []

    async def list_sessions(self, request: web.Request) -> web.Response:
        """List sessions across all known recent workspaces (and the active one).

        The active workspace is always included even if it isn't in the recent
        list yet. Each session is tagged with its ``workspace`` so the sidebar
        can group sessions per project. ``preview`` and ``name`` are surfaced
        when present on disk so the sidebar can show a meaningful title
        without an extra round trip per session.
        """
        current_ws = str(self.runtime.workspace_root)
        workspaces: List[str] = []
        seen: set = set()
        for ws in [current_ws, *self._read_recent_workspaces()]:
            if ws and ws not in seen:
                seen.add(ws)
                workspaces.append(ws)

        items: List[Dict[str, Any]] = []
        for ws in workspaces:
            try:
                pid = _project_hash(Path(ws))
                for s in FileStorage.list_sessions(pid):
                    if "id" not in s:
                        continue
                    items.append({
                        "id": s["id"],
                        "time": s.get("time"),
                        "workspace": ws,
                        "name": s.get("name"),
                        "model_name": s.get("model_name"),
                    })
            except Exception:
                # Skip workspaces that are unreadable / missing storage
                continue

        items.sort(key=lambda x: (x.get("time") or {}).get("updated", 0), reverse=True)
        return web.json_response({"sessions": items, "workspaces": workspaces})

    async def get_messages(self, request: web.Request) -> web.Response:
        sid = request.match_info["sid"]
        if not self.store.exists(sid):
            return web.json_response({"error": "session_not_found"}, status=404)
        # Always read from FileStorage so we get messages persisted by the
        # runtime during a running turn (tool results, assistant text, etc.).
        # The in-memory _loaded_sessions cache may be stale if the runtime
        # has persisted new messages since the session was first loaded.
        meta = FileStorage.get_session(self._pid_for(sid), sid) or {}
        all_msgs = list(FileStorage.get_messages(self._pid_for(sid), sid))
        # By default, return the LLM-visible view (last summary + messages
        # after it). With ?include_dropped=true, return the full chronological
        # history so the UI's collapse bars can render folded messages.
        include_dropped = request.query.get("include_dropped") == "true"
        if include_dropped:
            msgs = all_msgs
        else:
            from ziva.session.compaction import _llm_context
            msgs = _llm_context(all_msgs)
        return web.json_response({
            "messages": msgs,
            "last_usage": meta.get("last_usage"),
            "model_name": meta.get("model_name") or (meta.get("model_cfg", {}).get("name") if isinstance(meta.get("model_cfg"), dict) else None),
        })

    async def get_turns(self, request: web.Request) -> web.Response:
        sid = request.match_info["sid"]
        session = self.store.get_session(sid)
        if not session:
            return web.json_response({"error": "session_not_found"}, status=404)
        
        turns = []
        for turn in session.get("turns", []):
            t = turn.copy()
            if t.get("status") == "running":
                t["events"] = self.runtime.event_bus.history(sid)
            turns.append(t)
            
        return web.json_response({"turns": turns})

    async def create_turn(self, request: web.Request) -> web.Response:
        sid = request.match_info["sid"]
        if not self.store.exists(sid):
            # Auto-create session on first message (lazy creation).
            # Frontend generates UUID locally; session is only persisted
            # when the user actually sends the first message.
            FileStorage.create_session(self.runtime.project_id, {
                "id": sid,
                "time": {"created": int(time.time() * 1000), "updated": int(time.time() * 1000)},
            })

        # Reject if a turn is already in-flight for this session.
        rt_session = self.runtime._sessions.get(sid)
        if rt_session and rt_session.turn_task is not None and not rt_session.turn_task.done():
            return web.json_response({"error": "turn_already_running"}, status=429)

        payload = await request.json()
        messages = payload.get("messages") or []
        chat_messages = [ChatMessage(role=str(m.get("role", "user")), content=m.get("content", "")) for m in messages]
        turn_id = str(uuid.uuid4())
        turn = {"id": turn_id, "status": "running", "events": [], "result": None}
        session = self.store._ensure_loaded(sid)
        session["turns"].append(turn)

        if "messages" not in session:
            session["messages"] = []
        for m in chat_messages:
            session["messages"].append({"role": m.role, "content": m.content})

        token = CancellationToken()
        session = self.runtime._get_session(sid)
        session.cancel_token = token

        async def runner() -> None:
            try:
                # Only pass the new user messages — runtime.chat() manages history internally.
                # The cancel_token is stashed on the context for the streaming layer to
                # observe; task.cancel() above is the primary cancellation path.
                _, result, events = await self.runtime.chat_with_events(chat_messages, session_id=sid)
                # chat_with_events already emitted turn_end. Clear turn_task
                # IMMEDIATELY — before the message reload below — so a queued
                # createTurn that the frontend flushes on turn_end doesn't 429
                # against this still-not-done task. The reload reads the whole
                # session JSONL and can outlast the frontend's flush delay,
                # which previously stranded every queued message in 429 retries.
                s = self.runtime._sessions.get(sid)
                if s:
                    if s.cancel_token is token:
                        s.cancel_token = None
                    if s.turn_task is task:
                        s.turn_task = None
                # Reload messages from disk since runtime.chat() persisted them via FileStorage
                fresh_messages = []
                for msg_data in FileStorage.get_messages(self._pid_for(sid), sid):
                    fresh_messages.append(msg_data)
                self.store._loaded_sessions[sid]["messages"] = fresh_messages
                turn["events"] = events
                turn["result"] = {"role": result.role, "content": result.content, "finish_reason": result.finish_reason}
                turn["status"] = "done"
            except asyncio.CancelledError:
                turn["status"] = "cancelled"
                # Ensure frontend receives terminal event even if chat() didn't emit it
                await self.runtime._emit(sid, {"type": "turn_cancelled"})
            except Exception as exc:
                turn["status"] = "failed"
                turn["error"] = {"message": str(exc), "class": exc.__class__.__name__}
                # Emit turn_error so frontend exits "running" state instead of hanging
                await self.runtime._emit(sid, {"type": "turn_error", "error": str(exc), "class": exc.__class__.__name__})
            finally:
                # Compare-and-clear: only zero out the session's cancel_token /
                # turn_task if they still point at *this* runner's. Without the
                # identity check, a NEW create_turn that landed between
                # `task.cancel()` and this finally block would have its token
                # and task reference wiped — the new turn would still run
                # (asyncio keeps the task alive) but cancel_turn would no
                # longer find it on the session, and the next stop button
                # press would silently fail to cancel.
                s = self.runtime._sessions.get(sid)
                if s:
                    if s.cancel_token is token:
                        s.cancel_token = None
                    if s.turn_task is task:
                        s.turn_task = None

        task = asyncio.create_task(runner())
        session.turn_task = task
        return web.json_response({"accepted": True, "turn_id": turn_id})

    def _apply_post_compact(self, sid: str, working_set: List[ChatMessage]) -> Dict[str, Any]:
        """Rewrite disk + sync in-memory + runtime cache + refresh last_usage.

        `last_usage.prompt_tokens` is computed from `_llm_context(working_set)`
        — only the last summary + messages after it, which is the actual cost
        of the next turn.
        """
        records = []
        for m in working_set:
            record = {
                "role": m.role,
                "content": m.content,
                "tool_call_id": m.tool_call_id,
                "name": m.name,
                "tool_calls": [{"id": tc.id, "name": tc.name, "arguments": tc.arguments} for tc in m.tool_calls],
            }
            if m.reasoning_content:
                record["reasoning_content"] = m.reasoning_content
            if m.reasoning_signature:
                record["reasoning_signature"] = m.reasoning_signature
            if m._compaction_summary:
                record["_compaction_summary"] = True
            records.append(record)
        FileStorage.replace_messages(self._pid_for(sid), sid, records)

        session = self.store._ensure_loaded(sid)
        session["messages"] = list(records)
        if sid in self.runtime._sessions:
            from ziva.session.compaction import _llm_context
            self.runtime._sessions[sid].history = _llm_context(working_set)

        from ziva.session.compaction import estimate_tokens, _llm_context
        llm_visible = _llm_context(working_set)
        new_prompt_tokens = estimate_tokens(llm_visible)
        new_usage = {"prompt_tokens": new_prompt_tokens, "completion_tokens": 0, "total_tokens": new_prompt_tokens}
        FileStorage.update_session(self._pid_for(sid), sid, {
            "last_usage": new_usage,
        })
        return new_usage

    def _load_session_messages(self, sid: str) -> List[ChatMessage]:
        """Read messages from disk as ChatMessage objects."""
        messages: List[ChatMessage] = []
        for msg_data in FileStorage.get_messages(self._pid_for(sid), sid):
            messages.append(ChatMessage(
                role=msg_data.get("role", "user"),
                content=msg_data.get("content", ""),
                tool_call_id=msg_data.get("tool_call_id"),
                name=msg_data.get("name"),
                tool_calls=[
                    ToolCallItem(id=tc.get("id", ""), name=tc.get("name", ""), arguments=tc.get("arguments", {}))
                    for tc in msg_data.get("tool_calls", [])
                ],
                _compaction_summary=msg_data.get("_compaction_summary", False),
            ))
        return messages

    def _append_summary_to_disk(self, sid: str, summary: ChatMessage) -> None:
        """Append a single compaction-summary record to the session's message file.

        Kept for back-compat with external callers, but /compact now uses
        `_apply_post_compact` to slot the summary between the older and
        recent halves of the message list (so the runtime's filter view
        keeps the recent tail).
        """
        record = {
            "role": summary.role,
            "content": summary.content,
            "tool_call_id": summary.tool_call_id,
            "name": summary.name,
            "tool_calls": [{"id": tc.id, "name": tc.name, "arguments": tc.arguments} for tc in summary.tool_calls],
            "_compaction_summary": True,
        }
        FileStorage.append_message(self._pid_for(sid), sid, record)

    async def prune_session(self, request: web.Request) -> web.Response:
        """POST /sessions/{sid}/prune — strip old tool outputs, no model call.

        Rewrites the on-disk message file with the pruned list, since the
        pruned tool outputs are genuinely gone. There is no "summary"
        marker — the pruned tail is the new working set directly.
        """
        sid = request.match_info["sid"]
        if not self.store.exists(sid):
            return web.json_response({"error": "session_not_found"}, status=404)

        messages = self._load_session_messages(sid)
        from ziva.session.compaction import prune_messages
        pruned = prune_messages(messages)

        new_usage = self._apply_post_compact(sid, pruned)
        return web.json_response({
            "success": True,
            "message_count": len(pruned),
            "last_usage": new_usage,
        })

    async def compact_session(self, request: web.Request) -> web.Response:
        """POST /sessions/{sid}/compact — generate a model summary.

        On-disk layout is chronological:
            [msg1, msg2, ..., summary1, msg3, msg4, ..., summary2, ...]
        Each summary's folded range is the messages between it and the
        previous summary (or start of list).  The LLM only sees the
        latest summary + any messages after it. Returns `noop=true` if
        there's nothing to compress.

        This endpoint is the backend path for the manual `/compact` slash
        command. The runtime's start-of-round auto-compact hook (inside
        `_run_model_tool_loop`) runs the same `compact_messages` function
        but emits its own `status: compact` / `context_compacted` events
        for the UI toast — see runtime.py. This endpoint does not emit
        those events; the manual /compact flow's loading/success toast is
        managed entirely client-side (see main.ts: `handleSlashCommand`).
        """
        sid = request.match_info["sid"]
        if not self.store.exists(sid):
            return web.json_response({"error": "session_not_found"}, status=404)

        messages = self._load_session_messages(sid)
        # Honor the session's per-session model_name (set via PATCH /sessions
        # or POST /sessions?model_name=…) so compact uses the same model as
        # the chat turns it's summarizing. Fall back to the runtime config
        # when the session hasn't pinned a model.
        model_cfg = dict(self.runtime.config.get("model", {}))
        sess = self.runtime._sessions.get(sid)
        if sess and sess.model_name:
            model_cfg["name"] = sess.model_name
        model_name = model_cfg.get("name", "")
        context_window = int(self.runtime.config.get("memory", {}).get("context_window_tokens", 200000) or 200000)

        from ziva.session.compaction import (
            _llm_context, compact_messages, compose_post_compact_on_disk,
            find_last_summary_idx, find_cutoff_in_llm_visible,
        )
        from ziva.runtime import _create_adapter
        llm_visible = _llm_context(messages)

        # Match the runtime hook's K=5 asst-turns setting.
        from ziva.runtime import AUTO_COMPACT_KEEP_LAST_ASSISTANT_TURNS
        keep_last = AUTO_COMPACT_KEEP_LAST_ASSISTANT_TURNS

        # Build a per-session merged config so the adapter matches the model
        # the session is actually running on (mirrors chat()'s logic).
        turn_config = dict(self.runtime.config)
        turn_config["model"] = model_cfg
        model_adapter = _create_adapter(turn_config)
        try:
            summary_list = await compact_messages(
                llm_visible, context_window, model_name, model_adapter,
                keep_last_assistant_turns=keep_last,
            )
        except Exception as exc:
            return web.json_response({"error": "compact_failed", "message": str(exc)}, status=500)

        # No-op detection: empty result, or the same list reference (= unable to split).
        if not summary_list or summary_list is llm_visible:
            from ziva.session.compaction import estimate_tokens
            current_tokens = estimate_tokens(messages)
            return web.json_response({
                "success": True,
                "noop": True,
                "reason": "nothing_to_compact",
                "message_count": len(messages),
                "last_usage": {
                    "prompt_tokens": current_tokens,
                    "completion_tokens": 0,
                    "total_tokens": current_tokens,
                },
            })

        # Compose on-disk: preserved_old + [new_summary, ...to_keep]
        # `messages` and `summary_list` are both ChatMessage lists.
        last_summary_idx = find_last_summary_idx(messages)
        cutoff = find_cutoff_in_llm_visible(llm_visible, keep_last)
        on_disk = compose_post_compact_on_disk(
            messages, last_summary_idx, cutoff, summary_list
        )
        new_usage = self._apply_post_compact(sid, on_disk)
        return web.json_response({
            "success": True,
            "message_count": len(summary_list),
            "original_count": len(llm_visible),
            "last_usage": new_usage,
        })

    async def events(self, request: web.Request) -> web.StreamResponse:
        sid = request.match_info["sid"]
        if not self.store.exists(sid):
            return web.json_response({"error": "session_not_found"}, status=404)

        resp = web.StreamResponse(
            status=200,
            headers={
                "Content-Type": "text/event-stream",
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
            },
        )
        queue = self.runtime.event_bus.subscribe(sid)
        # The SSE pool in the UI can rapidly disconnect the previous
        # session's stream when the user switches. If the client aborts
        # before `resp.prepare` finishes writing the response headers,
        # aiohttp raises ClientConnectionResetError — which the
        # StreamResponse's own machinery doesn't catch, and which would
        # otherwise propagate up to the aiohttp app handler as an
        # unhandled error. Catch it here so the rest of the app stays
        # healthy; the connection is gone either way.
        try:
            await resp.prepare(request)
        except (ConnectionResetError, asyncio.CancelledError):
            self.runtime.event_bus.unsubscribe(sid, queue)
            return resp
        try:
            while True:
                event = await queue.get()
                payload = await asyncio.to_thread(
                    lambda: f"data: {json.dumps(event, ensure_ascii=False, default=str)}\n\n".encode("utf-8")
                )
                await resp.write(payload)
        except (asyncio.CancelledError, ConnectionResetError):
            pass
        finally:
            self.runtime.event_bus.unsubscribe(sid, queue)
        return resp

    async def events_global(self, request: web.Request) -> web.StreamResponse:
        """Single SSE stream that fans out every session's events.

        Each event already carries a `session_id` field (set by
        runtime._emit). The frontend's main.ts routes events to the
        per-session handler by that field, so the UI only needs one
        connection regardless of how many sessions exist. This replaces
        the previous per-session `/sessions/{sid}/events` model in the
        hot path; that endpoint is kept for backward compatibility but
        no longer used by the web UI.

        The connection stays open until the client disconnects. We
        catch the same `ConnectionResetError` / `asyncio.CancelledError`
        race as the per-session handler so an aborted client during
        `resp.prepare` doesn't propagate as an unhandled error.
        """
        resp = web.StreamResponse(
            status=200,
            headers={
                "Content-Type": "text/event-stream",
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
            },
        )
        queue = self.runtime.event_bus.subscribe_global()
        try:
            await resp.prepare(request)
        except (ConnectionResetError, asyncio.CancelledError):
            self.runtime.event_bus.unsubscribe_global(queue)
            return resp
        try:
            while True:
                event = await queue.get()
                payload = await asyncio.to_thread(
                    lambda: f"data: {json.dumps(event, ensure_ascii=False, default=str)}\n\n".encode("utf-8")
                )
                await resp.write(payload)
        except (asyncio.CancelledError, ConnectionResetError):
            pass
        finally:
            self.runtime.event_bus.unsubscribe_global(queue)
        return resp

    async def get_tools_status(self, request: web.Request) -> web.Response:
        sid = request.match_info["sid"]
        session = self.store.get_session(sid)
        if not session:
            return web.json_response({"error": "session_not_found"}, status=404)

        tool_events = []
        for turn in session.get("turns", []):
            for ev in turn.get("events", []):
                if ev.get("type") in ("tool_start", "tool_end"):
                    tool_events.append(ev)
        return web.json_response({"tools": tool_events})

    async def get_plan(self, request: web.Request) -> web.Response:
        sid = request.match_info["sid"]
        session = self.store.get_session(sid)
        if not session:
            return web.json_response({"error": "session_not_found"}, status=404)

        # Prefer persisted plan from session file (survives restart)
        plan_steps = session.get("plan") or []
        # Fallback: scan turn events for latest plan output
        if not plan_steps:
            for turn in session.get("turns", []):
                for ev in turn.get("events", []):
                    if ev.get("type") == "tool_end" and isinstance(ev.get("output"), dict):
                        output = ev["output"]
                        if "plan" in output:
                            plan_steps = output["plan"]
        return web.json_response({"plan": plan_steps})

    async def get_diff(self, request: web.Request) -> web.Response:
        sid = request.match_info["sid"]
        if not self.store.exists(sid):
            return web.json_response({"error": "session_not_found"}, status=404)

        workspace = self.runtime.workspace_root
        diff_content = ""
        try:
            proc = await asyncio.create_subprocess_shell(
                "git diff HEAD",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(workspace),
            )
            stdout, _ = await proc.communicate()
            diff_content = stdout.decode("utf-8", errors="replace")
        except Exception:
            diff_content = ""
        return web.json_response({"diff": diff_content})

    async def _read_git_branches(self) -> dict:
        """Read the current branch and the full branch list from the active
        workspace. Returns a safe default when the workspace isn't a git
        repo or git isn't available."""
        workspace = self.runtime.workspace_root
        try:
            # get current branch
            proc = await asyncio.create_subprocess_shell(
                "git rev-parse --abbrev-ref HEAD", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, cwd=str(workspace)
            )
            stdout, _ = await proc.communicate()
            current = stdout.decode("utf-8").strip()

            # get all branches
            proc = await asyncio.create_subprocess_shell(
                "git branch --format='%(refname:short)'", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, cwd=str(workspace)
            )
            stdout, _ = await proc.communicate()
            branches = [b.strip() for b in stdout.decode("utf-8").splitlines() if b.strip()]
            return {"current": current, "branches": branches}
        except Exception:
            return {"current": "main", "branches": ["main"]}

    async def get_git_branches(self, request: web.Request) -> web.Response:
        sid = request.match_info["sid"]
        if not self.store.exists(sid):
            return web.json_response({"error": "session_not_found"}, status=404)
        return web.json_response(await self._read_git_branches())

    async def get_workspace_git_branches(self, request: web.Request) -> web.Response:
        """Workspace-level git branch lookup. Used by the frontend right
        after switching workspace, so the status-bar branch indicator
        updates immediately even before the user picks a session."""
        return web.json_response(await self._read_git_branches())

    async def _do_git_checkout(self, branch: str, create: bool) -> web.Response:
        """Shared git-checkout implementation. Operates on the active
        workspace, so it's reusable for both session-scoped and
        workspace-scoped endpoints."""
        workspace = self.runtime.workspace_root
        cmd = f"git checkout -b {shlex.quote(branch)}" if create else f"git checkout {shlex.quote(branch)}"
        try:
            proc = await asyncio.create_subprocess_shell(
                cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, cwd=str(workspace)
            )
            stdout, stderr = await proc.communicate()
            if proc.returncode != 0:
                return web.json_response({"error": stderr.decode("utf-8")}, status=400)
            return web.json_response({"success": True})
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    async def git_checkout(self, request: web.Request) -> web.Response:
        sid = request.match_info["sid"]
        if not self.store.exists(sid):
            return web.json_response({"error": "session_not_found"}, status=404)
        payload = await request.json()
        branch = payload.get("branch")
        create = payload.get("create", False)
        return await self._do_git_checkout(branch, create)

    async def workspace_git_checkout(self, request: web.Request) -> web.Response:
        """Workspace-level git checkout. Used when the user wants to
        switch branches from the status-bar picker even though no
        session is active yet (e.g. right after switching workspace)."""
        payload = await request.json()
        branch = payload.get("branch")
        create = payload.get("create", False)
        return await self._do_git_checkout(branch, create)

    async def create_automation(self, request: web.Request) -> web.Response:
        self._load_persisted_automations()
        payload = await request.json()
        name = str(payload.get("name") or "unnamed")
        prompt = str(payload.get("prompt") or "")
        interval = max(1, int(payload.get("interval_seconds") or 300))
        run_immediately = bool(payload.get("run_immediately", False))
        schedule_time = payload.get("schedule_time")
        if schedule_time:
            schedule_time = str(schedule_time).strip() or None

        if not prompt:
            return web.json_response({"error": "prompt is required"}, status=400)

        aid = str(uuid.uuid4())
        sid = self.store.create()
        now = time.time()
        next_run = now if run_immediately else self._next_run_timestamp(schedule_time, interval)
        automation = Automation(
            id=aid,
            name=name,
            prompt=prompt,
            interval_seconds=interval,
            session_id=sid,
            schedule_time=schedule_time,
            next_run=next_run,
        )
        self.automations[aid] = automation
        self._persist_automation(automation)
        self._schedule_automation(automation)
        return web.json_response({"id": aid, "session_id": sid, "automation": self._automation_payload(automation)})

    async def list_automations(self, _request: web.Request) -> web.Response:
        self._load_persisted_automations()
        self._schedule_enabled_automations()
        items = [self._automation_payload(a) for a in self.automations.values()]
        items.sort(key=lambda a: a.get("created_at") or 0, reverse=True)
        return web.json_response({"automations": items})


    async def update_automation(self, request: web.Request) -> web.Response:
        self._load_persisted_automations()
        aid = request.match_info["aid"]
        automation = self.automations.get(aid)
        if not automation:
            return web.json_response({"error": "not_found"}, status=404)
        payload = await request.json()
        reschedule = False
        if "name" in payload:
            automation.name = str(payload["name"] or "unnamed")
        if "prompt" in payload:
            prompt = str(payload["prompt"] or "")
            if not prompt:
                return web.json_response({"error": "prompt is required"}, status=400)
            automation.prompt = prompt
        if "interval_seconds" in payload:
            automation.interval_seconds = max(1, int(payload["interval_seconds"] or 300))
            automation.next_run = self._next_run_timestamp(automation.schedule_time, automation.interval_seconds)
            reschedule = True
        if "schedule_time" in payload:
            st = payload.get("schedule_time")
            automation.schedule_time = str(st).strip() if st else None
            automation.next_run = self._next_run_timestamp(automation.schedule_time, automation.interval_seconds)
            reschedule = True
        if "enabled" in payload:
            enabled = bool(payload["enabled"])
            if automation.enabled != enabled:
                automation.enabled = enabled
                automation.next_run = self._next_run_timestamp(automation.schedule_time, automation.interval_seconds) if enabled else None
                reschedule = True
        self._persist_automation(automation)
        if reschedule:
            self._schedule_automation(automation)
        return web.json_response({"ok": True, "automation": self._automation_payload(automation)})

    async def run_automation_now(self, request: web.Request) -> web.Response:
        self._load_persisted_automations()
        aid = request.match_info["aid"]
        automation = self.automations.get(aid)
        if not automation:
            return web.json_response({"error": "not_found"}, status=404)

        async def _run_and_emit():
            await self._run_automation_once(automation, scheduled=False)
            # Result is already persisted on the automation object and
            # emitted via the automation_run SSE event by _run_automation_once.

        asyncio.create_task(_run_and_emit())
        return web.json_response({"accepted": True, "automation": self._automation_payload(automation)})

    async def delete_automation(self, request: web.Request) -> web.Response:
        self._load_persisted_automations()
        aid = request.match_info["aid"]
        automation = self.automations.get(aid)
        if not automation:
            return web.json_response({"error": "not_found"}, status=404)
        automation.enabled = False
        task = self._automation_tasks.pop(aid, None)
        if task:
            task.cancel()
        del self.automations[aid]
        FileStorage.delete_automation(self.runtime.project_id, aid)
        return web.json_response({"deleted": True})

    async def permission_reply(self, request: web.Request) -> web.Response:
        """Handle permission approval/rejection."""
        request_id = request.match_info["request_id"]
        payload = await request.json()
        action = payload.get("action", "once")
        message = payload.get("message")

        perm_manager = self.runtime.permission_manager
        perm_manager.reply(request_id, action, message)

        return web.json_response({"ok": True})

    async def question_reply(self, request: web.Request) -> web.Response:
        """Resolve a pending ask_user question future with the user's answer.

        The ask_user tool run() blocks on a per-session future. When the
        front-end submits the answer from the question card it hits
        this endpoint, which calls Runtime.set_user_answer to unblock
        the tool — the model round then continues with the real answer
        instead of an empty "Waiting…" tool_result. 404 if no question
        is currently waiting (e.g. the user navigated away).
        """
        sid = request.match_info["sid"]
        payload = await request.json()
        answer = payload.get("answer")
        call_id = payload.get("call_id", "")
        if not isinstance(answer, str) or not answer.strip():
            return web.json_response({"error": "missing_answer"}, status=400)
        ok = self.runtime.set_user_answer(sid, answer.strip(), call_id=call_id)
        if not ok:
            return web.json_response({"error": "no_pending_question"}, status=404)
        return web.json_response({"ok": True})

    async def upload_attachment(self, request: web.Request) -> web.Response:
        """Persist a user-attached image to disk and return its path.

        The frontend (Electron / browser) cannot write directly to the
        runtime's filesystem, so the actual bytes come over the wire
        here as a multipart `file` field. We drop them under
        ``~/.ziva/sessions/<pid>/attachments/<sid>/`` — a sibling of
        the messages JSONL — and hand back the absolute path. The
        subsequent ``/turns`` request embeds that path in an
        ``image_url.url`` block, and the runtime expands it to a
        base64 data URL *only* in the per-turn copy sent to the
        provider. The persisted history keeps the path so reloads
        don't re-read multi-MB blobs from disk.

        Returns ``{path, mime, size}`` on success.
        """
        from ziva.storage.file_storage import FileStorage
        sid = request.match_info["sid"]
        pid = self._pid_for(sid)
        # Lazy-create on first attachment upload, mirroring create_turn.
        # The frontend generates UUIDs locally for new sessions and only
        # registers them with the server when the first message is sent.
        # If the user attaches an image to a brand-new session before
        # sending a message — e.g. right after switching workspace — we
        # need to persist the session here too, otherwise the upload
        # silently fails with session_not_found and the user's image
        # disappears without explanation.
        if not FileStorage.get_session(pid, sid) and sid not in self.store._loaded_sessions:
            FileStorage.create_session(pid, {
                "id": sid,
                "time": {"created": int(time.time() * 1000), "updated": int(time.time() * 1000)},
            })

        try:
            reader = await request.multipart()
        except Exception as exc:
            return web.json_response({"error": "invalid_multipart", "detail": str(exc)}, status=400)

        file_bytes: bytes | None = None
        file_name = "clip"
        mime = "application/octet-stream"
        async for part in reader:
            if part.name != "file":
                # Drain any other fields we don't care about.
                await part.read()
                continue
            file_name = part.filename or "clip"
            mime = part.headers.get("Content-Type", mime)
            file_bytes = await part.read(decode=False)
            break  # only the first `file` field

        if not file_bytes:
            return web.json_response({"error": "no_file"}, status=400)

        # Sanitize the file name to something we can put on disk
        # safely (no path traversal via a user-supplied filename).
        suffix = Path(file_name).suffix.lower()
        if suffix not in {".png", ".jpg", ".jpeg", ".gif", ".webp"}:
            suffix = ".png"
        # Filename is a timestamp + a small random suffix so concurrent
        # pastes (or two pastes within the same millisecond) never
        # collide on disk.
        ts_ms = int(time.time() * 1000)
        nonce = uuid.uuid4().hex[:6]
        disk_name = f"clip-{ts_ms}-{nonce}{suffix}"

        attachments_dir = FileStorage._project_dir(pid) / "attachments" / sid
        attachments_dir.mkdir(parents=True, exist_ok=True)
        disk_path = attachments_dir / disk_name
        disk_path.write_bytes(file_bytes)

        return web.json_response({
            "path": str(disk_path),
            "mime": mime,
            "size": len(file_bytes),
        })

    async def serve_attachment(self, request: web.Request) -> web.Response:
        """Stream an attachment file back to the browser for display.

        The persisted message history embeds absolute paths in
        ``image_url`` blocks. When the UI renders old chat messages,
        those paths won't load directly from the browser (cross-origin,
        no static handler), so we proxy the bytes through this
        endpoint. The same path-validation rules as
        ``upload_attachment`` apply — we only serve files under
        ``~/.ziva/sessions/<pid>/attachments/`` to avoid an arbitrary
        read primitive.
        """
        from ziva.storage.file_storage import FileStorage

        raw = request.query.get("path", "")
        try:
            candidate = Path(raw).resolve()
        except (OSError, ValueError):
            return web.json_response({"error": "invalid_path"}, status=400)

        # Resolve the session's project from the path itself so attachments
        # keep working after the user switches workspace.
        parts = candidate.parts
        try:
            attachments_idx = parts.index("attachments")
            sid = parts[attachments_idx + 1]
        except (ValueError, IndexError):
            return web.json_response({"error": "invalid_attachment_path"}, status=400)

        pid = self._pid_for(sid)
        root = (FileStorage._project_dir(pid) / "attachments").resolve()

        # Reject anything outside the session's attachments root (no path traversal).
        try:
            candidate.relative_to(root)
        except ValueError:
            return web.json_response({"error": "outside_attachments_dir"}, status=403)
        if not candidate.is_file():
            return web.json_response({"error": "not_found"}, status=404)

        mime, _ = mimetypes.guess_type(candidate.name)
        mime = mime or "application/octet-stream"
        return web.Response(body=candidate.read_bytes(), content_type=mime)

    async def delete_session(self, request: web.Request) -> web.Response:
        sid = request.match_info["sid"]
        # The sidebar lets the user delete sessions from any project, so
        # honor an explicit `workspace` in the request body. Without it
        # we fall back to the active runtime project.
        target_pid = self.runtime.project_id
        try:
            payload = await request.json()
        except Exception:
            payload = {}
        ws = (payload or {}).get("workspace")
        if isinstance(ws, str) and ws:
            try:
                target_pid = _project_hash(Path(ws))
            except Exception:
                pass
        FileStorage.delete_session(target_pid, sid)
        if sid in self.store._loaded_sessions and target_pid == self.runtime.project_id:
            del self.store._loaded_sessions[sid]
        # Pop session state — cancels turn, cleans up MCP client, etc.
        session = self.runtime._sessions.pop(sid, None)
        if session:
            if session.turn_task:
                session.turn_task.cancel()
            if session.mcp_client:
                try:
                    await session.mcp_client.cleanup()
                except Exception:
                    pass
            # Cancel any pending questions
            for fut in list(session.pending_questions.values()):
                if not fut.done():
                    fut.cancel()
        # Clean up EventBus queues and history
        self.runtime.event_bus.unsubscribe_all(sid)
        self.runtime.event_bus.clear_history(sid)
        return web.json_response({"deleted": True})

    async def cancel_turn(self, request: web.Request) -> web.Response:
        sid = request.match_info["sid"]
        # Cancel all pending questions first so await_user_answer returns immediately
        self.runtime.cancel_all_questions(sid)
        session = self.runtime._sessions.get(sid)
        if session:
            if session.cancel_token:
                session.cancel_token.cancel()
            task = session.turn_task
            session.turn_task = None
            if task:
                task.cancel()
        return web.json_response({"cancelled": True})

    async def update_session(self, request: web.Request) -> web.Response:
        sid = request.match_info["sid"]
        payload = await request.json()
        updates = {}
        if "name" in payload:
            updates["name"] = payload["name"]
        if "model_name" in payload:
            updates["model_name"] = payload["model_name"]
        # Allow the sidebar to rename sessions in any project.
        target_pid = self.runtime.project_id
        ws = payload.get("workspace")
        if isinstance(ws, str) and ws:
            try:
                target_pid = _project_hash(Path(ws))
            except Exception:
                pass
        # For the active project we still validate via the in-memory store
        # so a stale UI cannot rename a session that no longer exists; for
        # other projects we trust the file storage directly.
        if target_pid == self.runtime.project_id and not self.store.exists(sid):
            return web.json_response({"error": "session_not_found"}, status=404)
        if updates:
            FileStorage.update_session(target_pid, sid, updates)
        # Mirror model_name onto the in-memory SessionState so the next
        # chat() turn picks it up immediately — don't wait for a
        # disk reload on the next _get_session. Only the active
        # project's sessions live in memory; sessions from other
        # projects will be populated from disk when they're loaded.
        if "model_name" in updates and target_pid == self.runtime.project_id:
            sess = self.runtime._sessions.get(sid)
            if sess is not None:
                sess.model_name = updates["model_name"]
        return web.json_response({"ok": True})

    async def revert_files(self, request: web.Request) -> web.Response:
        sid = request.match_info["sid"]
        if not self.store.exists(sid):
            return web.json_response({"error": "session_not_found"}, status=404)
        payload = await request.json()
        files = payload.get("files", [])
        workspace = self.runtime.workspace_root
        reverted = []
        for f in files:
            try:
                proc = await asyncio.create_subprocess_shell(
                    f"git checkout -- {shlex.quote(f)}",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=str(workspace),
                )
                await proc.communicate()
                reverted.append(f)
            except Exception:
                pass
        return web.json_response({"reverted": reverted})

    async def get_mcp_status(self, _request: web.Request) -> web.Response:
        # Find the first session with MCP connected to report status
        mcp_client = None
        for session in self.runtime._sessions.values():
            if session.mcp_client:
                mcp_client = session.mcp_client
                break
        if not mcp_client:
            return web.json_response({"servers": [], "connected": False, "tools": []})

        servers = []
        for name in mcp_client.connected_servers:
            # Count tools belonging to *this* server only (MCPToolWrapper carries
            # its server name). Previously this summed ALL tools for every server.
            tool_count = sum(1 for t in mcp_client._tools if getattr(t, "_server_name", None) == name)
            servers.append({"name": name, "status": "connected", "tool_count": tool_count})

        mcp_tools = []
        for rec in self.runtime.registry.list_kind("tool"):
            if rec.id.startswith("mcp."):
                spec = rec.instance.spec()
                mcp_tools.append({"name": spec["name"], "description": spec.get("description", "")})

        return web.json_response({
            "servers": servers,
            "connected": True,
            "tools": mcp_tools,
        })

    async def get_config(self, _request: web.Request) -> web.Response:
        model_cfg = self.runtime.config.get("model", {})
        providers = self.runtime.config.get("providers", [])

        available: list[str] = []
        models_list: list[dict] = []
        for p in providers:
            for m in p.get("models", []):
                if m.get("name"):
                    available.append(m["name"])
                    models_list.append(m)
        if not available:
            available = [model_cfg.get("name", "unknown")]

        return web.json_response({
            "model": {
                "current": model_cfg.get("name", "unknown"),
                "available": available,
                "models": models_list,
            },
            "providers": providers,
            "approval": {
                "current": self.runtime.config.get("approval", {}).get("policy", "suggest"),
                "options": ["suggest", "auto-edit", "full-auto"],
            },
            "agents": self.runtime.config.get("agents", {}),
        })

    @property
    def _global_config_path(self) -> Path:
        return Path.home() / ".ziva" / "config.yaml"

    async def update_config(self, request: web.Request) -> web.Response:
        payload = await request.json()
        config_path = self._global_config_path
        import yaml

        # Always base writes on the current disk content.  If a stale server
        # process is still running with an old/default in-memory config, this
        # prevents it from overwriting newer edits made by the user.
        fresh: Dict[str, Any] = {}
        if config_path.exists():
            try:
                loaded = yaml.safe_load(config_path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    fresh = loaded
            except Exception:
                pass
        if not fresh:
            fresh = dict(self.runtime.config)

        if "model" in payload:
            fresh.setdefault("model", {}).update(payload["model"])
            self.runtime.config.setdefault("model", {}).update(payload["model"])
        if "approval" in payload:
            fresh.setdefault("approval", {}).update(payload["approval"])
            self.runtime.config.setdefault("approval", {}).update(payload["approval"])

        # Persist the merged config and keep the runtime in sync.
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(
            yaml.dump(fresh, default_flow_style=False, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )
        self.runtime.config = fresh
        return web.json_response({"ok": True})

    async def get_config_yaml(self, _request: web.Request) -> web.Response:
        """Return the raw YAML config file content."""
        config_path = self._global_config_path
        if not config_path.exists():
            return web.json_response({"yaml": ""})
        return web.json_response({"yaml": config_path.read_text(encoding="utf-8")})

    async def save_config_yaml(self, request: web.Request) -> web.Response:
        """Save raw YAML content to the global config file."""
        payload = await request.json()
        yaml_text = payload.get("yaml", "")
        config_path = self._global_config_path
        config_path.parent.mkdir(parents=True, exist_ok=True)
        import yaml
        try:
            data = yaml.safe_load(yaml_text)
            if data is not None and not isinstance(data, dict):
                return web.json_response({"error": "Config must be a YAML mapping"}, status=400)
        except yaml.YAMLError as e:
            return web.json_response({"error": f"Invalid YAML: {e}"}, status=400)
        config_path.write_text(yaml_text, encoding="utf-8")
        return web.json_response({"ok": True})

    async def get_config_json(self, _request: web.Request) -> web.Response:
        """Return the parsed config as JSON."""
        return web.json_response(self.runtime.config)

    async def save_config_json(self, request: web.Request) -> web.Response:
        """Save a JSON config object, writing it as YAML to the global config.

        Merges the payload onto the current on-disk config so that fields the
        UI doesn't send (or that are missing from a stale in-memory copy) are
        not silently dropped.
        """
        payload = await request.json()
        import yaml
        config_path = self._global_config_path

        disk_config: Dict[str, Any] = {}
        if config_path.exists():
            try:
                loaded = yaml.safe_load(config_path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    disk_config = loaded
            except Exception:
                pass

        merged = _deep_merge(disk_config, payload)
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(yaml.dump(merged, default_flow_style=False, allow_unicode=True, sort_keys=False), encoding="utf-8")
        # Hot-reload the in-memory config
        self.runtime.config = merged
        return web.json_response({"ok": True})
    async def choose_folder(self, _request: web.Request) -> web.Response:
        """Use osascript to open a native folder selection dialog."""
        try:
            proc = await asyncio.create_subprocess_shell(
                "osascript -e 'POSIX path of (choose folder with prompt \"Select Project Folder\")'",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, _ = await proc.communicate()
            if proc.returncode == 0:
                folder_path = stdout.decode("utf-8").strip()
                if folder_path:
                    return web.json_response({"path": folder_path})
            return web.json_response({"error": "No folder selected"}, status=400)
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    async def get_recent_workspaces(self, _request: web.Request) -> web.Response:
        recent_path = Path.home() / ".ziva" / "recent_workspaces.json"
        try:
            if recent_path.exists():
                return web.json_response({"workspaces": json.loads(recent_path.read_text())})
        except Exception:
            pass
        return web.json_response({"workspaces": []})

    async def remove_workspace(self, request: web.Request) -> web.Response:
        """Remove a workspace from the recent list without deleting its data."""
        payload = await request.json()
        path = payload.get("path")
        if not isinstance(path, str) or not path:
            return web.json_response({"error": "Missing path"}, status=400)
        recent_path = Path.home() / ".ziva" / "recent_workspaces.json"
        try:
            workspaces = []
            if recent_path.exists():
                workspaces = json.loads(recent_path.read_text())
            workspaces = [w for w in workspaces if w != path]
            recent_path.parent.mkdir(parents=True, exist_ok=True)
            recent_path.write_text(json.dumps(workspaces))
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)
        return web.json_response({"workspaces": workspaces})

    async def switch_workspace(self, request: web.Request) -> web.Response:
        payload = await request.json()
        new_path = payload.get("path")
        if not new_path:
            return web.json_response({"error": "Missing path"}, status=400)

        target = Path(new_path).resolve()
        if not target.is_dir():
            return web.json_response({"error": "Not a valid directory"}, status=400)

        # Only swap the active workspace identity.
        # Background sessions from other workspaces keep running — their
        # stored project_id routes FileStorage calls correctly.
        self.runtime.workspace_root = target
        self.runtime._project_id = _project_hash(target)

        # Config is global (shared across workspaces). Reload from the global
        # config so external edits are picked up, but do NOT switch to the
        # Config is global (shared across workspaces). Reload from the global
        # config so external edits are picked up. The runtime has a single
        # source of truth — `~/.ziva/config.yaml` — and chat() rebuilds a
        # fresh adapter per turn from the current config, so no per-session
        # cache invalidation is needed here.
        from ziva.config.loader import load_effective_config
        try:
            self.runtime.config = load_effective_config(self._global_config_path)
        except Exception as e:
            # Don't fail the switch if the global config is broken — surface
            # the error and keep the old config so existing sessions don't get
            # torn down mid-flight.
            logger.warning("Failed to reload global config: %s", e)

        # Reload automations for the new workspace
        self.automations.clear()
        for task in list(self._automation_tasks.values()):
            task.cancel()
        self._automation_tasks.clear()
        self._load_persisted_automations()
        self._schedule_enabled_automations()

        # Save to recent workspaces
        recent_path = Path.home() / ".ziva" / "recent_workspaces.json"
        try:
            recent_path.parent.mkdir(parents=True, exist_ok=True)
            workspaces = []
            if recent_path.exists():
                workspaces = json.loads(recent_path.read_text())
            str_target = str(target)
            if str_target in workspaces:
                workspaces.remove(str_target)
            workspaces.insert(0, str_target)
            # keep top 20
            recent_path.write_text(json.dumps(workspaces[:20]))
        except Exception:
            pass

        return web.json_response({"success": True})

    async def get_status(self, _request: web.Request) -> web.Response:
        model = self.runtime.config.get("model", {})
        return web.json_response({
            "model": model.get("name", "unknown"),
            "workspace": str(self.runtime.workspace_root),
            "tools": [t["name"] for t in self.runtime.list_tools()],
            "approval_policy": self.runtime.config.get("approval", {}).get("policy", "suggest"),
            "context_window": int(self.runtime.config.get("memory", {}).get("context_window_tokens", 200000) or 200000),
        })

    async def list_skills(self, _request: web.Request) -> web.Response:
        """Return the list of skills the runtime loaded at startup.

        The runtime scans `skill.extra_paths` (falling back to the
        legacy `mcp.extra_skill_paths`) and the well-known global
        directories `~/.ziva/skills` / `~/.agents/skills` for
        `SKILL.md` files, parsing YAML frontmatter for `name` and
        `description`. The sidebar Skills panel uses this list;
        clicking a skill resolves to `/skills/file?path=<SKILL.md>`
        for the markdown body.
        """
        skill_index = self.runtime.build_skill_index()
        return web.json_response({"skills": skill_index})

    async def read_skill_file(self, request: web.Request) -> web.Response:
        """Read a file from inside one of the configured skill directories.

        The UI navigates relative links inside a `SKILL.md` (e.g.
        `references/snapshot-refs.md`) by re-rooting them at the skill's
        directory. To prevent that endpoint from being abused as a generic
        file reader, the requested path is rejected unless it lives under
        one of the configured `extra_skill_paths` directories.
        """
        raw = request.query.get("path", "")
        if not raw:
            return web.json_response({"error": "path_required"}, status=400)
        target = Path(raw).expanduser().resolve()
        allowed_roots = self._skill_root_paths()
        if not any(self._is_within(target, root) for root in allowed_roots):
            return web.json_response(
                {"error": "path_outside_skill_roots", "path": str(target)},
                status=403,
            )
        if not target.is_file():
            return web.json_response({"error": "not_a_file", "path": str(target)}, status=404)
        try:
            content = target.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            return web.json_response({"error": "read_failed", "message": str(exc)}, status=500)
        return web.json_response({
            "path": str(target),
            "content": content,
            "name": target.stem,
            "size": target.stat().st_size,
        })

    def _skill_root_paths(self) -> List[Path]:
        """Compute the absolute paths of the skill directories the runtime
        scanned, so the file endpoint can confine reads to those roots.
        Reads `skill.extra_paths`."""
        config_paths = self.runtime.config.get("skill", {}).get("extra_paths", [])
        roots: List[Path] = []
        for sp in config_paths:
            p = Path(sp).expanduser().resolve()
            if p.exists():
                roots.append(p)
        return roots

    @staticmethod
    def _is_within(path: Path, root: Path) -> bool:
        try:
            path.relative_to(root)
            return True
        except ValueError:
            return False

    async def start(self, host: str = "127.0.0.1", port: int = 4097) -> None:
        """Start the server (call stop() to shut down)."""
        self._runner = web.AppRunner(self.app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, host=host, port=port)
        await site.start()
        # Kick off STT warmup in a background daemon thread. The first
        # user-driven /api/stt call would otherwise pay a 5+ second cold
        # start (import mlx_whisper + 461MB model load + Metal shader JIT
        # + numba JIT). By starting warmup here, that cost overlaps with
        # the user's normal app-launch idle time instead of being
        # sandwiched between "stop talking" and "see transcribed text".
        # Warmup failure is non-fatal — speech_to_text still works, it
        # just takes a few seconds on the first request.
        self._stt_status = "warming"
        threading.Thread(
            target=self._warmup_stt,
            name="stt-warmup",
            daemon=True,
        ).start()

    def _warmup_stt(self) -> None:
        """Load the STT model in the background so the first user
        transcription is fast. Runs in a daemon thread; exceptions are
        logged but never re-raised.

        Mirrors the model-resolution logic in speech_to_text so we
        warm up the same model the user will hit. If the model isn't
        on disk yet (will be downloaded on first real use), this is a
        no-op — we'd rather not block startup on a multi-GB download.
        """
        try:
            # Resolve the same model path speech_to_text would use.
            models_dir = Path.home() / ".ziva" / "models"
            stt_model = self.runtime.config.get("stt", {}).get(
                "model", "whisper-small-mlx"
            )
            local_path = None
            for candidate in [models_dir / stt_model, models_dir / "mlx-community" / stt_model]:
                if candidate.exists() and (candidate / "weights.npz").exists():
                    local_path = candidate
                    break
            if local_path is None:
                # Model not downloaded yet — let the first real request
                # trigger the download + warmup. That's the only way to
                # know what the user actually wants.
                self._stt_status = "needs_download"
                return

            # Imports here (not at module top) because mlx_whisper pulls
            # in a heavy Metal/native stack that's only meaningful when
            # we're actually going to run STT.
            import imageio_ffmpeg
            ffmpeg_dir = str(Path(imageio_ffmpeg.get_ffmpeg_exe()).parent)
            os.environ["PATH"] = ffmpeg_dir + os.pathsep + os.environ.get("PATH", "")

            import mlx_whisper  # noqa: F401  — import alone warms Metal/native stack
            # Force the model load + decoder warmup by transcribing a
            # 1-second silent wav. This populates ModelHolder.model and
            # JIT-compiles the Metal kernels.
            import wave, tempfile
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
                with wave.open(tmp, "wb") as w:
                    w.setnchannels(1)
                    w.setsampwidth(2)
                    w.setframerate(16000)
                    w.writeframes(b"\x00\x00" * 16000)  # 1s silence
                tmp_path = tmp.name
            try:
                mlx_whisper.transcribe(
                    tmp_path,
                    path_or_hf_repo=str(local_path),
                    language=None,
                )
                self._stt_status = "ready"
            finally:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
        except Exception as exc:
            # Warmup is best-effort. Log so it shows up in backend.log
            # (Electron's main.ts pipes stderr there) but don't crash.
            logger.warning("STT warmup failed (first /api/stt will be slow): %s", exc)
            self._stt_status = "error"

    async def stop(self) -> None:
        """Gracefully stop the server."""
        await self._cancel_automation_tasks()
        try:
            await self.runtime.shutdown()
        except Exception:
            pass
        if self._runner:
            try:
                await self._runner.cleanup()
            except Exception:
                pass

    async def run_async(self, host: str = "127.0.0.1", port: int = 4097) -> None:
        """Start and block until cancelled."""
        await self.start(host=host, port=port)
        try:
            while True:
                await asyncio.sleep(3600)
        except (asyncio.CancelledError, KeyboardInterrupt):
            pass
        finally:
            await self.stop()

    # ---- Panel endpoints ----

    async def files_tree(self, req: web.Request) -> web.Response:
        """Return directory tree for the current workspace.

        Without `path`, scans the workspace root. With `path` (relative to
        the workspace), scans that subtree only — used by the Files tab to
        lazily expand directories deeper than the initial fetch depth, so the
        tree can reach any depth instead of being capped at the request depth.
        Paths in the response are always relative to the workspace root.
        """
        workspace = self.runtime.workspace_root
        if not workspace or not Path(workspace).is_dir():
            return web.json_response({"entries": []})
        depth = min(int(req.query.get("depth", "2")), 5)
        base = Path(workspace).resolve()
        scan_root = base
        sub = (req.query.get("path") or "").strip()
        if sub:
            candidate = (base / sub).resolve()
            try:
                candidate.relative_to(base)
            except ValueError:
                return web.json_response({"error": "Path outside workspace"}, status=403)
            if not candidate.is_dir():
                return web.json_response({"entries": []})
            scan_root = candidate

        def _scan(path: Path, d: int) -> list:
            items: list = []
            try:
                for child in sorted(path.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
                    # Skip hidden dirs and common non-essential dirs
                    if child.name.startswith(".") or child.name in ("node_modules", "__pycache__", ".git", "venv", ".venv", ".tox", ".mypy_cache"):
                        continue
                    entry: Dict[str, Any] = {"name": child.name, "path": str(child.relative_to(base)), "type": "dir" if child.is_dir() else "file"}
                    if child.is_dir() and d > 0:
                        entry["children"] = _scan(child, d - 1)
                    items.append(entry)
            except PermissionError:
                pass
            return items

        entries = _scan(scan_root, depth)
        return web.json_response({"entries": entries})

    async def files_read(self, req: web.Request) -> web.Response:
        """Read a file from the workspace. Supports binary mode for images."""
        rel_path = req.query.get("path", "")
        binary = req.query.get("binary", "")
        workspace = self.runtime.workspace_root
        if not workspace:
            return web.json_response({"error": "No workspace"}, status=400)
        base = Path(workspace).resolve()
        target = (base / rel_path).resolve()
        # Security: ensure target is under workspace. Use relative_to
        # rather than a str startswith check — the latter is bypassable
        # when one path is a string prefix of another (e.g. workspace
        # "/foo" vs target "/foobar").
        try:
            target.relative_to(base)
        except ValueError:
            return web.json_response({"error": "Path outside workspace"}, status=403)
        if not target.is_file():
            return web.json_response({"error": "Not a file"}, status=404)
        try:
            size = target.stat().st_size
            if binary:
                # Return raw binary content with correct mime type
                if size > 10 * 1024 * 1024:  # 10MB for images
                    return web.json_response({"error": "File too large"}, status=413)
                ct, _ = mimetypes.guess_type(str(target))
                return web.Response(body=target.read_bytes(), content_type=ct or "application/octet-stream")
            if size > 2 * 1024 * 1024:  # 2MB limit for text
                return web.json_response({"error": "File too large"}, status=413)
            content = target.read_text(errors="replace")
            return web.json_response({"content": content, "path": rel_path, "size": size})
        except Exception as exc:
            return web.json_response({"error": str(exc)}, status=500)

    async def terminal_ws(self, req: web.Request) -> web.Response:
        """WebSocket endpoint for interactive terminal."""
        ws = web.WebSocketResponse()
        await ws.prepare(req)

        import termios as _termios_mod

        workspace = self.runtime.workspace_root or str(Path.home())
        master_fd, slave_fd = pty.openpty()

        proc = subprocess.Popen(
            [os.environ.get("SHELL", "/bin/bash")],
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            cwd=workspace,
            env={**os.environ, "TERM": "xterm-256color"},
            close_fds=True,
        )
        os.close(slave_fd)

        async def _read_pty() -> None:
            loop = asyncio.get_event_loop()
            reader = asyncio.StreamReader()
            transport, _ = await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), os.fdopen(master_fd, "rb"))
            try:
                while True:
                    data = await reader.read(4096)
                    if not data:
                        break
                    try:
                        await ws.send_json({"type": "output", "data": data.decode(errors="replace")})
                    except Exception:
                        break
            except Exception:
                pass
            finally:
                transport.close()

        async def _write_pty() -> None:
            try:
                async for msg in ws:
                    if msg.type == web.WSMsgType.TEXT:
                        try:
                            data = json.loads(msg.data)
                            if data.get("type") == "input":
                                os.write(master_fd, data["data"].encode())
                            elif data.get("type") == "resize":
                                cols = max(1, int(data.get("cols", 80)))
                                rows = max(1, int(data.get("rows", 24)))
                                winsize = struct.pack("HHHH", rows, cols, 0, 0)
                                fcntl.ioctl(master_fd, _termios_mod.TIOCSWINSZ, winsize)
                        except Exception:
                            pass
                    elif msg.type in (web.WSMsgType.ERROR, web.WSMsgType.CLOSE):
                        break
            except Exception:
                pass

        reader_task = asyncio.create_task(_read_pty())
        writer_task = asyncio.create_task(_write_pty())

        try:
            await asyncio.gather(reader_task, writer_task, return_exceptions=True)
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
            try:
                os.close(master_fd)
            except OSError:
                pass
            reader_task.cancel()
            writer_task.cancel()

        return ws

    async def proxy_url(self, req: web.Request) -> web.Response:
        """Proxy a URL for the browser panel (fallback for non-Electron)."""
        import aiohttp
        url = req.query.get("url", "")
        if not url:
            return web.json_response({"error": "Missing url parameter"}, status=400)
        if not url.startswith(("http://", "https://")):
            return web.json_response({"error": "Only http/https allowed"}, status=400)
        try:
            headers = {
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
                "Accept-Encoding": "identity",
            }
            async with aiohttp.ClientSession() as session:
                async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=15), allow_redirects=True) as resp:
                    body = await resp.read()
                    ct = resp.headers.get("Content-Type", "text/html")
                    # Inject base tag to fix relative URLs in proxied HTML
                    if "text/html" in ct:
                        html = body.decode("utf-8", errors="replace")
                        import html as html_mod
                        safe_url = html_mod.escape(url)
                        html = html.replace("<head>", f'<head><base href="{safe_url}">')
                        body = html.encode("utf-8")
                    return web.Response(body=body, content_type=ct.split(";")[0])
        except Exception as exc:
            return web.json_response({"error": str(exc)}, status=502)

    async def stt_status(self, request: web.Request) -> web.Response:
        """Lightweight probe so the frontend can render a "preparing voice
        input…" hint while the STT model is loading. Status values:
          - idle             : warmup hasn't been kicked off yet
          - warming         : background thread is loading the model
          - ready           : model loaded, transcribe() will be fast
          - needs_download  : model not on disk yet; first request will
                              download + warm up (~minutes for 461 MB)
          - error           : warmup raised; check backend.log
        """
        return web.json_response({"status": self._stt_status})

    async def speech_to_text(self, request: web.Request) -> web.Response:
        """Transcribe audio using mlx-whisper (Apple Silicon GPU-accelerated)."""
        import tempfile
        try:
            # Parse multipart form data to extract audio blob
            audio_data = None
            ext = ".wav"
            reader = await request.multipart()
            while True:
                part = await reader.next()
                if part is None:
                    break
                if part.name == "audio":
                    audio_data = await part.read()
                    # Detect format from part headers
                    ct = (part.headers.get("Content-Type") or "").lower()
                    if "webm" in ct:
                        ext = ".webm"
                    elif "mp4" in ct or "mpeg" in ct:
                        ext = ".mp4"
                    elif "ogg" in ct:
                        ext = ".ogg"
                    elif "wav" in ct:
                        ext = ".wav"
                    break

            if not audio_data:
                return web.json_response({"error": "No audio data"}, status=400)

            with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp:
                tmp.write(audio_data)
                tmp_path = tmp.name

            try:
                # mlx_whisper.audio.load_audio() shells out to `ffmpeg` to
                # decode the temp file. In the PyInstaller bundle the
                # runtime PATH is /usr/bin:/bin:/usr/sbin:/sbin only —
                # homebrew ffmpeg at /opt/homebrew/bin/ffmpeg isn't
                # visible. imageio-ffmpeg ships a static ffmpeg binary
                # inside its wheel, which PyInstaller's hook bundles into
                # the executable; we prepend its directory to PATH so the
                # subprocess picks it up. We do this even for .wav files
                # because mlx_whisper normalises the audio through ffmpeg
                # regardless of input format (resampling + channel layout).
                import imageio_ffmpeg
                import mlx_whisper
                ffmpeg_dir = str(Path(imageio_ffmpeg.get_ffmpeg_exe()).parent)
                os.environ["PATH"] = ffmpeg_dir + os.pathsep + os.environ.get("PATH", "")

                models_dir = Path.home() / ".ziva" / "models"
                models_dir.mkdir(parents=True, exist_ok=True)
                stt_model = self.runtime.config.get("stt", {}).get(
                    "model", "whisper-small-mlx"
                )
                # Check local models_dir first, fall back to HF repo
                local_path = None
                for candidate in [models_dir / stt_model, models_dir / "mlx-community" / stt_model]:
                    if candidate.exists() and (candidate / "weights.npz").exists():
                        local_path = candidate
                        break
                model_ref = str(local_path) if local_path else f"mlx-community/{stt_model}"
                loop = asyncio.get_event_loop()
                result = await loop.run_in_executor(
                    None,
                    lambda: mlx_whisper.transcribe(
                        tmp_path,
                        path_or_hf_repo=model_ref,
                        language=None,
                    ),
                )
                text = result.get("text", "").strip()
                return web.json_response({"text": text})
            finally:
                os.unlink(tmp_path)
        except Exception as exc:
            return web.json_response({"error": str(exc)}, status=500)

    async def list_background_agents(self, request: web.Request) -> web.Response:
        # Whitelist serializable fields. The stored dict includes the asyncio
        # Task under "task", which web.json_response cannot serialize — that
        # made this endpoint 500 with a non-JSON body.
        safe_keys = ("agent_id", "call_id", "session_id", "task_desc",
                     "status", "result", "error", "tools_used", "finished_at")
        agents = [{k: a.get(k) for k in safe_keys} for a in self.runtime._background_agents.values()]
        return web.json_response({"agents": agents})

    async def get_background_agent(self, request: web.Request) -> web.Response:
        agent_id = request.match_info["agent_id"]
        agent = self.runtime._background_agents.get(agent_id)
        if not agent:
            return web.json_response({"error": "not_found"}, status=404)
        # Whitelist serializable fields (same reason as list_background_agents:
        # the stored dict holds an asyncio Task under "task" which json_response
        # cannot serialize → would 500 with a non-JSON body).
        safe_keys = ("agent_id", "call_id", "session_id", "task_desc",
                     "status", "result", "error", "tools_used", "finished_at")
        safe = {k: agent.get(k) for k in safe_keys}
        return web.json_response({"agent": safe})

    async def cancel_background_agent(self, request: web.Request) -> web.Response:
        agent_id = request.match_info["agent_id"]
        agent = self.runtime._background_agents.get(agent_id)
        if not agent:
            return web.json_response({"error": "not_found"}, status=404)
        if agent["status"] != "running":
            return web.json_response({"error": "not_running", "status": agent["status"]}, status=409)
        agent["status"] = "cancelled"
        agent["error"] = "Cancelled by user"
        if self.runtime.event_bus:
            await self.runtime.event_bus.publish(agent["session_id"], {
                "type": "subagent_end",
                "agent_id": agent_id,
                "call_id": agent.get("call_id"),
                "status": "cancelled",
                "background": True,
            })
        return web.json_response({"ok": True, "agent_id": agent_id})
