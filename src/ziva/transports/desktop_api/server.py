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
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List

from aiohttp import web

from ziva.config.loader import _deep_merge
from ziva.runtime import Runtime
from ziva.scheduled import (
    ScheduleError,
    compute_next_run,
    describe_schedule,
    normalize_schedule,
)
from ziva.shared_types import CancellationToken, ChatMessage, ChatResult, ToolCallItem
from ziva.storage.file_storage import FileStorage, _project_hash
from ziva.transports.im_bridge import IMBridge


logger = logging.getLogger(__name__)

# Extensions the /attachments proxy will serve for in-browser preview:
# images, media (video/audio), pdf/html, and common text/code. Deliberately
# excludes archives (zip/tar/...), office docs (doc/xls/ppt), binaries, and
# config/secret files (yaml/env/ini) so this stays a preview surface, not a
# generic arbitrary-file reader.
_PREVIEWABLE_EXTS = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg", ".ico",
    ".mp4", ".webm", ".mov", ".m4v", ".ogv", ".mkv", ".avi",
    ".mp3", ".wav", ".ogg", ".m4a", ".aac", ".flac", ".opus",
    ".pdf", ".html", ".htm",
    ".txt", ".md", ".markdown", ".log", ".csv", ".json", ".xml",
    ".py", ".js", ".ts", ".tsx", ".jsx", ".css", ".scss",
    ".sh", ".bash", ".rs", ".go", ".java", ".kt",
    ".c", ".cpp", ".cc", ".h", ".hpp", ".rb", ".php", ".swift", ".sql",
}

# Text/code extensions to ALWAYS serve as text/plain so the browser displays
# them inline instead of downloading. mimetypes gets several of these wrong
# (.md/.go → None → octet-stream → download; .ts → video/mp2t; .sh → x-sh).
_TEXT_PREVIEW_EXTS = {
    ".txt", ".md", ".markdown", ".log",
    ".py", ".js", ".ts", ".tsx", ".jsx", ".css", ".scss",
    ".sh", ".bash", ".rs", ".go", ".java", ".kt",
    ".c", ".cpp", ".cc", ".h", ".hpp", ".rb", ".php", ".swift", ".sql",
}


@dataclass
class Automation:
    id: str
    name: str
    prompt: str
    enabled: bool = True
    last_run: float | None = None
    last_result: str | None = None
    last_error: str | None = None
    next_run: float | None = None
    run_count: int = 0
    # History of recent runs, newest first. Each: {id, ts, prompt, result,
    # error, status}. Capped to bound storage. Shown as cards in the
    # automation detail view — click a card to see input + output.
    runs: list = field(default_factory=list)
    # Schedule spec — see src/ziva/scheduled.py.
    schedule: dict = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    # Deprecated: kept as an optional field only for backward compat
    # with previously persisted automation records that were created
    # with a long-lived backing session. New automations never set
    # this; _run_automation_once allocates a fresh ephemeral sid per
    # run to keep histories isolated.
    session_id: str = ""

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Automation":
        sched_raw = data.get("schedule")
        if not isinstance(sched_raw, dict):
            raise ScheduleError("persisted record is missing `schedule` object")
        try:
            schedule = normalize_schedule(sched_raw)
        except ScheduleError as exc:
            raise ScheduleError(
                f"persisted record has invalid schedule: {exc}"
            ) from exc
        return cls(
            id=str(data.get("id") or uuid.uuid4()),
            name=str(data.get("name") or "unnamed"),
            prompt=str(data.get("prompt") or ""),
            enabled=bool(data.get("enabled", True)),
            last_run=data.get("last_run"),
            last_result=data.get("last_result"),
            last_error=data.get("last_error"),
            next_run=data.get("next_run"),
            run_count=int(data.get("run_count") or 0),
            runs=list(data.get("runs") or []),
            schedule=schedule,
            created_at=float(data.get("created_at") or time.time()),
            updated_at=float(data.get("updated_at") or time.time()),
            session_id=str(data.get("session_id") or ""),
        )

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        # Drop the empty deprecated session_id from the response so
        # callers don't see a stale "" value lying around.
        if not d.get("session_id"):
            d.pop("session_id", None)
        return d


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
            # Record the workspace this session was created in so tools
            # resolve cwd/relative paths against it even after the user
            # switches to another workspace. See SessionState.workspace_root.
            "workspace_root": str(self.runtime.workspace_root),
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


# --- Module-level helpers shared by HTTP routes + IM bridge slash commands. ---
# Both code paths need the exact same persistence + cache refresh semantics,
# so the implementation lives here once and is imported by both. Kept free
# functions (not methods) so the IM bridge doesn't need to hold a back-
# reference to its server instance.


