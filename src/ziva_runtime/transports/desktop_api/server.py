from __future__ import annotations

import asyncio
import json
import shlex
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List

from aiohttp import web

from ziva_runtime.permissions import get_permission_manager
from ziva_runtime.runtime import Runtime
from ziva_runtime.shared_types import CancellationToken, ChatMessage, ToolCallItem
from ziva_runtime.storage.file_storage import FileStorage


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


@dataclass
class SessionStore:
    runtime: Runtime
    _loaded_sessions: Dict[str, Dict] = field(default_factory=dict)

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
            session = FileStorage.get_session(self.runtime.project_id, sid)
            if session:
                messages = []
                for msg_data in FileStorage.get_messages(self.runtime.project_id, sid):
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
        FileStorage.append_message(self.runtime.project_id, sid, {"role": role, "content": content})

    def list_all(self) -> list[Dict]:
        """List all sessions from file storage."""
        return FileStorage.list_sessions(self.runtime.project_id)

    def exists(self, sid: str) -> bool:
        """Check if session exists."""
        return FileStorage.get_session(self.runtime.project_id, sid) is not None


class DesktopAPIServer:
    def __init__(self, runtime: Runtime) -> None:
        self.runtime = runtime
        self.store = SessionStore(runtime=runtime)
        self.automations: Dict[str, Automation] = {}
        self._automation_tasks: Dict[str, asyncio.Task] = {}
        self._runner: web.AppRunner | None = None
        self.app = web.Application()
        self.app.router.add_get("/", self.index)
        self.app.router.add_get("/sessions", self.list_sessions)
        self.app.router.add_post("/sessions", self.create_session)
        self.app.router.add_get("/sessions/{sid}/messages", self.get_messages)
        self.app.router.add_get("/sessions/{sid}/turns", self.get_turns)
        self.app.router.add_post("/sessions/{sid}/turns", self.create_turn)
        self.app.router.add_post("/sessions/{sid}/compact", self.compact_session)
        self.app.router.add_post("/sessions/{sid}/prune", self.prune_session)
        self.app.router.add_post("/sessions/{sid}/cancel", self.cancel_turn)
        self.app.router.add_get("/events", self.events_global)
        self.app.router.add_get("/sessions/{sid}/events", self.events)
        self.app.router.add_get("/sessions/{sid}/tools", self.get_tools_status)
        self.app.router.add_get("/sessions/{sid}/plan", self.get_plan)
        self.app.router.add_get("/sessions/{sid}/diff", self.get_diff)
        self.app.router.add_post("/sessions/{sid}/revert", self.revert_files)
        self.app.router.add_patch("/sessions/{sid}", self.update_session)
        self.app.router.add_post("/automations", self.create_automation)
        self.app.router.add_get("/automations", self.list_automations)
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
        # Serve static assets from build output
        static_dir = Path(__file__).resolve().parent / "static"
        self.app.router.add_static("/assets", static_dir / "assets")

    async def index(self, _request: web.Request) -> web.Response:
        html = (Path(__file__).resolve().parent / "static" / "index.html").read_text(encoding="utf-8")
        return web.Response(text=html, content_type="text/html")

    async def create_session(self, request: web.Request) -> web.Response:
        sid = self.store.create()
        return web.json_response({"id": sid})

    async def list_sessions(self, request: web.Request) -> web.Response:
        sessions = self.store.list_all()
        items = [{"id": s["id"]} for s in sessions if "id" in s]
        return web.json_response({"sessions": items})

    async def get_messages(self, request: web.Request) -> web.Response:
        sid = request.match_info["sid"]
        if not self.store.exists(sid):
            return web.json_response({"error": "session_not_found"}, status=404)
        # Always read from FileStorage so we get messages persisted by the
        # runtime during a running turn (tool results, assistant text, etc.).
        # The in-memory _loaded_sessions cache may be stale if the runtime
        # has persisted new messages since the session was first loaded.
        meta = FileStorage.get_session(self.runtime.project_id, sid) or {}
        all_msgs = list(FileStorage.get_messages(self.runtime.project_id, sid))
        # By default, return the post-compact view (matches what the runtime
        # sees). With ?include_dropped=true, return the full history so the
        # UI's "expand earlier messages" affordance can render what was
        # compressed away.
        include_dropped = request.query.get("include_dropped") == "true"
        if include_dropped:
            msgs = all_msgs
        else:
            from ziva_runtime.session.compaction import _summary_only
            msgs = _summary_only(all_msgs)
        return web.json_response({
            "messages": msgs,
            "last_usage": meta.get("last_usage"),
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
            return web.json_response({"error": "session_not_found"}, status=404)
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
                # Reload messages from disk since runtime.chat() persisted them via FileStorage
                fresh_messages = []
                for msg_data in FileStorage.get_messages(self.runtime.project_id, sid):
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
                s = self.runtime._sessions.get(sid)
                if s:
                    s.cancel_token = None
                    s.turn_task = None

        task = asyncio.create_task(runner())
        session.turn_task = task
        return web.json_response({"accepted": True, "turn_id": turn_id})

    def _apply_post_compact(self, sid: str, working_set: List[ChatMessage]) -> Dict[str, Any]:
        """Rewrite disk + sync in-memory + runtime cache + refresh last_usage.

        Used by /prune (working_set is the pruned list) and /compact
        (working_set is `[summary, ...compacted_originals]`). Either way,
        the on-disk file and the in-memory store are replaced with the
        same content. `_compacted` flags are persisted so the UI's
        collapse bar can identify which messages are folded.

        `last_usage.prompt_tokens` is computed from the LLM-visible
        subset (`_llm_context(working_set)`) — that's the actual cost
        of the next turn, not the on-disk size which includes the
        compacted originals that won't be sent to the model.
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
            if m._compaction_summary:
                record["_compaction_summary"] = True
            if m._compacted:
                record["_compacted"] = True
            records.append(record)
        FileStorage.replace_messages(self.runtime.project_id, sid, records)

        session = self.store._ensure_loaded(sid)
        session["messages"] = list(records)
        if sid in self.runtime._sessions:
            # Runtime cache holds ONLY the LLM-visible context — the
            # compacted originals are kept on disk for the UI but should
            # not bloat the next chat() call's prompt.
            from ziva_runtime.session.compaction import _llm_context
            self.runtime._sessions[sid].history = _llm_context(working_set)

        from ziva_runtime.session.compaction import estimate_tokens, _llm_context
        llm_visible = _llm_context(working_set)
        new_prompt_tokens = estimate_tokens(llm_visible)
        new_usage = {"prompt_tokens": new_prompt_tokens, "completion_tokens": 0, "total_tokens": new_prompt_tokens}
        FileStorage.update_session(self.runtime.project_id, sid, {
            "id": sid,
            "time": {"updated": int(time.time() * 1000)},
            "last_usage": new_usage,
        })
        return new_usage

    def _load_session_messages(self, sid: str) -> List[ChatMessage]:
        """Read messages from disk as ChatMessage objects."""
        messages: List[ChatMessage] = []
        for msg_data in FileStorage.get_messages(self.runtime.project_id, sid):
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
                _compacted=msg_data.get("_compacted", False),
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
        FileStorage.append_message(self.runtime.project_id, sid, record)

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
        from ziva_runtime.session.compaction import prune_messages
        pruned = prune_messages(messages)

        new_usage = self._apply_post_compact(sid, pruned)
        return web.json_response({
            "success": True,
            "message_count": len(pruned),
            "last_usage": new_usage,
        })

    async def compact_session(self, request: web.Request) -> web.Response:
        """POST /sessions/{sid}/compact — generate a model summary.

        Replaces the LLM-visible history with `[summary]` while preserving
        the original messages on disk (stamped with `_compacted=True`).
        Aligned with codex CLI / claude code semantics: no "recent tail"
        is preserved in the LLM context, the summary is the new starting
        point. The UI's collapse bar can still expand to show the
        originals, so the user never loses their history — it just gets
        folded.

        /prune is a separate user-driven operation; /compact is a pure
        summary and does NOT collapse tool outputs.

        On-disk layout becomes `[summary, ...compacted_originals]`.

        Returns a 200 with `noop=true` if there is nothing meaningful to
        compress (e.g. fewer than 3 messages or the model + fallback both
        produced no summary). The UI can treat this the same as a
        successful no-op.
        """
        sid = request.match_info["sid"]
        if not self.store.exists(sid):
            return web.json_response({"error": "session_not_found"}, status=404)

        messages = self._load_session_messages(sid)
        model_cfg = self.runtime.config.get("model", {})
        model_name = model_cfg.get("name", "")
        context_window = int(self.runtime.config.get("memory", {}).get("context_window_tokens", 200000) or 200000)

        # Only compact the LLM-visible context (summary + new messages).
        # Compacted originals from earlier compactions are preserved on disk
        # but excluded from the new summary to avoid re-compressing old data.
        from ziva_runtime.session.compaction import _llm_context
        llm_visible = _llm_context(messages)
        # Collect pre-existing compacted originals so they are preserved on
        # disk after _apply_post_compact replaces the message file.
        old_compacted = [m for m in messages if m._compacted]

        try:
            from ziva_runtime.session.compaction import compact_messages
            summary_list, compacted_originals = await compact_messages(
                llm_visible, context_window, model_name, self.runtime.model_adapter
            )
        except Exception as exc:
            return web.json_response({"error": "compact_failed", "message": str(exc)}, status=500)

        summary_msg = summary_list[0] if summary_list else None
        if summary_msg is None:
            # Nothing to compact — too few messages or the model + fallback
            # both produced no summary. Surface a graceful noop rather than
            # a 500 so the UI can show a clear "nothing to compact" toast.
            from ziva_runtime.session.compaction import estimate_tokens
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

        # On-disk layout: [new_summary, ...just-compacted_originals, ...old_compacted]
        # Old compacted originals are appended at the end so each compact's
        # originals form a contiguous block following its summary.
        on_disk = [summary_msg] + list(compacted_originals) + old_compacted
        new_usage = self._apply_post_compact(sid, on_disk)
        return web.json_response({
            "success": True,
            "message_count": 1,
            "original_count": len(compacted_originals),
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
                await resp.write(f"data: {json.dumps(event, ensure_ascii=False, default=str)}\n\n".encode("utf-8"))
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

        plan_steps = []
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

    async def create_automation(self, request: web.Request) -> web.Response:
        payload = await request.json()
        name = payload.get("name", "unnamed")
        prompt = payload.get("prompt", "")
        interval = int(payload.get("interval_seconds", 300))

        if not prompt:
            return web.json_response({"error": "prompt is required"}, status=400)

        aid = str(uuid.uuid4())
        sid = self.store.create()
        automation = Automation(id=aid, name=name, prompt=prompt, interval_seconds=interval, session_id=sid)
        self.automations[aid] = automation

        async def runner() -> None:
            while automation.enabled:
                try:
                    messages = [ChatMessage(role="user", content=automation.prompt)]
                    result = await self.runtime.chat(messages, session_id=automation.session_id)
                    automation.last_run = time.time()
                    automation.last_result = result.content[:500]
                except Exception:
                    automation.last_run = time.time()
                await asyncio.sleep(automation.interval_seconds)

        self._automation_tasks[aid] = asyncio.create_task(runner())
        return web.json_response({"id": aid, "session_id": sid})

    async def list_automations(self, _request: web.Request) -> web.Response:
        items = []
        for a in self.automations.values():
            items.append({
                "id": a.id, "name": a.name, "interval_seconds": a.interval_seconds,
                "enabled": a.enabled, "last_run": a.last_run, "last_result": a.last_result,
            })
        return web.json_response({"automations": items})

    async def delete_automation(self, request: web.Request) -> web.Response:
        aid = request.match_info["aid"]
        automation = self.automations.get(aid)
        if not automation:
            return web.json_response({"error": "not_found"}, status=404)
        automation.enabled = False
        task = self._automation_tasks.pop(aid, None)
        if task:
            task.cancel()
        del self.automations[aid]
        return web.json_response({"deleted": True})

    async def permission_reply(self, request: web.Request) -> web.Response:
        """Handle permission approval/rejection."""
        request_id = request.match_info["request_id"]
        payload = await request.json()
        action = payload.get("action", "once")
        message = payload.get("message")

        perm_manager = get_permission_manager()
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

    async def delete_session(self, request: web.Request) -> web.Response:
        sid = request.match_info["sid"]
        FileStorage.delete_session(self.runtime.project_id, sid)
        if sid in self.store._loaded_sessions:
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
        if not self.store.exists(sid):
            return web.json_response({"error": "session_not_found"}, status=404)
        payload = await request.json()
        updates = {}
        if "name" in payload:
            updates["name"] = payload["name"]
        if updates:
            FileStorage.update_session(self.runtime.project_id, sid, updates)
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
            tool_count = sum(1 for t in mcp_client._tools if True)
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
        })

    async def update_config(self, request: web.Request) -> web.Response:
        payload = await request.json()
        if "model" in payload:
            if "model" not in self.runtime.config:
                self.runtime.config["model"] = {}
            self.runtime.config["model"].update(payload["model"])
        if "approval" in payload:
            if "approval" not in self.runtime.config:
                self.runtime.config["approval"] = {}
            self.runtime.config["approval"].update(payload["approval"])
        return web.json_response({"ok": True})

    async def get_config_yaml(self, _request: web.Request) -> web.Response:
        """Return the raw YAML config file content."""
        config_path = self.runtime.workspace_root / ".ziva" / "config.yaml"
        if not config_path.exists():
            return web.json_response({"yaml": ""})
        return web.json_response({"yaml": config_path.read_text(encoding="utf-8")})

    async def save_config_yaml(self, request: web.Request) -> web.Response:
        """Save raw YAML content to the workspace config file."""
        payload = await request.json()
        yaml_text = payload.get("yaml", "")
        config_path = self.runtime.workspace_root / ".ziva" / "config.yaml"
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
        """Save a JSON config object, writing it as YAML."""
        payload = await request.json()
        import yaml
        config_path = self.runtime.workspace_root / ".ziva" / "config.yaml"
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(yaml.dump(payload, default_flow_style=False, allow_unicode=True, sort_keys=False), encoding="utf-8")
        # Hot-reload the in-memory config
        self.runtime.config.update(payload)
        return web.json_response({"ok": True})

    async def get_status(self, _request: web.Request) -> web.Response:
        model = self.runtime.config.get("model", {})
        return web.json_response({
            "model": model.get("name", "unknown"),
            "workspace": str(self.runtime.workspace_root),
            "tools": [t["name"] for t in self.runtime.list_tools()],
            "approval_policy": self.runtime.config.get("approval", {}).get("policy", "suggest"),
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
        skill_index = self.runtime.config.get("_skill_index", [])
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

    async def stop(self) -> None:
        """Gracefully stop the server."""
        for task in self._automation_tasks.values():
            task.cancel()
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
