"""``IMBridge`` — owns the adapters and routes inbound IM messages to sessions.

An IM message becomes a normal user turn on a normal ziva session — the same
``runtime.chat_with_events`` path the desktop composer's ``create_turn``
takes. The session is created once per ``(channel, account_id, chat_id)``
conversation and reused so context survives across messages. The model's reply
is forwarded back to the IM chat via the originating adapter.

Security: every inbound message is checked against a sender whitelist
(``allowed_senders`` in ``~/.ziva/config.yaml`` under ``im_bridge``). An empty whitelist
rejects everything (fail-closed) — without this, anyone who can message the
linked account could trigger ``shell`` / ``edit_file`` on the host.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
import shutil
import time
import uuid
from pathlib import Path
from typing import Any, Dict

from ziva.runtime import Runtime
from ziva.shared_types import CancellationToken, ChatMessage
from ziva.storage.file_storage import FileStorage

from ziva.transports.im_bridge.adapters.feishu import FeishuAdapter
from ziva.transports.im_bridge.adapters.telegram import TelegramAdapter
from ziva.transports.im_bridge.adapters.base import BaseAdapter, classify_media
from ziva.transports.im_bridge.models import IncomingMessage, OutgoingMessage
from ziva.transports.im_bridge.store import IMConfig

logger = logging.getLogger(__name__)

# Channel name → adapter class. WeChat is intentionally omitted from the UI
# because it has no official Bot API and requires an external, unofficial
# gateway; the adapter file remains in case we re-enable it later.
ADAPTERS: Dict[str, type[BaseAdapter]] = {
    "feishu": FeishuAdapter,
    "telegram": TelegramAdapter,
}


class IMBridge:
    def __init__(self, runtime: Runtime, store: Any) -> None:
        self.runtime = runtime
        self.store = store  # desktop_api.SessionStore
        self.config = IMConfig.load()
        self._adapters: Dict[str, BaseAdapter] = {}
        self._locks: Dict[str, asyncio.Lock] = {}
        # Sender IDs that were recently rejected by the whitelist. Surfaced in
        # the UI so the owner can approve them without looking up IDs manually.
        self._pending_senders: list[Dict[str, Any]] = []
        # Ask_user: while a question is awaiting an IM-side reply, we record
        # the routing (sid → chat_id/channel) and the question's call_id so a
        # follow-up message from the same chat can be intercepted as the
        # answer instead of being fed to the model as a new turn.
        self._pending_questions: Dict[str, Dict[str, Any]] = {}
        self._sid_route: Dict[str, Dict[str, str]] = {}
        # chat_id -> FIFO queue of pending ask_user call_ids. A single turn
        # can ask several questions at once (parallel ask_user tool calls);
        # we match the user's replies to the questions in the order they were
        # asked. One chat maps to one session, so the queue is per-conversation.
        self._pending_chat_index: Dict[str, list[str]] = {}

    # -- lifecycle ----------------------------------------------------------

    async def start(self) -> None:
        # Subscribe once: the ask_user tool emits a question, the bridge
        # pushes it to the IM chat, and the user's reply is matched back
        # to the question via the pending_questions map in _handle.
        try:
            self.runtime.on_ask_user(self._on_ask_user_question)
            self.runtime.on_send_file(self._on_send_file)
        except Exception:
            logger.exception("im_bridge: failed to register runtime callbacks")
        for name, cfg in self.config.channels.items():
            if cfg.enabled and name in ADAPTERS:
                try:
                    await self._start_adapter(name)
                except Exception:
                    logger.exception("im_bridge: failed to start channel %s", name)
                    # Disable the channel so a bad config doesn't retry forever.
                    cfg.enabled = False
                    self.config.save()

    async def stop(self) -> None:
        for name, adapter in list(self._adapters.items()):
            try:
                await adapter.stop()
            except Exception:
                logger.exception("im_bridge: error stopping channel %s", name)
        self._adapters.clear()

    async def _start_adapter(self, name: str) -> BaseAdapter:
        # Stop any existing instance for this channel first.
        old = self._adapters.pop(name, None)
        if old:
            try:
                await old.stop()
            except Exception:
                pass
        cfg = self.config.channels[name]
        cls = ADAPTERS[name]
        adapter = cls(cfg, self.on_message)
        self._adapters[name] = adapter
        await adapter.start()
        if adapter.status().get("state") == "error":
            err = adapter.status().get("error") or "连接失败"
            self._adapters.pop(name, None)
            raise RuntimeError(err)
        # Persist any identity the adapter discovered (e.g. telegram bot id).
        if cfg.account_id and cfg.account_id != self.config.channels[name].account_id:
            self.config.channels[name].account_id = cfg.account_id
        self.config.save()
        return adapter

    # -- inbound path -------------------------------------------------------

    async def on_message(self, msg: IncomingMessage) -> None:
        """Entry point for adapters — fire-and-forget so the receive loop
        isn't blocked while the model runs."""
        asyncio.create_task(self._handle(msg))

    async def _handle(self, msg: IncomingMessage) -> None:
        if not self._is_allowed_sender(msg):
            logger.info("im_bridge: dropped non-whitelisted sender %s", msg.sender_id)
            return
        # Ask_user answer interception (must run before /stop — a /stop
        # issued while waiting on a question should still cancel the turn
        # and let the ask_user future resolve, but a free-text answer
        # arrives here as a normal message and should be matched first).
        # When multiple questions are pending for this chat, each reply
        # answers the oldest unanswered one (FIFO) — the order they were
        # asked.
        queue = self._pending_chat_index.get(msg.chat_id) or []
        call_id = queue[0] if queue else None
        if call_id and call_id in self._pending_questions:
            queue.pop(0)
            if not queue:
                self._pending_chat_index.pop(msg.chat_id, None)
            info = self._pending_questions.pop(call_id)
            sid = info["sid"]
            # Take the first line of the user's reply as the answer; IM
            # platforms commonly prefix quoted/forwarded text that would
            # otherwise confuse the model.
            answer = (msg.text or "").strip().splitlines()[0] if msg.text else ""
            try:
                self.runtime.set_user_answer(sid, answer, call_id=call_id)
            except Exception:
                logger.exception("im_bridge: set_user_answer failed for %s", call_id)
            return
        # Parse slash commands before the per-conversation lock so that
        # /stop can cancel the running turn that is currently holding the
        # lock. Without this, /stop would queue behind the running turn and
        # could only execute after the model had already finished.
        text = (msg.text or "").strip()
        is_slash = text.startswith("/") and not msg.images
        cmd = text.split(maxsplit=1)[0].lower() if is_slash else ""
        if is_slash and cmd == "/stop":
            reply = await self._dispatch_slash_command(msg, text)
            if reply is not None:
                adapter = self._adapters.get(msg.channel)
                if adapter:
                    try:
                        await adapter.send_message(OutgoingMessage(chat_id=msg.chat_id, text=reply))
                    except Exception:
                        logger.exception("im_bridge: failed to send /stop reply to %s", msg.channel)
            return
        # Serialize turns per conversation so concurrent messages queue
        # rather than racing on the same session's history.
        lock = self._locks.setdefault(msg.route_key, asyncio.Lock())
        async with lock:
            # Slash commands. Only run when the message has no images — an
            # image + slash text is almost certainly an unintentional
            # command attempt. /new /model /stop /compact are recognized; any
            # other "/…" text still falls through to the LLM (the desktop
            # composer does the same thing).
            if is_slash:
                reply = await self._dispatch_slash_command(msg, text)
                if reply is not None:
                    adapter = self._adapters.get(msg.channel)
                    if adapter:
                        try:
                            await adapter.send_message(OutgoingMessage(chat_id=msg.chat_id, text=reply))
                        except Exception:
                            logger.exception("im_bridge: failed to send slash reply to %s", msg.channel)
                    return
            sid = self._route_session(msg)
            self._ensure_session(sid, msg)
            image_paths = self._persist_images(sid, msg.images)
            # Best-effort typing indicator: the user on the IM side gets
            # immediate feedback that the agent is processing, even if the
            # turn takes several seconds. ``stop_typing`` is called in both
            # success and failure paths to remove the placeholder.
            adapter = self._adapters.get(msg.channel)
            if adapter:
                try:
                    await adapter.send_typing(msg.chat_id, msg.message_id)
                except Exception:
                    logger.exception("im_bridge: send_typing failed for %s", msg.channel)
            try:
                reply, output_images = await self._run_turn(sid, msg.text, image_paths)
            except Exception as exc:
                logger.exception("im_bridge: turn failed for %s", msg.route_key)
                reply, output_images = f"[ziva error] {exc}", []
            if adapter:
                try:
                    await adapter.stop_typing(msg.chat_id)
                except Exception:
                    logger.exception("im_bridge: stop_typing failed for %s", msg.channel)
                try:
                    await adapter.send_message(OutgoingMessage(
                        chat_id=msg.chat_id, text=reply, images=output_images,
                    ))
                except Exception:
                    logger.exception("im_bridge: failed to send reply to %s", msg.channel)

    async def _dispatch_slash_command(self, msg: IncomingMessage, text: str) -> str | None:
        """Run an IM-side slash command. Returns the reply text, or None when
        the text starts with "/" but isn't a recognized command (caller
        should fall through to the LLM so the model can interpret it)."""
        if not text:
            return None
        # Split command + optional argument: "/model foo bar" → ("/model", "foo bar")
        parts = text.split(maxsplit=1)
        if not parts or not parts[0]:
            return None
        cmd = parts[0].lower()
        arg = parts[1].strip() if len(parts) > 1 else ""

        # --- /new: end the current IM session, start a fresh one ---------------
        if cmd == "/new":
            old_sid = self.config.route_for(msg.route_key)
            try:
                new_sid = self.store.create()
            except Exception as exc:
                logger.exception("im_bridge: /new failed to create session")
                return f"[ziva error] failed to start a new session: {exc}"
            # Rebind the (channel, account, chat) route to the fresh sid so
            # subsequent messages continue the new conversation. Keep the old
            # session on disk in case the user wants to scroll back.
            self.config.set_route(msg.route_key, new_sid)
            try:
                # /new has no user text yet, so a fallback name is the only
                # option — but we still tag source/channel/sender for the
                # sidebar icon + routing. The name gets replaced on the next
                # real message by _ensure_session's first-text fallback.
                FileStorage.update_session(self.runtime.project_id, new_sid, {
                    "source": "im-bridge",
                    "channel": msg.channel,
                    "chat_id": msg.chat_id,
                    "sender_id": msg.sender_id,
                    "sender_name": msg.sender_name,
                    "name": f"{msg.sender_name} · {msg.channel}",
                    "time": {"created": int(time.time() * 1000), "updated": int(time.time() * 1000)},
                })
            except Exception:
                logger.exception("im_bridge: /new failed to tag fresh session")
            old_hint = f" (previous: `{old_sid[:8]}`)" if old_sid else ""
            return f"已开启新会话 `{new_sid[:8]}`{old_hint}。"

        # --- /stop: cancel the in-flight turn on the current session ----------
        if cmd == "/stop":
            sid = self.config.route_for(msg.route_key)
            if not sid:
                return "当前没有正在运行的会话。"
            session = self.runtime._sessions.get(sid)
            if not session:
                # Ensure the session is loaded from disk if it exists; a
                # desktop-initiated turn may be running on this session and
                # the IM bridge has not yet loaded it into memory.
                try:
                    if self.store.exists(sid):
                        session = self.runtime._get_session(sid)
                except Exception:
                    logger.exception("im_bridge: /stop failed to load session %s", sid)
            if not session:
                return "当前没有正在运行的会话。"
            token = session.cancel_token
            task = session.turn_task
            if not token or (not task or task.done()):
                return "当前没有正在运行的轮次。"
            # Mark the turn record as cancelled immediately so the desktop UI
            # stops showing a stop button the next time it polls /turns.
            store_session = self.store._ensure_loaded(sid)
            for t in store_session.get("turns", []):
                if t.get("status") == "running":
                    t["status"] = "cancelled"
                    break
            # Both cancel paths: token for the model loop, task for the asyncio
            # task itself. The desktop stop button does the same thing.
            token.cancel()
            task.cancel()
            # Cancel any pending ask_user future so the awaited tool sees
            # a cancelled envelope instead of hanging forever, and clear our
            # own pending-question bookkeeping for this conversation so the
            # user's next message isn't swallowed as a stale answer.
            try:
                self.runtime.cancel_all_questions(sid)
            except Exception:
                logger.exception("im_bridge: cancel_all_questions failed for %s", sid)
            self._clear_pending_for_session(sid)
            return "已停止当前轮次。"

        # --- /model: list or switch model ------------------------------------
        if cmd == "/model":
            try:
                cfg = self.runtime.config
            except Exception:
                cfg = {}
            model_cfg = cfg.get("model", {}) if isinstance(cfg, dict) else {}
            current = model_cfg.get("name", "unknown")
            available: list[str] = []
            for p in (cfg.get("providers", []) if isinstance(cfg, dict) else []):
                for m in p.get("models", []):
                    if m.get("name"):
                        available.append(m["name"])
            if not available:
                available = [current]
            sid = self.config.route_for(msg.route_key)
            # `current` must reflect the SESSION's model (set by a prior
            # /model switch), not the global config default — otherwise the
            # list always marks the default and never the switched model.
            if sid:
                _sess_meta = FileStorage.get_session(self.runtime.project_id, sid) or {}
                current = _sess_meta.get("model_name") or current
            if not arg:
                lines = [f"● {m}" if m == current else f"○ {m}" for m in available]
                return f"可用模型（当前: {current}）：\n" + "\n".join(lines) + "\n\n用法: /model <名称>"
            if arg not in available:
                return f"未知模型: {arg}\n可用: {', '.join(available)}"
            if not sid:
                return "请先发送一条消息再切换模型。"
            try:
                FileStorage.update_session(self.runtime.project_id, sid, {"model_name": arg})
            except Exception as exc:
                logger.exception("im_bridge: /model failed")
                return f"[ziva error] 切换模型失败: {exc}"
            # Mirror the change onto the live runtime SessionState so the
            # next chat turn uses the new model immediately.  Writing to disk
            # is not enough: once a session is loaded into memory, the
            # runtime keeps using session.model_name for every turn's system
            # context and adapter selection.
            sess = self.runtime._get_session(sid)
            sess.model_name = arg
            # Broadcast a sync event so the desktop UI updates its dropdown
            # (and split-pane / background copies) without requiring a page
            # refresh or session switch.
            with contextlib.suppress(Exception):
                await self.runtime._emit(sid, {"type": "model_changed", "model_name": arg})
            return f"已将模型切换为 {arg}。"

        # --- /compact: trim the IM session's history ------------------------
        if cmd == "/compact" or cmd == "/prune":
            sid = self.config.route_for(msg.route_key)
            if not sid:
                return "当前没有可压缩的会话。"
            try:
                # Both paths share the same persistence / cache refresh
                # helper that the HTTP /sessions/{sid}/compact and /prune
                # routes use. Keeping the work in one place means the IM
                # side automatically picks up any future change to the
                # on-disk layout or the in-memory caches.
                from ziva.transports.desktop_api.server import (
                    persist_message_set, load_session_messages,
                )
                from ziva.session.compaction import (
                    _llm_context, compact_messages, compose_post_compact_on_disk,
                    find_last_summary_idx, find_cutoff_in_llm_visible,
                )
                from ziva.runtime import _create_adapter, AUTO_COMPACT_KEEP_LAST_ASSISTANT_TURNS

                pid = self.runtime.project_id
                msgs = load_session_messages(sid, pid)
                if not msgs:
                    return "当前会话没有消息可处理。"
                if cmd == "/prune":
                    # /prune is purely local — no LLM call, just strip tool
                    # outputs from old messages.
                    from ziva.session.compaction import prune
                    kept = prune(msgs, keep_last=2)
                    persist_message_set(sid, kept, pid, self.runtime, self.store)
                    removed = len(msgs) - len(kept)
                    return f"已清理工具输出（移除 {removed} 条）。"

                # /compact: ask the same model for a summary, then replace
                # the older messages on disk with that summary.
                model_cfg = dict(self.runtime.config.get("model", {}))
                sess_state = self.runtime._sessions.get(sid)
                if sess_state and sess_state.model_name:
                    model_cfg["name"] = sess_state.model_name
                context_window = int(
                    self.runtime.config.get("memory", {}).get("context_window_tokens", 200000) or 200000
                )
                turn_config = dict(self.runtime.config)
                turn_config["model"] = model_cfg
                adapter = _create_adapter(turn_config)
                llm_visible = _llm_context(msgs)
                summary_list = await compact_messages(
                    llm_visible, context_window, model_cfg.get("name", ""), adapter,
                    keep_last_assistant_turns=AUTO_COMPACT_KEEP_LAST_ASSISTANT_TURNS,
                )
                if not summary_list or summary_list is llm_visible:
                    return "当前上下文已经很短，无需压缩。"
                last_summary_idx = find_last_summary_idx(msgs)
                cutoff = find_cutoff_in_llm_visible(llm_visible, AUTO_COMPACT_KEEP_LAST_ASSISTANT_TURNS)
                on_disk = compose_post_compact_on_disk(msgs, last_summary_idx, cutoff, summary_list)
                persist_message_set(sid, on_disk, pid, self.runtime, self.store)
                return f"已压缩上下文（保留 {len(summary_list)} 条）。"
            except Exception as exc:
                logger.exception("im_bridge: /compact failed")
                return f"[ziva error] 压缩失败: {exc}"

        return None  # not a recognized command — let the LLM handle it.

    def _is_allowed_sender(self, msg: IncomingMessage) -> bool:
        allowed = self.config.allowed_senders
        if msg.sender_id in allowed:
            return True
        # Remember the sender so the owner can approve them via the UI.
        self._remember_pending_sender(msg)
        # fail-closed: empty whitelist rejects everything.
        return False

    async def _on_ask_user_question(self, session_id: str, question: str, options: Any, call_id: str, multi_select: bool = False) -> None:
        """Forward an ask_user question to the originating IM chat.

        Registered with ``runtime.on_ask_user``. We look up the routing
        the bridge recorded when the IM message was first routed to a
        session, format the question + options into a short text reply,
        and push it back through the adapter. The reply is then matched
        to the pending call_id when the user sends their next message.

        We ``await`` the adapter send (rather than fire-and-forget) so the
        question is actually delivered before the tool blocks waiting for
        the answer — a detached task can be GC'd or fail silently, which
        leaves the IM user with no question and the model hanging forever.
        The ask_user tool awaits coroutines returned by callbacks.
        """
        route = self._sid_route.get(session_id) or {}
        chat_id = route.get("chat_id", "")
        channel = route.get("channel", "")
        if not chat_id or not channel:
            # The session was never routed through IM (e.g. a desktop-driven
            # turn happens to call ask_user); no IM-side answer is possible.
            logger.info(
                "im_bridge: ask_user from session %s has no IM route; not pushing to any chat",
                session_id,
            )
            return
        adapter = self._adapters.get(channel)
        if not adapter:
            logger.warning("im_bridge: ask_user channel %s not connected; question not delivered", channel)
            return
        self._pending_questions[call_id] = {
            "sid": session_id,
            "chat_id": chat_id,
            "channel": channel,
            "options": options or [],
            "multi_select": multi_select,
        }
        # Append (FIFO) so concurrent ask_user calls in one turn each get
        # matched to the user's replies in the order they were asked.
        self._pending_chat_index.setdefault(chat_id, []).append(call_id)
        text = self._format_ask_user_prompt(question, options or [], multi_select)
        try:
            await adapter.send_message(OutgoingMessage(chat_id=chat_id, text=text))
        except Exception:
            logger.exception("im_bridge: failed to push ask_user question to %s", channel)

    async def _on_send_file(self, session_id: str, path: str, media_type: str | None, caption: str) -> bool:
        """Deliver a ``send_file`` payload to the originating IM chat.

        Registered with ``runtime.on_send_file``. We reuse the routing the
        bridge recorded when the IM message was first routed to a session
        (``_sid_route``). The kind is the model's ``media_type`` hint, falling
        back to extension classification: images go via the ``images`` field
        (adapters send as photos), everything else via ``files`` (adapters
        send as video/document). Returns True iff a channel took the delivery.
        """
        route = self._sid_route.get(session_id) or {}
        chat_id = route.get("chat_id", "")
        channel = route.get("channel", "")
        if not chat_id or not channel:
            # Desktop-driven turn (no IM route) — nothing to push to.
            return False
        adapter = self._adapters.get(channel)
        if not adapter:
            logger.warning("im_bridge: send_file channel %s not connected", channel)
            return False
        # media_type from the model is one of image/video/file; classify_media
        # returns image/video/document. Either way only "image" routes to the
        # photo path — everything else is a file (video or document).
        kind = media_type or classify_media(path)
        try:
            if kind == "image":
                await adapter.send_message(
                    OutgoingMessage(chat_id=chat_id, text=caption, images=[path])
                )
            else:
                await adapter.send_message(
                    OutgoingMessage(chat_id=chat_id, text=caption, files=[path])
                )
        except Exception:
            logger.exception("im_bridge: failed to deliver send_file to %s", channel)
            return False
        return True

    @staticmethod
    def _format_ask_user_prompt(question: str, options: list, multi_select: bool) -> str:
        """Render an ask_user question + options as plain text for IM."""
        lines = [f"❓ {question}"]
        if options:
            kind = "(可多选，请回复序号或内容)" if multi_select else "(回复序号或内容)"
            lines.append(kind)
            for idx, opt in enumerate(options, start=1):
                if isinstance(opt, dict):
                    label = opt.get("label", "")
                    desc = opt.get("description", "")
                    if desc:
                        lines.append(f"{idx}. {label} — {desc}")
                    else:
                        lines.append(f"{idx}. {label}")
                else:
                    lines.append(f"{idx}. {opt}")
        return "\n".join(lines)

    def _clear_pending_for_session(self, sid: str) -> None:
        """Drop ask_user bookkeeping for a session (e.g. on /stop).

        Cancelling the runtime futures alone isn't enough: our own
        ``_pending_questions`` / ``_pending_chat_index`` would otherwise still
        match the user's next message to a dead question and swallow it.
        One chat maps to one session, so dropping the chat's queue is enough.
        """
        route = self._sid_route.get(sid) or {}
        chat_id = route.get("chat_id", "")
        if chat_id:
            self._pending_chat_index.pop(chat_id, None)
        self._pending_questions = {
            cid: v for cid, v in self._pending_questions.items()
            if v.get("sid") != sid
        }

    def _remember_pending_sender(self, msg: IncomingMessage) -> None:
        """Keep the most recent blocked sender IDs in memory for the UI."""
        # Remove duplicates (same sender_id) and keep the newest entry at the end.
        self._pending_senders = [s for s in self._pending_senders if s.get("sender_id") != msg.sender_id]
        self._pending_senders.append({
            "channel": msg.channel,
            "sender_id": msg.sender_id,
            "sender_name": msg.sender_name,
            "timestamp": time.time(),
        })
        # Cap the list so it doesn't grow unbounded.
        if len(self._pending_senders) > 20:
            self._pending_senders = self._pending_senders[-20:]

    def get_pending_senders(self) -> list[Dict[str, Any]]:
        return list(self._pending_senders)

    def approve_sender(self, sender_id: str) -> bool:
        if not sender_id or sender_id in self.config.allowed_senders:
            return False
        self.config.allowed_senders.append(sender_id)
        self.config.save()
        self._pending_senders = [s for s in self._pending_senders if s.get("sender_id") != sender_id]
        return True

    def _route_session(self, msg: IncomingMessage) -> str:
        sid = self.config.route_for(msg.route_key)
        if sid and self.store.exists(sid):
            return sid
        # New conversation → new ordinary session (same as clicking "新对话").
        sid = self.store.create()
        self.config.set_route(msg.route_key, sid)
        return sid

    def _ensure_session(self, sid: str, msg: IncomingMessage) -> None:
        """Tag the session with its IM origin (ad-hoc fields, not a metadata
        API — Runtime has no create_session(metadata=…), SessionState has no
        metadata field). These fields drive the sidebar source icon + routing
        and do not change session behavior.

        The ``name`` field is only written when the session doesn't already
        have one. Otherwise every incoming message would clobber a
        user-renamed title (PATCH /sessions/{sid} from the sidebar) or the
        title the desktop UI's enrichment loop derived from the first user
        message. For the fallback, prefer the actual message text over the
        bare "sender · channel" string — the former is informative even for
        sessions outside the sidebar's slice(0, 10) enrichment window.
        """
        ws = self.config.default_workspace or str(self.runtime.workspace_root)
        updates: Dict[str, Any] = {
            "source": "im-bridge",
            "channel": msg.channel,
            "chat_id": msg.chat_id,
            "sender_id": msg.sender_id,
            "sender_name": msg.sender_name,
            "workspace_root": ws,
            "time": {"created": int(time.time() * 1000), "updated": int(time.time() * 1000)},
        }
        try:
            existing = FileStorage.get_session(self.runtime.project_id, sid) or {}
        except Exception:
            existing = {}
        if not existing.get("name"):
            text_preview = (msg.text or "").strip().replace("\n", " ")
            # Truncate to roughly one sidebar row; Chinese chars are wide
            # so 60 chars ≈ 1.5 rows of text, plenty of signal.
            if len(text_preview) > 60:
                text_preview = text_preview[:60].rstrip() + "…"
            if text_preview:
                updates["name"] = text_preview
            elif msg.images:
                updates["name"] = f"[图片] {msg.sender_name}"
            else:
                updates["name"] = f"{msg.sender_name} · {msg.channel}"
        FileStorage.update_session(self.runtime.project_id, sid, updates)
        # Cache the route so ask_user callbacks can locate the IM chat
        # without having to walk self.config.routes or query the store.
        self._sid_route[sid] = {"chat_id": msg.chat_id, "channel": msg.channel}

    def _persist_images(self, sid: str, temp_paths: list[str]) -> list[str]:
        """Move adapter-downloaded images into the session attachments dir.

        Returns the new absolute paths under
        ``~/.ziva/sessions/<pid>/attachments/<sid>/``. The runtime later
        expands these paths into base64 data URLs for vision models.
        """
        if not temp_paths:
            return []
        pid = self.runtime.project_id
        attachments_dir = FileStorage.project_dir(pid) / "attachments" / sid
        attachments_dir.mkdir(parents=True, exist_ok=True)
        moved: list[str] = []
        for src in temp_paths:
            src_path = Path(src)
            if not src_path.exists():
                continue
            ext = src_path.suffix.lower().lstrip(".") or "jpg"
            dest = attachments_dir / f"{uuid.uuid4().hex}.{ext}"
            try:
                shutil.move(str(src_path), str(dest))
                moved.append(str(dest))
            except Exception:
                logger.exception("im_bridge: failed to move image %s to attachments", src)
                # fall back to copy + delete
                try:
                    shutil.copy2(str(src_path), str(dest))
                    moved.append(str(dest))
                except Exception:
                    logger.exception("im_bridge: failed to copy image %s", src)
        return moved

    async def _run_turn(self, sid: str, text: str, image_paths: list[str] | None = None) -> tuple[str, list[str]]:
        """Run one user turn — same path as desktop ``create_turn``.

        Returns ``(reply_text, image_paths_to_send)``. The image list is
        scraped from the runtime's ``tool_end`` events: tools that emit
        images (e.g. screenshot / image-generation) record them under
        ``event.output.image_url`` (see ``runtime.py`` tool_end shape),
        and the IM adapter sends each as a separate message after the
        final text reply.

        Sets ``turn_task`` / ``cancel_token`` on the session so the desktop
        stop button and the in-flight-turn 429 check both work if the user
        opens this IM session while it's replying.

        Also writes a turn record into the shared ``SessionStore`` so that
        the desktop UI's ``GET /sessions/{sid}/turns`` sees this turn as
        running/finished, the same as a desktop-initiated turn. Without this,
        opening an IM-driven session while the model is running shows an idle
        composer instead of a stop button.
        """
        session = self.runtime._get_session(sid)
        if session.turn_task is not None and not session.turn_task.done():
            raise RuntimeError("turn_already_running")
        # Tag the session with its IM channel so the runtime surfaces it in
        # the system prompt — the model then knows send_file delivers here.
        _route = self._sid_route.get(sid) or {}
        if _route.get("channel"):
            session.im_channel = _route["channel"]

        # Create a SessionStore turn record so the desktop UI can observe
        # this turn's status (running/done/cancelled/failed). The record
        # lives in memory only, like desktop turns.
        turn_id = str(uuid.uuid4())
        store_session = self.store._ensure_loaded(sid)
        if "turns" not in store_session:
            store_session["turns"] = []
        turn_record: dict[str, Any] = {"id": turn_id, "status": "running", "events": [], "result": None}
        store_session["turns"].append(turn_record)

        token = CancellationToken()
        session.cancel_token = token
        task = asyncio.current_task()
        session.turn_task = task
        try:
            content_parts: list[Any] = []
            if text:
                content_parts.append({"type": "text", "text": text})
            for path in image_paths or []:
                content_parts.append({"type": "image_url", "image_url": {"url": path}})
            if not content_parts:
                content_parts.append({"type": "text", "text": ""})
            _, result, events = await self.runtime.chat_with_events(
                [ChatMessage(role="user", content=content_parts)],
                session_id=sid,
            )
            turn_record["events"] = events
            turn_record["result"] = {
                "role": result.role,
                "content": result.content,
                "finish_reason": result.finish_reason,
            }
            # A token-cancelled model loop returns a normal ChatResult with
            # finish_reason == "cancelled".
            turn_record["status"] = "cancelled" if result.finish_reason == "cancelled" else "done"
            reply = _format_reply(result, events)
            # Pull image references out of the reply text too: image tools are
            # often referenced inline as markdown ``![alt](sandbox:/path)``
            # rather than (only) as tool_end image events. We strip the
            # markdown from the text (it would show as raw ``![...]`` on IM)
            # and send each resolved image as a separate message.
            reply, md_images = _extract_markdown_images(reply)
            output_images = _dedupe_images(_collect_output_images(events) + md_images)
            return reply, output_images
        except asyncio.CancelledError:
            turn_record["status"] = "cancelled"
            # The runtime's own token-cancelled path emits turn_cancelled,
            # but when this task is cancelled externally (e.g. desktop
            # /cancel or the IM /stop command) the runtime loop may not have
            # a chance to emit it. Send it explicitly so the frontend exits
            # "running" state instead of hanging.
            with contextlib.suppress(Exception):
                await self.runtime._emit(sid, {"type": "turn_cancelled"})
            raise
        except Exception as exc:
            logger.exception("im_bridge: turn failed for %s", sid)
            turn_record["status"] = "failed"
            turn_record["error"] = {"message": str(exc), "class": exc.__class__.__name__}
            raise
        finally:
            cur = self.runtime._sessions.get(sid)
            if cur:
                if cur.cancel_token is token:
                    cur.cancel_token = None
                if cur.turn_task is task:
                    cur.turn_task = None

    # -- HTTP-facing surface ------------------------------------------------

    def list_channels(self) -> list[Dict[str, Any]]:
        out: list[Dict[str, Any]] = []
        for name in ADAPTERS:
            cfg = self.config.channels.get(name)
            if cfg is None:
                continue
            adapter = self._adapters.get(name)
            if adapter:
                status = adapter.status()
            else:
                status = {
                    "channel": name,
                    "state": "disconnected",
                    "display_name": name,
                    "account_id": cfg.account_id or "",
                    "error": None,
                }
            out.append({
                "name": name,
                "enabled": cfg.enabled,
                "configured": cfg.configured(),
                **{k: v for k, v in status.items() if k != "channel"},
            })
        return out

    async def start_channel(self, name: str, fields: Dict[str, Any]) -> Dict[str, Any]:
        if name not in ADAPTERS:
            return {"error": "unknown_channel", "message": f"unknown channel: {name}"}
        cfg = self.config.channels[name]
        # Merge provided credential fields (secrets may come as "" if the
        # frontend redacted them — only overwrite when a real value is given).
        for key in ("app_id", "app_secret", "account_id", "bot_token", "gateway_url", "proxy_url"):
            if key in fields:
                val = fields[key]
                if isinstance(val, str) and val and not val.startswith("•"):
                    setattr(cfg, key, val)
        cfg.enabled = True
        self.config.save()
        try:
            await self._start_adapter(name)
        except Exception as exc:
            logger.exception("im_bridge: start_channel %s failed", name)
            cfg.enabled = False
            self.config.save()
            return {"error": "start_failed", "message": str(exc)}
        if self._adapters[name].status().get("state") == "error":
            cfg.enabled = False
            self.config.save()
            return {"error": "start_failed", "message": self._adapters[name].status().get("error") or "连接失败"}
        return {"ok": True, "status": self._adapters[name].status()}

    async def stop_channel(self, name: str) -> Dict[str, Any]:
        cfg = self.config.channels.get(name)
        if cfg:
            cfg.enabled = False
            self.config.save()
        adapter = self._adapters.pop(name, None)
        if adapter:
            await adapter.stop()
        return {"ok": True}

    def channel_status(self, name: str) -> Dict[str, Any]:
        adapter = self._adapters.get(name)
        if adapter:
            return adapter.status()
        cfg = self.config.channels.get(name)
        return {
            "channel": name,
            "state": "disconnected",
            "display_name": name,
            "account_id": cfg.account_id if cfg else "",
            "error": None,
        }

    def get_config(self) -> Dict[str, Any]:
        return self.config.to_public_dict()

    def update_config(self, updates: Dict[str, Any]) -> Dict[str, Any]:
        if "default_workspace" in updates:
            self.config.default_workspace = updates["default_workspace"] or None
        if "allowed_senders" in updates and isinstance(updates["allowed_senders"], list):
            self.config.allowed_senders = [str(s) for s in updates["allowed_senders"]]
        self.config.save()
        return self.config.to_public_dict()