def _chat_message_from_record(msg_data: dict) -> "ChatMessage":
    msg = ChatMessage(
        role=msg_data.get("role", "user"),
        content=msg_data.get("content", ""),
        tool_call_id=msg_data.get("tool_call_id"),
        name=msg_data.get("name"),
        tool_calls=[
            ToolCallItem(id=tc.get("id", ""), name=tc.get("name", ""), arguments=tc.get("arguments", {}))
            for tc in msg_data.get("tool_calls", [])
        ],
        _compaction_summary=msg_data.get("_compaction_summary", False),
        _hidden=msg_data.get("_hidden", False),
    )
    # Restore reasoning fields so compact/reload preserves the thinking
    # card. Without this, any read-modify-write cycle (compact, prune,
    # rewind) silently erases reasoning_content from disk because the
    # loaded ChatMessage has None, and persist_message_set only writes
    # the field when it's truthy.
    rc = msg_data.get("reasoning_content")
    if rc:
        msg.reasoning_content = rc
    rs = msg_data.get("reasoning_signature")
    if rs:
        msg.reasoning_signature = rs
    return msg


def load_session_messages(sid: str, pid: str) -> List["ChatMessage"]:
    """Read messages from disk as ChatMessage objects."""
    messages: List["ChatMessage"] = []
    for msg_data in FileStorage.get_messages(pid, sid):
        messages.append(_chat_message_from_record(msg_data))
    return messages


def _first_user_text(chat_messages: List["ChatMessage"]) -> str:
    """Extract a sidebar title from the first user message.

    Returns the truncated (60-char) text of the first user-role message,
    or "[图片]" if the user only sent image parts, or "" if no user message
    exists at all (defensive — create_turn always sends one, but the helper
    shouldn't crash on weird payloads).
    """
    for m in chat_messages:
        if m.role != "user":
            continue
        content = m.content
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            text = " ".join(
                (p or {}).get("text", "")
                for p in content
                if isinstance(p, dict) and p.get("type") == "text"
            ).strip()
        else:
            text = ""
        text = " ".join(text.split())  # collapse whitespace/newlines
        if text:
            return text[:60].rstrip() + ("…" if len(text) > 60 else "")
        return "[图片]"  # user message exists but has no text part
    return ""


def persist_message_set(
    sid: str,
    working_set: List["ChatMessage"],
    pid: str,
    runtime: Any,
    store: Any,
) -> Dict[str, Any]:
    """Rewrite a session's message list to disk + sync every in-memory cache
    + refresh last_usage. Generic helper used by compact / prune / rewind +
    the IM bridge's `/compact` slash command — anything that replaces the
    whole on-disk history in one shot.

    `last_usage.prompt_tokens` is computed from `_llm_context(working_set)`
    — only the last summary + messages after it, which is the actual cost
    of the next turn.
    """
    records: list[dict] = []
    for m in working_set:
        record: dict = {
            "role": m.role,
            "content": m.content,
            "tool_call_id": m.tool_call_id,
            "name": m.name,
            "tool_calls": [
                {"id": tc.id, "name": tc.name, "arguments": tc.arguments} for tc in m.tool_calls
            ],
        }
        if m.reasoning_content:
            record["reasoning_content"] = m.reasoning_content
        if m.reasoning_signature:
            record["reasoning_signature"] = m.reasoning_signature
        if m._compaction_summary:
            record["_compaction_summary"] = True
        if m._hidden:
            record["_hidden"] = True
        records.append(record)

    FileStorage.replace_messages(pid, sid, records)

    # Sync the SessionStore in-memory cache so the next getMessages returns
    # the rewritten history without a disk re-read.
    session = store._ensure_loaded(sid)
    session["messages"] = list(records)

    # Sync the Runtime session history cache so the next chat() picks up the
    # rewritten history without re-loading from disk.
    if sid in runtime._sessions:
        from ziva.session.compaction import _llm_context
        runtime._sessions[sid].history = _llm_context(working_set)

    from ziva.session.compaction import estimate_tokens, _llm_context
    llm_visible = _llm_context(working_set)
    new_prompt_tokens = estimate_tokens(llm_visible)
    new_usage = {"prompt_tokens": new_prompt_tokens, "completion_tokens": 0, "total_tokens": new_prompt_tokens}
    FileStorage.update_session(pid, sid, {"last_usage": new_usage})
    return new_usage


class DesktopAPIServer:
    def __init__(self, runtime: Runtime) -> None:
        self.runtime = runtime
        self.runtime.automation_callback = self._reload_automations
        # Expose a programmatic "run now" hook on the runtime so the
        # manage_scheduled_tasks tool's `action: "run"` can trigger an
        # automation without going through the HTTP layer.
        self.runtime.trigger_automation_now = self.trigger_automation_now
        self.store = SessionStore(runtime=runtime)
        # IM bridge: in-process component that turns inbound IM messages into
        # ordinary sessions (same chat_with_events path as the desktop
        # composer). See docs/im-bridge.md.
        self._im_bridge = IMBridge(self.runtime, self.store)
        self.automations: Dict[str, Automation] = {}
        self._automation_tasks: Dict[str, asyncio.Task] = {}
        self._runner: web.AppRunner | None = None
        # STT warmup status is owned by ``stt_warmup`` module (started
        # even before this server exists). The frontend polls
        # /api/stt/status to render a "preparing voice input…" hint
        # while the model is loading.
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
        self.app.router.add_post("/sessions/{sid}/rewind", self.rewind_session)
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
        # IM bridge (in-process; see transports/im_bridge/).
        self.app.router.add_get("/api/im/channels", self.list_im_channels)
        self.app.router.add_post("/api/im/channels/{name}/start", self.start_im_channel)
        self.app.router.add_post("/api/im/channels/{name}/stop", self.stop_im_channel)
        self.app.router.add_get("/api/im/channels/{name}/status", self.im_channel_status)
        self.app.router.add_get("/api/im/config", self.get_im_config)
        self.app.router.add_put("/api/im/config", self.update_im_config)
        self.app.router.add_get("/api/im/pending-senders", self.list_pending_senders)
        self.app.router.add_post("/api/im/pending-senders/{sender_id}/approve", self.approve_pending_sender)
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
        await self._im_bridge.start()

    async def _on_cleanup(self, _app: web.Application) -> None:
        await self._cancel_automation_tasks()
        await self._im_bridge.stop()

    # ---- IM bridge ----

    async def list_im_channels(self, _request: web.Request) -> web.Response:
        return web.json_response({"channels": self._im_bridge.list_channels()})

    async def start_im_channel(self, request: web.Request) -> web.Response:
        name = request.match_info["name"]
        try:
            payload = await request.json()
        except Exception:
            payload = {}
        result = await self._im_bridge.start_channel(name, payload or {})
        if isinstance(result, dict) and result.get("error"):
            return web.json_response(result, status=400)
        return web.json_response(result)

    async def stop_im_channel(self, request: web.Request) -> web.Response:
        name = request.match_info["name"]
        return web.json_response(await self._im_bridge.stop_channel(name))

    async def im_channel_status(self, request: web.Request) -> web.Response:
        return web.json_response(self._im_bridge.channel_status(request.match_info["name"]))

    async def get_im_config(self, _request: web.Request) -> web.Response:
        return web.json_response(self._im_bridge.get_config())

    async def update_im_config(self, request: web.Request) -> web.Response:
        try:
            payload = await request.json()
        except Exception:
            payload = {}
        return web.json_response(self._im_bridge.update_config(payload or {}))

    async def list_pending_senders(self, _request: web.Request) -> web.Response:
        return web.json_response({"senders": self._im_bridge.get_pending_senders()})

    async def approve_pending_sender(self, request: web.Request) -> web.Response:
        sender_id = request.match_info["sender_id"]
        if self._im_bridge.approve_sender(sender_id):
            return web.json_response({"ok": True})
        return web.json_response({"error": "already_allowed_or_unknown"}, status=400)

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
            try:
                automation = Automation.from_dict(item)
            except ScheduleError:
                # Unparseable records are simply skipped. We never
                # touch the on-disk file — the user can clean up by
                # hand if they care.
                continue
            if not automation.next_run and automation.enabled:
                next_run = compute_next_run(automation.schedule, time.time())
                if next_run is None:
                    next_run = time.time() + 60
                automation.next_run = next_run
                self._persist_automation(automation)
            self.automations[automation.id] = automation

    def _persist_automation(self, automation: Automation) -> None:
        automation.updated_at = time.time()
        FileStorage.upsert_automation(self.runtime.project_id, automation.to_dict())

    def _automation_payload(self, automation: Automation) -> Dict[str, Any]:
        return automation.to_dict()

    def _next_run_timestamp(self, schedule: dict) -> float | None:
        """Compute the next run timestamp from a (canonical) schedule dict.

        Thin wrapper over :func:`ziva.scheduled.compute_next_run` so
        callers don't need to know about the schedule module.
        """
        return compute_next_run(schedule, time.time())

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
                next_run = self._next_run_timestamp(automation.schedule)
                automation.next_run = next_run if next_run is not None else now + 60
                self._persist_automation(automation)
            await asyncio.sleep(max(0, automation.next_run - now))
            automation = self.automations.get(automation_id)
            if not automation or not automation.enabled:
                return
            await self._run_automation_once(automation, scheduled=True)

    async def _run_automation_once(self, automation: Automation, *, scheduled: bool, session_id: str | None = None) -> ChatResult | None:
        # Resolution order for the run target sid:
        #   1. caller-supplied `session_id` — the frontend can opt to run
        #      "Run now" inside the user's currently-active chat so the
        #      stream shows up there (streaming is NOT suppressed for
        #      such sessions because they're ordinary user sessions).
        #   2. a brand-new ephemeral sid created for this run only.
        #
        # We deliberately do NOT reuse automation.session_id across runs:
        # chat() loads the session's history from disk every time, so a
        # shared backing session would silently accumulate every prior
        # run's prompt + response + tool calls as context for the next
        # run — classic cross-run pollution that grows unbounded until
        # the model starts tripping its context window. A fresh sid
        # per run gives every run an empty history. The session is
        # still marked `is_automation: True` so the sidebar hides it
        # and runtime._emit() suppresses its streaming events.
        if session_id:
            sid = session_id
            in_user_session = True
        else:
            sid = self.store.create()
            in_user_session = False
            FileStorage.update_session(self.runtime.project_id, sid, {"is_automation": True})
        try:
            # Mark the in-memory session as an automation backing session
            # so runtime._emit() suppresses streaming events for it.
            # Skipped for caller-supplied user sessions — those should
            # stream normally into the user's active chat.
            backing_sess = self.runtime._get_session(sid)
            if not in_user_session:
                backing_sess.is_automation = True
            messages = [ChatMessage(role="user", content=automation.prompt)]
            result = await self.runtime.chat(messages, session_id=sid)
            automation.last_run = time.time()
            automation.last_result = result.content
            automation.last_error = None
            automation.run_count += 1
            automation.runs.insert(0, {
                "id": str(uuid.uuid4()),
                "ts": automation.last_run,
                "sid": sid,
                "prompt": automation.prompt,
                "result": result.content,
                "error": None,
                "status": "done",
            })
            automation.runs = automation.runs[:50]
            next_run = self._next_run_timestamp(automation.schedule) if automation.enabled else None
            automation.next_run = next_run if next_run is not None else None
            self._persist_automation(automation)
            await self.runtime._emit(sid, {
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
            automation.runs.insert(0, {
                "id": str(uuid.uuid4()),
                "ts": automation.last_run,
                "sid": sid,
                "prompt": automation.prompt,
                "result": None,
                "error": str(exc),
                "status": "failed",
            })
            automation.runs = automation.runs[:50]
            next_run = self._next_run_timestamp(automation.schedule) if automation.enabled else None
            automation.next_run = next_run if next_run is not None else None
            self._persist_automation(automation)
            await self.runtime._emit(sid, {
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
        updates: dict[str, Any] = {
            # Persist the workspace where the session was created so that
            # a session created in workspace A stays bound to A even if the
            # user switches to B before the first turn is sent.
            "workspace_root": str(self.runtime.workspace_root),
        }
        if model_name is not None:
            updates["model_name"] = model_name
        FileStorage.update_session(self.runtime.project_id, sid, updates)
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

    def _register_current_workspace(self) -> None:
        """Record the current workspace in the recent list so other backends
        and future app launches can discover its sessions."""
        current = str(self.runtime.workspace_root)
        recent_path = Path.home() / ".ziva" / "recent_workspaces.json"
        try:
            recent_path.parent.mkdir(parents=True, exist_ok=True)
            workspaces = []
            if recent_path.exists():
                workspaces = json.loads(recent_path.read_text())
            if current in workspaces:
                workspaces.remove(current)
            workspaces.insert(0, current)
            recent_path.write_text(json.dumps(workspaces[:20]))
        except Exception:
            pass

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
                    # Hide automation backing sessions from the sidebar —
                    # scheduled runs happen in the background and surface
                    # only their result in the Automations UI.
                    if s.get("is_automation"):
                        continue
                    items.append({
                        "id": s["id"],
                        "time": s.get("time"),
                        "workspace": ws,
                        "name": s.get("name"),
                        "model_name": s.get("model_name"),
                        "channel": s.get("channel"),
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

        # Defensive: if the in-memory store has no turn record but the runtime
        # session still has a live task (e.g. an IM-driven turn started
        # before this server process loaded the session), synthesize a
        # running turn so the desktop UI can show a stop button and query
        # events. The synthetic record is transient — it only exists for
        # this HTTP response.
        if not turns:
            rt_session = self.runtime._sessions.get(sid)
            if rt_session and rt_session.turn_task is not None and not rt_session.turn_task.done():
                turns.append({
                    "id": "synthetic-running",
                    "status": "running",
                    "events": self.runtime.event_bus.history(sid),
                    "result": None,
                })

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
                "workspace_root": str(self.runtime.workspace_root),
            })

        # Reject if a turn is already in-flight for this session.
        rt_session = self.runtime._sessions.get(sid)
        if rt_session and rt_session.turn_task is not None and not rt_session.turn_task.done():
            return web.json_response({"error": "turn_already_running"}, status=429)

        payload = await request.json()
        messages = payload.get("messages") or []
        chat_messages = [ChatMessage(role=str(m.get("role", "user")), content=m.get("content", "")) for m in messages]

        # Stamp a human-readable session name on the first user turn so the
        # sidebar can show a meaningful title without per-session enrichment.
        # Only written when the session has no name yet — later turns don't
        # overwrite a user-renamed title, matching the IM bridge's
        # ``_ensure_session`` behaviour. Sessions created via the desktop
        # composer start with no name; the first turn's text becomes the
        # title. Pure-image turns (no text parts) fall back to "[图片]".
        try:
            existing = FileStorage.get_session(self.runtime.project_id, sid) or {}
        except Exception:
            existing = {}
        if not existing.get("name"):
            fallback = _first_user_text(chat_messages)
            if fallback:
                FileStorage.update_session(self.runtime.project_id, sid, {"name": fallback})

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

    def _persist_message_set(self, sid: str, working_set: List[ChatMessage]) -> Dict[str, Any]:
        """Rewrite a session's message list to disk + sync every in-memory
        cache + refresh last_usage. The generic "persist exactly this message
        set" used by compact / prune / rewind — anything that replaces the
        whole on-disk history in one shot.

        Thin wrapper over the module-level helper so IM bridge / tests /
        other callers can reuse the same logic without owning a server.
        """
        return persist_message_set(
            sid=sid,
            working_set=working_set,
            pid=self._pid_for(sid),
            runtime=self.runtime,
            store=self.store,
        )

    def _load_session_messages(self, sid: str) -> List[ChatMessage]:
        """Read messages from disk as ChatMessage objects."""
        return load_session_messages(sid, self._pid_for(sid))

    def _append_summary_to_disk(self, sid: str, summary: ChatMessage) -> None:
        """Append a single compaction-summary record to the session's message file.

        Kept for back-compat with external callers, but /compact now uses
        `_persist_message_set` to slot the summary between the older and
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

        new_usage = self._persist_message_set(sid, pruned)
        return web.json_response({
            "success": True,
            "message_count": len(pruned),
            "last_usage": new_usage,
        })

    async def rewind_session(self, request: web.Request) -> web.Response:
        """POST /sessions/{sid}/rewind {up_to_index} — Claude Code-style rewind.

        Two target kinds:
        - ``role="user"``: delete that user message AND everything after it,
          return the user's original text + image attachment paths so the UI
          drops them back into the composer for editing/resend. Stop, no
          model run.
        - ``role="tool"``: snap to the END of that tool-call group (keep the
          tool result + any sibling parallel tool results from the same
          assistant tool_calls), delete everything after. Lands on a complete,
          legally-paired boundary; stop and wait for the user. No composer
          restore.

        Refuses with 409 if a turn is currently running on this session.
        """
        sid = request.match_info["sid"]
        if not self.store.exists(sid):
            return web.json_response({"error": "session_not_found"}, status=404)
        rt_session = self.runtime._sessions.get(sid)
        if rt_session and rt_session.turn_task is not None and not rt_session.turn_task.done():
            return web.json_response({"error": "turn_running"}, status=409)

        payload = await request.json()
        up_to_index = payload.get("up_to_index")
        if not isinstance(up_to_index, int) or up_to_index < 0:
            return web.json_response({"error": "invalid up_to_index"}, status=400)

        messages = self._load_session_messages(sid)
        if up_to_index >= len(messages):
            return web.json_response({"error": "up_to_index out of range"}, status=400)
        target = messages[up_to_index]
        if target.role not in ("user", "tool"):
            return web.json_response({"error": "rewind target must be a user or tool message"}, status=400)

        # Compute keep_count and extract what to restore to the composer.
        removed_text = ""
        removed_images: List[str] = []
        if target.role == "user":
            # User rewind: delete this user AND everything after, restore its
            # original text + image attachment paths into the composer.
            keep_count = up_to_index
            content = target.content
            if isinstance(content, str):
                removed_text = content
            elif isinstance(content, list):
                removed_text = "\n".join(
                    p.get("text", "")
                    for p in content
                    if isinstance(p, dict) and p.get("type") == "text" and p.get("text")
                )
                for p in content:
                    if isinstance(p, dict) and p.get("type") == "image_url":
                        iu = p.get("image_url")
                        url = iu.get("url") if isinstance(iu, dict) else iu
                        if isinstance(url, str) and url:
                            removed_images.append(url)
            else:
                removed_text = str(content)
        else:
            # Tool rewind: the user clicked a tool card. Snap to the END of
            # its tool-call group — keep this tool result AND any sibling
            # parallel tool results from the same assistant tool_calls, so we
            # land on a complete, legally-paired boundary. Then stop and wait
            # for the user (no composer restore).
            keep_count = up_to_index
            while keep_count < len(messages):
                m = messages[keep_count]
                # Keep tool results AND any _hidden user image messages the
                # runtime attaches right after (read_file on an image emits
                # tool_result + a hidden user image carrying the data URL,
                # tagged with the tool_call_id). Without this, rewinding a
                # read_file image tool card would delete its image. Stopping
                # at the first plain user/assistant message lands on a
                # complete, legally-paired boundary.
                if m.role == "tool" or (m.role == "user" and getattr(m, "_hidden", False)):
                    keep_count += 1
                else:
                    break

        kept = messages[:keep_count]
        self._persist_message_set(sid, kept)
        return web.json_response({
            "rewound": True,
            "kind": target.role,
            "removed_count": len(messages) - keep_count,
            "removed_user_content": removed_text,
            "removed_user_images": removed_images,
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
        new_usage = self._persist_message_set(sid, on_disk)
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
        if not self.store.exists(sid):
            return web.json_response({"error": "session_not_found"}, status=404)

        # Read the persisted plan directly from disk. The in-memory SessionStore
        # entry is rebuilt from messages only and drops session-level metadata
        # like `plan`, so relying on it returns stale/empty results.
        pid = self._pid_for(sid)
        disk_session = FileStorage.get_session(pid, sid) or {}
        plan_steps = disk_session.get("plan") or []

        # Fallback: a plan may exist only in the runtime SessionState if it was
        # just set but the disk write hasn't happened yet (unlikely, but cheap).
        if not plan_steps:
            rt_session = self.runtime._sessions.get(sid)
            if rt_session:
                plan_steps = rt_session.plan or []

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

        # Schedule is required and must be the discriminated-union shape.
        schedule_raw = payload.get("schedule")
        if not isinstance(schedule_raw, dict):
            return web.json_response(
                {"error": "invalid_schedule", "detail": "request body must include `schedule` (object)"},
                status=400,
            )
        try:
            schedule = normalize_schedule(schedule_raw)
        except ScheduleError as exc:
            return web.json_response(
                {"error": "invalid_schedule", "detail": str(exc)}, status=400
            )

        # Anchor "every" tasks at create time so the first run aligns
        # to the grid regardless of when the create request lands.
        if schedule["kind"] == "every" and "anchor_at" not in schedule:
            schedule["anchor_at"] = int(time.time())

        run_immediately = bool(payload.get("run_immediately", False))

        if not prompt:
            return web.json_response({"error": "prompt is required"}, status=400)

        aid = str(uuid.uuid4())
        now = time.time()
        if run_immediately:
            next_run = now
        else:
            computed = self._next_run_timestamp(schedule)
            next_run = computed if computed is not None else now + 60
        # No backing session is created up-front: each run gets its own
        # ephemeral sid inside _run_automation_once to avoid cross-run
        # context pollution. session_id is kept as an optional field
        # on Automation for backward compat with previously persisted
        # records (older builds reserved one session per automation).
        automation = Automation(
            id=aid,
            name=name,
            prompt=prompt,
            session_id="",  # deprecated; see _run_automation_once
            schedule=schedule,
            next_run=next_run,
        )
        self.automations[aid] = automation
        self._persist_automation(automation)
        self._schedule_automation(automation)
        return web.json_response({"id": aid, "automation": self._automation_payload(automation)})

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

        # Schedule update must be the discriminated-union shape.
        if "schedule" in payload:
            if not isinstance(payload["schedule"], dict):
                return web.json_response(
                    {"error": "invalid_schedule", "detail": "`schedule` must be an object"},
                    status=400,
                )
            try:
                automation.schedule = normalize_schedule(payload["schedule"])
            except ScheduleError as exc:
                return web.json_response(
                    {"error": "invalid_schedule", "detail": str(exc)}, status=400
                )
            reschedule = True
        elif "interval_seconds" in payload or "schedule_time" in payload:
            return web.json_response(
                {
                    "error": "invalid_schedule",
                    "detail": "send `schedule: {kind, ...}` instead",
                },
                status=400,
            )

        if "enabled" in payload:
            enabled = bool(payload["enabled"])
            if automation.enabled != enabled:
                automation.enabled = enabled
                if enabled:
                    computed = self._next_run_timestamp(automation.schedule)
                    automation.next_run = (
                        computed if computed is not None else time.time() + 60
                    )
                else:
                    automation.next_run = None
                reschedule = True

        if reschedule and automation.enabled:
            computed = self._next_run_timestamp(automation.schedule)
            if computed is not None:
                automation.next_run = computed

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

        # Optional target session: when the frontend passes the user's active
        # session id, the run executes there so the streaming process shows
        # up in the chat (manual "Run now"). Without it the run falls back
        # to the automation's hidden backing session.
        target_session_id: str | None = None
        if request.body_exists:
            try:
                payload = await request.json()
                if isinstance(payload, dict) and isinstance(payload.get("session_id"), str):
                    target_session_id = payload["session_id"]
            except Exception:
                pass

        async def _run_and_emit():
            await self._run_automation_once(automation, scheduled=False, session_id=target_session_id)
            # Result is already persisted on the automation object and
            # emitted via the automation_run SSE event by _run_automation_once.

        asyncio.create_task(_run_and_emit())
        return web.json_response({"accepted": True, "automation": self._automation_payload(automation)})

    async def trigger_automation_now(self, aid: str, session_id: str | None = None) -> ChatResult | None:
        """Programmatic equivalent of POST /automations/{aid}/run.

        Called by the `manage_scheduled_tasks` tool's `action: "run"` to
        fire a task immediately without round-tripping through the HTTP
        layer. The run is fire-and-forget — returns once the async task
        is scheduled, not once the underlying chat completes.
        """
        self._load_persisted_automations()
        automation = self.automations.get(aid)
        if automation is None:
            raise KeyError(aid)

        async def _run_and_emit():
            await self._run_automation_once(
                automation, scheduled=False, session_id=session_id
            )

        asyncio.create_task(_run_and_emit())

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
        # Per-run sessions (each ephemeral, marked is_automation) are
        # left on disk intentionally: they're hidden from the sidebar
        # and reloading them won't pollute any new run (every new run
        # gets its own sid). Older automations persisted with a
        # long-lived session_id have that session already cleared by
        # the is_automation / hidden filter; nothing else to clean up.
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

        # Keep the client-supplied filename (and its extension) — only
        # sanitizing it so a hostile name can't escape the attachments dir.
        # Previously every upload was forced to clip-*.png, which silently
        # renamed a user's .pdf / .zip / .mp4 to .png and dropped the name.
        raw_name = (file_name or "").replace("\\", "/").split("/")[-1]
        for _ch in '\\/:*?"<>|':
            raw_name = raw_name.replace(_ch, "_")
        safe_name = raw_name.strip(". ")[:120] or f"clip-{uuid.uuid4().hex[:8]}"
        stem = Path(safe_name).stem or "clip"
        suffix = Path(safe_name).suffix.lower()
        if not suffix:
            # No extension on the name — infer one from the MIME so the file
            # is still recognizable on disk (and previewable by extension).
            suffix = mimetypes.guess_extension((mime or "").split(";")[0].strip()) or ".bin"
        attachments_dir = FileStorage._project_dir(pid) / "attachments" / sid
        attachments_dir.mkdir(parents=True, exist_ok=True)
        # Two uploads of the same filename must not overwrite each other —
        # fall back to a nonce-suffixed name on collision.
        disk_path = attachments_dir / safe_name
        if disk_path.exists():
            disk_path = attachments_dir / f"{stem}-{uuid.uuid4().hex[:6]}{suffix}"
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
            sid = None

        # Two path classes are allowed:
        #  1. Standard attachment paths (~/.ziva/sessions/<pid>/attachments/<sid>/...)
        #     — validated against the session's attachments root.
        #  2. Tool-generated images outside the attachments tree (e.g.
        #     /tmp/vvg_star/starry_night.jpg) — allowed only when the
        #     extension is in the image whitelist, so we don't turn this
        #     into an arbitrary file-read primitive for /etc/passwd etc.
        if sid is not None:
            # IMPORTANT: validate against the pid encoded in the URL itself,
            # not _pid_for(sid). The project-hash algorithm changed once
            # (pure hex → <basename>-<hex>), and even without that, users
            # move workspaces or rename them — every change invalidates the
            # absolute paths baked into old messages. _pid_for(sid) would
            # return the *current* pid, so an old-pid URL would 403 even
            # though the same file still exists under the new pid. Instead,
            # accept the URL's own pid and, if its directory is gone, fall
            # back to scanning sibling sessions/<*>/attachments/<sid>/ for
            # a file with the same name.
            from ziva.storage.file_storage import _BASE_DIR
            sessions_root = _BASE_DIR / "sessions"  # ~/.ziva/sessions
            sessions_root = sessions_root.resolve()
            try:
                tail = candidate.relative_to(sessions_root)  # <pid>/attachments/<sid>/file
            except ValueError:
                return web.json_response({"error": "outside_sessions_root"}, status=403)
            tail_parts = tail.parts
            if len(tail_parts) < 4 or tail_parts[1] != "attachments":
                return web.json_response({"error": "outside_attachments_dir"}, status=403)

            # Filename must look like an image — anything else is rejected
            # even when the path layout matches, to keep this from becoming
            # a generic file reader.
            filename = tail_parts[-1]
            if Path(filename).suffix.lower() not in _PREVIEWABLE_EXTS:
                return web.json_response({"error": "unsupported_file_type"}, status=403)

            if not candidate.is_file():
                # The original pid directory no longer exists (workspace moved
                # or hash algorithm changed). Look for the same file under
                # every known pid so historical messages keep rendering.
                for alt_root in sessions_root.iterdir():
                    if not alt_root.is_dir():
                        continue
                    alt_path = alt_root / "attachments" / sid / filename
                    if alt_path.is_file():
                        candidate = alt_path.resolve()
                        break
                else:
                    return web.json_response({"error": "not_found"}, status=404)
        else:
            if candidate.suffix.lower() not in _PREVIEWABLE_EXTS:
                return web.json_response({"error": "outside_attachments_dir"}, status=403)

        if not candidate.is_file():
            return web.json_response({"error": "not_found"}, status=404)

        mime, _ = mimetypes.guess_type(candidate.name)
        # Text/code files: force text/plain so the browser shows them inline
        # (mimetypes returns None / octet-stream for .md/.go, or wrong types
        # like video/mp2t for .ts, all of which trigger a download).
        if candidate.suffix.lower() in _TEXT_PREVIEW_EXTS:
            return web.Response(body=candidate.read_bytes(), content_type="text/plain", charset="utf-8")
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
        if "provider_name" in payload:
            updates["provider_name"] = payload["provider_name"]
        if "thinking_mode" in payload:
            updates["thinking_mode"] = payload["thinking_mode"]
        # Resolve the project_id the session actually lives in. Sessions
        # can belong to any of the known workspaces (active + recently
        # visited); using the runtime's current project_id here would
        # 404 a cross-workspace PATCH even when the session file is
        # sitting right there in another project's directory.
        #
        # An explicit `workspace` field in the payload still wins — the
        # sidebar uses it to rename sessions in non-active projects.
        target_pid = self._pid_for(sid)
        ws = payload.get("workspace")
        if isinstance(ws, str) and ws:
            try:
                target_pid = _project_hash(Path(ws))
            except Exception:
                pass
        if not FileStorage.get_session(target_pid, sid):
            return web.json_response({"error": "session_not_found"}, status=404)
        if updates:
            FileStorage.update_session(target_pid, sid, updates)
        # Mirror model_name onto the in-memory SessionState so the next
        # chat() turn picks it up immediately — don't wait for a
        # disk reload on the next _get_session. Only the active
        # project's sessions live in memory; sessions from other
        # projects will be populated from disk when they're loaded.
        if "model_name" in updates or "provider_name" in updates or "thinking_mode" in updates:
            sess = self.runtime._sessions.get(sid)
            if sess is not None:
                if "model_name" in updates:
                    sess.model_name = updates["model_name"]
                if "provider_name" in updates:
                    sess.provider_name = updates["provider_name"]
                if "thinking_mode" in updates:
                    sess.thinking_mode = updates["thinking_mode"]
            # Broadcast so split panes / IM-initiated changes stay in sync
            # even when the PATCH itself didn't originate in the current UI.
            changed: dict = {"type": "model_changed"}
            if "model_name" in updates:
                changed["model_name"] = updates["model_name"]
            if "provider_name" in updates:
                changed["provider_name"] = updates["provider_name"]
            if "thinking_mode" in updates:
                changed["thinking_mode"] = updates["thinking_mode"]
            await self.runtime._emit(sid, changed)
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
                    # Attach the owning provider so the UI can group/split
                    # same-named models across providers (e.g. glm-5.2 under
                    # both glm and opencode). Copy so we don't mutate config.
                    entry = dict(m)
                    entry["provider"] = p.get("name")
                    models_list.append(entry)
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

        target = Path(new_path).expanduser().resolve()
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
        one of the configured ``extra_paths`` directories — or, for the
        common "Claude's official skill tree via symlink" pattern, the
        target lives anywhere under ``$HOME`` after symlink resolution.
        Matches the symlink policy in ``Runtime.build_skill_index`` so a
        skill that's listed in the index can always be opened.
        """
        raw = request.query.get("path", "")
        if not raw:
            return web.json_response({"error": "path_required"}, status=400)
        raw_path = Path(raw).expanduser().absolute()
        target = raw_path.resolve()
        allowed_roots = self._skill_root_paths()
        home = Path.home().resolve()
        # Both halves must hold:
        #   * raw_path (the link path the caller submitted) must live
        #     under a configured ``extra_paths`` root — limits entry
        #     points to the same dirs the skill index scanned, so a
        #     stray URL like ``?path=~/.ssh/id_rsa`` cannot read private
        #     files even though they are under ``$HOME``.
        #   * target (after symlink resolution) must stay under
        #     ``$HOME`` — a symlink in ``~/.ziva/skills/<x>`` pointing
        #     outside ``$HOME`` (e.g. at ``/etc/passwd``) is still
        #     rejected. Mirrors ``Runtime.build_skill_index``'s
        #     symlink filter.
        if not (any(self._is_within(raw_path, r) for r in allowed_roots)
                and self._is_within(target, home)):
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
        # Make sure the current workspace is discoverable when listing sessions.
        # Without this, a backend started with workspace A only knows about A
        # plus previously-switched workspaces; a desktop app reusing this
        # backend would not see sessions from A unless we record it here.
        self._register_current_workspace()
        # STT warmup is started even earlier (see app.cli start_stt_warmup,
        # called right after Runtime is constructed). It runs in a daemon
        # thread that survives this method, so by the time the frontend
        # can hit /api/stt the model may already be loaded — see
        # /api/stt/status for the live state.

    def _warmup_stt(self) -> None:
        """Legacy method kept for backward compatibility. The actual
        warmup logic now lives in ``stt_warmup._warmup_stt`` and is
        kicked off in ``app.cli`` before the server is even constructed.
        Calling this method delegates to the new module.
        """
        from .stt_warmup import start_stt_warmup  # local import to avoid circulars
        start_stt_warmup(self.runtime)

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

        The state lives in :mod:`ziva.transports.desktop_api.stt_warmup`
        because the warmup is kicked off in ``app.cli`` *before* this
        server is constructed — running the warmup later (after
        ``site.start``) would lose the seconds spent during
        ``Runtime`` construction and ``BrowserWindow`` setup.
        """
        from .stt_warmup import stt_status as _stt_status
        return web.json_response({"status": _stt_status})

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
                    "model", "mlx-community/whisper-small-mlx"
                )
                # stt_model is a full HF repo id (e.g. "mlx-community/whisper-small-mlx").
                # Check local models_dir first, fall back to the repo id verbatim.
                candidate = models_dir / stt_model
                local_path = candidate if (candidate.exists() and (candidate / "weights.npz").exists()) else None
                model_ref = str(local_path) if local_path else stt_model
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