def _format_reply(result: Any, events: list[Dict[str, Any]] | None) -> str:
    """Return only the final model answer for IM replies.

    The previous version included reasoning and tool summaries, but IM
    channels are too noisy for long structured output. Keep replies short
    and readable.
    """
    answer = _text_of(result.content if hasattr(result, "content") else result)
    return answer or "已处理"


def _collect_output_images(events: list[Dict[str, Any]] | None) -> list[str]:
    """Pull image paths out of a turn's runtime events.

    Tools that emit images (e.g. screenshot, image generation) record the
    path under ``event.output.image_url`` (``type == "image"`` payload;
    see ``runtime.py``'s ``tool_end`` shape). We keep the order the model
    produced them so the IM adapter sends them in the same order they
    appeared in the assistant's reply.
    """
    if not events:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for ev in events:
        if ev.get("type") != "tool_end":
            continue
        output = ev.get("output")
        if not isinstance(output, dict):
            continue
        if output.get("type") != "image":
            continue
        path = output.get("image_url") or ""
        if path and path not in seen:
            seen.add(path)
            out.append(path)
    return out


# Markdown image: ![alt text](url). Non-greedy on alt; url has no closing paren.
_MD_IMAGE_RE = re.compile(r"!\[([^\]]*)\]\(\s*([^)\s]+)\s*\)")


def _resolve_image_url(url: str) -> str:
    """Turn a markdown image URL into a reference ``decode_image_ref`` can read.

    Image tools are referenced inline with custom/local schemes the desktop
    frontend understands but IM adapters don't — e.g.
    ``sandbox:/abs/path.png`` or ``file:///abs/path.png``. We strip those to
    the underlying local path. ``data:`` URLs pass through unchanged; bare
    paths pass through unchanged (existence is checked by the caller).
    """
    if not url:
        return ""
    if url.startswith("data:") or url.startswith("http://") or url.startswith("https://"):
        return url
    for scheme in ("file://", "file:", "sandbox:"):
        if url.startswith(scheme):
            return url[len(scheme):]
    return url


def _is_sendable_image(ref: str) -> bool:
    """Whether an adapter can actually upload this ref right now.

    ``data:`` URLs are always sendable; local paths only if the file exists.
    ``http(s)`` URLs are left out (the adapters don't fetch remote URLs) —
    they fall back to plain text so the link stays visible.
    """
    if not ref:
        return False
    if ref.startswith("data:"):
        return True
    if ref.startswith("http://") or ref.startswith("https://"):
        return False
    return os.path.exists(ref)


def _extract_markdown_images(text: str | None) -> tuple[str, list[str]]:
    """Strip sendable markdown images from ``text`` and return their refs.

    Returns ``(cleaned_text, image_refs)``. Each sendable image (local file or
    data URL) is removed from the text — the bridge sends it as a separate
    message, and the raw ``![alt](url)`` would otherwise render as literal
    text on IM. Non-sendable images (e.g. remote URLs) are replaced with their
    alt text / URL so the user still sees a readable reference.
    """
    if not text:
        return "", []
    images: list[str] = []

    def _sub(match: re.Match[str]) -> str:
        alt, url = match.group(1), match.group(2).strip()
        ref = _resolve_image_url(url)
        if ref and _is_sendable_image(ref):
            images.append(ref)
            return ""
        return alt or url

    cleaned = _MD_IMAGE_RE.sub(_sub, text)
    # Tidy whitespace left where an image was removed.
    cleaned = re.sub(r"[ \t]+\n", "\n", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    return cleaned, images


def _dedupe_images(items: list[str]) -> list[str]:
    """Order-preserving dedupe — a tool image and its inline markdown ref
    can point at the same file; don't send it twice."""
    seen: set[str] = set()
    out: list[str] = []
    for it in items:
        if it and it not in seen:
            seen.add(it)
            out.append(it)
    return out


def _truncate(text: str, max_len: int) -> str:
    """Truncate text with an ellipsis, preserving line breaks."""
    if len(text) <= max_len:
        return text
    return text[:max_len - 3] + "..."


def _text_of(content: Any) -> str:
    """Coerce a ChatResult.content (str | list of parts) into plain text."""
    if isinstance(content, str):
        # 去掉首尾空白：模型 prompt 拼接或 SDK 解析常常会让 text block 以
        # 换行开头（Anthropic 上比较常见），不 strip 会导致 IM 回复有"莫名"
        # 的前导空行。
        return content.strip()
    if isinstance(content, list):
        parts = []
        for p in content:
            if isinstance(p, str):
                parts.append(p)
            elif isinstance(p, dict):
                parts.append(str(p.get("text") or p.get("content") or ""))
        return "".join(parts).strip()
    return str(content)
