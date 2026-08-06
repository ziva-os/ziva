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
import json
import logging
import os
import re
import shutil
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List

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
        # Permission requests forwarded to IM: request_id -> routing info.
        # Same FIFO per-chat queue as ask_user so replies match in order.
        self._pending_permissions: Dict[str, Dict[str, Any]] = {}
        self._pending_permission_chat_index: Dict[str, list[str]] = {}
        # /restart coordination: True while a graceful execvp is in progress.
        # Concurrent /restart commands are rejected with a "Restart already in
        # progress" reply so the pending-payload file is written exactly once.
        self._restart_in_flight: bool = False

    # -- lifecycle ----------------------------------------------------------

    async def start(self) -> None:
        # Subscribe once: the ask_user tool emits a question, the bridge
        # pushes it to the IM chat, and the user's reply is matched back
        # to the question via the pending_questions map in _handle.
        try:
            self.runtime.on_ask_user(self._on_ask_user_question)
            self.runtime.on_send_file(self._on_send_file)
            self.runtime.on_permission_request(self._on_permission_request)
        except Exception:
            logger.exception("im_bridge: failed to register runtime callbacks")
        for name, cfg in self.config.channels.items():
            if cfg.enabled and name in ADAPTERS:
                try:
                    await self._start_adapter(name)
                except Exception:
                    logger.exception("im_bridge: failed to start channel %s", name)
                    # Leave enabled=True. Flipping it to False here used to
                    # discard the user's saved intent on a transient failure,
                    # so the next restart skipped the channel entirely and the
                    # user had to re-enter credentials to bring it back. The
                    # adapter's status already reflects the error; a restart
                    # retries from the saved config automatically.

        # After all adapters have had a chance to connect, check whether
        # the previous process asked us to send restart notifications. The
        # old process writes ~/.ziva/.restart_pending.json right before
        # execvp; we read it here and ack each target that is still wired up.
        # See docs/im-restart.md §10 for the schema and rationale.
        await self._consume_restart_pending()

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
        # Permission reply interception (checked before ask_user so a
        # pending permission doesn't get swallowed by an ask_user queue).
        perm_queue = self._pending_permission_chat_index.get(msg.chat_id) or []
        perm_id = perm_queue[0] if perm_queue else None
        if perm_id and perm_id in self._pending_permissions:
            perm_queue.pop(0)
            if not perm_queue:
                self._pending_permission_chat_index.pop(msg.chat_id, None)
            self._pending_permissions.pop(perm_id, None)
            answer = (msg.text or "").strip().lower()
            if answer in ("a", "always", "总是", "全部允许"):
                reply_action = "always_session"
            elif answer in ("y", "yes", "允许", "是", "ok", "好"):
                reply_action = "once"
            else:
                reply_action = "reject"
            from ziva.permissions import get_permission_manager
            pm = get_permission_manager()
            try:
                pm.reply(perm_id, reply_action)
            except Exception:
                logger.exception("im_bridge: permission reply failed for %s", perm_id)
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
            file_paths = self._persist_files(sid, msg.files)
            text = msg.text
            if file_paths:
                from pathlib import Path as _P
                note = "".join(
                    f"\n[用户发送的文件：{_P(p).name or p}，已保存到 {p}]"
                    for p in file_paths
                )
                text = (text + note) if text else note.strip()
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
                reply, output_images = await self._run_turn(sid, text, image_paths)
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
            # Surface the new session's model + effort so the IM user can see
            # what they're starting with (mirrors the desktop composer, which
            # always shows both). A fresh session has no per-session override,
            # so default effort to the model's highest supported level — same
            # rule the desktop UI applies — and persist it so the backend
            # agrees from the first turn.
            try:
                _cfg = self.runtime.config
            except Exception:
                _cfg = {}
            _mc = _cfg.get("model", {}) if isinstance(_cfg, dict) else {}
            _mn = _mc.get("name", "unknown")
            _pn = _mc.get("provider_name")
            _providers = _cfg.get("providers", []) if isinstance(_cfg, dict) else []
            if not _pn:
                _mn_l = (_mn or "").lower()
                for _p in _providers:
                    if any((m.get("name") or "").lower() == _mn_l for m in _p.get("models", [])):
                        _pn = _p.get("name")
                        break
            _levels = self.runtime._effort_levels_for_model(_mn) if _mn else []
            _effort = _levels[-1] if _levels else (_mc.get("thinking_mode") or "disabled")
            try:
                FileStorage.update_session(self.runtime.project_id, new_sid, {
                    "model_name": _mn, "provider_name": _pn, "thinking_mode": _effort,
                })
                sess = self.runtime._get_session(new_sid)
                sess.model_name = _mn
                sess.provider_name = _pn
                sess.thinking_mode = _effort
            except Exception:
                logger.exception("im_bridge: /new failed to pin model/effort")
            _shown = f"{_pn}:{_mn}" if _pn else _mn
            return (f"已开启新会话 `{new_sid[:8]}`{old_hint}。\n"
                    f"模型: {_shown} · effort: {_effort}")

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
            providers = cfg.get("providers", []) if isinstance(cfg, dict) else []
            # provider:model entries so same-named models across providers are
            # distinguishable (e.g. glm:glm-5.2 vs opencode:glm-5.2).
            entries: list[tuple[str, str]] = []
            for p in providers:
                for m in p.get("models", []):
                    if m.get("name"):
                        entries.append((p.get("name", ""), m["name"]))
            available = [f"{pn}:{mn}" for pn, mn in entries]
            sid = self.config.route_for(msg.route_key)
            current_mn = model_cfg.get("name", "unknown")
            current_pn: str | None = model_cfg.get("provider_name")
            if sid:
                _sess_meta = FileStorage.get_session(self.runtime.project_id, sid) or {}
                current_mn = _sess_meta.get("model_name") or current_mn
                current_pn = _sess_meta.get("provider_name") or current_pn
            # Resolve the owning provider when none is recorded so the current
            # marker lines up with the "provider:model" entries above (e.g.
            # the global default MiniMax-M3 shows as "MiniMax:MiniMax-M3").
            if not current_pn:
                _mn = (current_mn or "").lower()
                for p in providers:
                    if any((m.get("name") or "").lower() == _mn for m in p.get("models", [])):
                        current_pn = p.get("name")
                        break
            current = f"{current_pn}:{current_mn}" if current_pn else current_mn
            if not available:
                available = [current]
            if not arg:
                lines = [f"● {e}" if e == current else f"○ {e}" for e in available]
                return f"可用模型（当前: {current}）：\n" + "\n".join(lines) + "\n\n用法: /model <provider:model>"
            # Accept "provider:model" (exact) or bare "model" (first-wins).
            want_pn, want_mn = (arg.split(":", 1) + [""])[:2] if ":" in arg else (None, arg)
            match = next(((pn, mn) for pn, mn in entries
                          if (not want_pn or pn == want_pn) and mn.lower() == want_mn.lower()), None)
            if not match:
                return f"未知模型: {arg}\n可用: {', '.join(available)}"
            if not sid:
                return "请先发送一条消息再切换模型。"
            try:
                FileStorage.update_session(self.runtime.project_id, sid,
                                           {"model_name": match[1], "provider_name": match[0]})
            except Exception as exc:
                logger.exception("im_bridge: /model failed")
                return f"[ziva error] 切换模型失败: {exc}"
            sess = self.runtime._get_session(sid)
            sess.model_name = match[1]
            sess.provider_name = match[0]
            with contextlib.suppress(Exception):
                await self.runtime._emit(sid, {"type": "model_changed",
                                               "model_name": match[1], "provider_name": match[0]})
            return f"已将模型切换为 {match[0]}:{match[1]}。"

        if cmd == "/effort":
            _EFFORTS = ("disabled", "low", "medium", "high", "xhigh", "max")
            try:
                cfg = self.runtime.config
            except Exception:
                cfg = {}
            model_cfg = cfg.get("model", {}) if isinstance(cfg, dict) else {}
            sid = self.config.route_for(msg.route_key)
            current = model_cfg.get("thinking_mode", "disabled")
            if sid:
                _sess_meta = FileStorage.get_session(self.runtime.project_id, sid) or {}
                current = _sess_meta.get("thinking_mode") or current
            if not arg:
                return f"当前 effort: {current}\n选项: {', '.join(_EFFORTS)}"
            if arg not in _EFFORTS:
                return f"未知 effort: {arg}\n选项: {', '.join(_EFFORTS)}"
            if not sid:
                return "请先发送一条消息再切换 effort。"
            try:
                FileStorage.update_session(self.runtime.project_id, sid, {"thinking_mode": arg})
            except Exception as exc:
                logger.exception("im_bridge: /effort failed")
                return f"[ziva error] 切换 effort 失败: {exc}"
            sess = self.runtime._get_session(sid)
            sess.thinking_mode = arg
            with contextlib.suppress(Exception):
                await self.runtime._emit(sid, {"type": "model_changed", "thinking_mode": arg})
            return f"已将 effort 切换为 {arg}。"

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

        # --- /restart: replace the current process with a fresh ziva instance.
        # The handler writes the notify target into the shared restart payload
        # and then triggers execvp. The "✅ Restarted in Xs" follow-up is sent
        # by the new process once it boots — see docs/im-restart.md §10.
        if cmd == "/restart":
            return await self._handle_restart(msg)

        return None  # not a recognized command — let the LLM handle it.

    # -- /restart -------------------------------------------------------------

    async def _handle_restart(self, msg: IncomingMessage) -> str:
        """Slash-command handler for ``/restart``.

        Aligns with the Ziva desktop's restart path: when the Electron
        desktop is running, sends "restart" to its unix socket so the
        whole app does ``app.relaunch() + app.quit()`` — same as the
        desktop chat's ``/restart`` and ``ziva desktop restart``. Falls
        back to ``runtime._graceful_execvp()`` (the old behaviour) only
        when the desktop socket is missing, e.g. a bare ``ziva desktop
        serve`` started outside Electron.

        1. Rejects concurrent /restart commands (one restart per restart).
        2. Persists the "who to notify on completion" target into the
           restart payload file. The new process reads it on boot and
           sends a "✅ Restarted in Xs" message back to the same chat —
           see ``docs/im-restart.md`` §10.
        3. Either signals Electron to relaunch (preferred, stable) or
           execvp's the current python process (fallback for non-Electron
           usage).

        The reply text is returned synchronously so the IM adapter can
        send it before the process exits; in practice it usually arrives
        just before the new process boots.
        """
        if self._restart_in_flight:
            return "Restart already in progress."

        # Lock first so a second concurrent /restart from a different sender
        # is rejected even if it races us while we're awaiting the relaunch.
        self._restart_in_flight = True

        # Single notify target: the chat that asked. (If multiple channels
        # are configured and the user wants notifications on more than one,
        # they can send /restart again after the new process boots and the
        # manual session UI will surface it.)
        notify_targets: List[Dict[str, str]] = [
            {"channel": msg.channel, "chat_id": msg.chat_id}
        ]
        requested_at = int(time.time())

        # Build the payload the new process will read. Versioned so future
        # schema changes can ignore unknown payloads gracefully.
        payload = {
            "version": 1,
            "requested_at_unix": requested_at,
            "notify": notify_targets,
        }

        try:
            await self._do_restart(payload)
        except Exception as exc:
            # Roll back the in-flight flag so a follow-up /restart can retry.
            self._restart_in_flight = False
            logger.exception("im_bridge: /restart failed")
            return f"[ziva error] restart failed: {exc}"

        # If _do_restart returned instead of relaunching, the new process is
        # alive somewhere else and we're still here — give a useful reply.
        return "Restart scheduled; new process will send confirmation."

    async def _do_restart(self, payload: Dict[str, Any]) -> None:
        """Trigger a restart of the Ziva backend.

        Prefers the Electron desktop's restart socket (whole-app
        ``app.relaunch() + app.quit()``) when present — same path the
        desktop chat ``/restart`` and ``ziva desktop restart`` CLI use,
        so all four entry points converge on one mechanism. Falls back
        to ``runtime._graceful_execvp()`` when running bare (no
        Electron), so the daemon survives the restart in-place.

        Always writes the pending-payload file first so the new process
        can ack the chat with "Restarted in Xs" via
        ``_consume_restart_pending``.
        """
        sock_path = self._RESTART_SOCKET_PATH

        # 1. Persist the notify payload regardless of which restart path we
        #    take — the new process reads it on boot.
        await self._persist_restart_payload(payload)

        # 2. Pick the restart path. Electron socket preferred; bare
        #    `ziva desktop serve` falls back to in-process execvp.
        if sock_path.exists():
            logger.warning("im_bridge: /restart → Electron socket (whole-app relaunch)")
            await self._send_restart_to_socket(sock_path)
        else:
            logger.warning("im_bridge: /restart → runtime._graceful_execvp (no Electron)")
            await self.runtime._graceful_execvp(
                reason="im_restart",
                pending_payload=payload,
            )

    async def _persist_restart_payload(self, payload: Dict[str, Any]) -> None:
        """Write ``payload`` to ``~/.ziva/.restart_pending.json``.

        New process reads this on boot (``_consume_restart_pending``) and
        sends "✅ Restarted in Xs" back to the requesting chat. Same file
        format regardless of whether the restart came from IM, the
        desktop chat, or the CLI.
        """
        path = self._RESTART_PENDING_PATH
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(payload), encoding="utf-8")
        except OSError as exc:
            # Persist failure is not fatal — the restart itself can still
            # proceed; the user just won't get the "✅ Restarted in Xs" ack.
            logger.warning("im_bridge: failed to persist restart payload (%s)", exc)

    async def _send_restart_to_socket(self, sock_path: Path) -> None:
        """Send ``restart\\n`` to the Electron main process's restart
        listener. The main process then does ``app.relaunch() +
        app.quit()`` — see ``electron/main.ts::startRestartListener``.
        Blocking I/O so we hop to a thread.
        """
        import socket as _socket

        def _send() -> None:
            with _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM) as s:
                s.settimeout(3.0)
                s.connect(str(sock_path))
                s.sendall(b"restart\n")

        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, _send)

    # Restart payload layout — see docs/im-restart.md §10.3 for the rationale.
    # Kept as a module-level constant so tests can reference the same path.
    _RESTART_PENDING_PATH = Path.home() / ".ziva" / ".restart_pending.json"
    _RESTART_PENDING_STALE_SECONDS = 300  # 5 min — anything older is stale
    # Unix socket the Electron main process listens on for CLI / IM restart
    # signals. Tests override this to tmp_path so they don't accidentally
    # hit a real Electron instance.
    _RESTART_SOCKET_PATH = Path.home() / ".ziva" / "restart.sock"

    async def _consume_restart_pending(self) -> None:
        """Send "✅ Restarted in Xs" to chat(s) that requested a restart.

        Reads ``~/.ziva/.restart_pending.json`` (written by the previous
        process right before ``execvp``). Stale or malformed payloads are
        silently discarded so a corrupted file never blocks startup.
        """
        path = self._RESTART_PENDING_PATH
        if not path.exists():
            return
        logger.info("im_bridge: found restart pending payload at %s", path)

        # Parse + mtime check before doing any work. If the payload is
        # bad we delete it so it doesn't poison the next startup.
        try:
            stat = path.stat()
        except OSError:
            return

        age = time.time() - stat.st_mtime
        if age > self._RESTART_PENDING_STALE_SECONDS:
            logger.warning(
                "im_bridge: ignoring stale restart_pending file (%.0fs old)", age
            )
            try:
                path.unlink()
            except OSError:
                pass
            return

        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("im_bridge: malformed restart_pending file (%s); deleting", exc)
            try:
                path.unlink()
            except OSError:
                pass
            return

        if not isinstance(payload, dict) or payload.get("version") != 1:
            logger.warning(
                "im_bridge: unknown restart_pending version %r; deleting",
                payload.get("version") if isinstance(payload, dict) else None,
            )
            try:
                path.unlink()
            except OSError:
                pass
            return

        requested_at = int(payload.get("requested_at_unix") or 0)
        duration = max(0, int(time.time()) - requested_at)
        if duration >= 120:
            text = f"✅ Restarted in {duration // 60}m{duration % 60}s"
        else:
            text = f"✅ Restarted in {duration}s"

        sent_any = False
        attempted = False
        for target in payload.get("notify", []) or []:
            channel = target.get("channel", "")
            chat_id = target.get("chat_id", "")
            if not channel or not chat_id:
                continue
            adapter = self._adapters.get(channel)
            if not adapter:
                # Channel is gone from config — no point retrying next boot.
                logger.warning(
                    "im_bridge: restart notify skipped — channel %r not active", channel
                )
                continue
            attempted = True
            # Retry briefly so a not-yet-connected adapter (most IM adapters
            # return from start() before the upstream handshake completes)
            # has a chance to come up before we give up.
            last_exc: Exception | None = None
            for attempt in range(4):
                try:
                    await adapter.send_message(OutgoingMessage(chat_id=chat_id, text=text))
                    sent_any = True
                    logger.info(
                        "im_bridge: restart ack sent to %s/%s after %d attempt(s)",
                        channel, chat_id, attempt + 1,
                    )
                    break
                except Exception as exc:
                    last_exc = exc
                    if attempt < 3:
                        await asyncio.sleep(0.5)
            if not sent_any and last_exc is not None:
                logger.warning(
                    "im_bridge: failed to send restart ack to %s/%s after 4 attempts: %s",
                    channel, chat_id, last_exc,
                )

        # Decision matrix:
        #   attempted=False (no matching channels)  → delete (config-removed, won't retry)
        #   attempted=True,  sent_any=True          → delete (delivered)
        #   attempted=True,  sent_any=False         → KEEP (transient — retry next boot)
        if not attempted or sent_any:
            try:
                path.unlink()
            except OSError:
                logger.warning("im_bridge: failed to delete restart_pending after handling")
        else:
            logger.warning(
                "im_bridge: restart_pending kept for retry on next startup "
                "(adapter %r exists but all sends failed)",
                [t.get("channel") for t in payload.get("notify", []) or []],
            )

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

    def _on_permission_request(self, req_info: Dict[str, Any]) -> None:
        """Forward a permission request to the originating IM chat.

        Called when a tool needs approval in ``suggest`` mode. We look
        up the routing the bridge recorded when the IM message was first
        routed to a session, format a short prompt, and push it. The
        user's next message in that chat is intercepted as the
        approve/deny reply instead of being fed to the model.
        """
        session_id = req_info.get("sessionID", "")
        route = self._sid_route.get(session_id) or {}
        chat_id = route.get("chat_id", "")
        channel = route.get("channel", "")
        if not chat_id or not channel:
            return
        adapter = self._adapters.get(channel)
        if not adapter:
            return
        req_id = req_info.get("id", "")
        tool = (req_info.get("tool") or {})
        tool_name = tool.get("name", "unknown")
        args = tool.get("arguments", {})
        self._pending_permissions[req_id] = {
            "sid": session_id,
            "chat_id": chat_id,
            "channel": channel,
        }
        self._pending_permission_chat_index.setdefault(chat_id, []).append(req_id)
        # Format: 🔧 shell(args) → reply y/n
        arg_str = ""
        if isinstance(args, dict):
            for k in ("command", "file_path", "path"):
                if k in args:
                    arg_str = str(args[k])[:60]
                    break
        text = f"🔧 `{tool_name}`"
        if arg_str:
            text += f"({arg_str})"
        text += "\n回复 `y` 允许一次 · `a` 总是允许 · `n` 拒绝"
        asyncio.create_task(self._send_permission_prompt(adapter, chat_id, text, channel))

    async def _send_permission_prompt(self, adapter: Any, chat_id: str, text: str, channel: str) -> None:
        try:
            await adapter.send_message(OutgoingMessage(chat_id=chat_id, text=text))
        except Exception:
            logger.exception("im_bridge: failed to push permission prompt to %s", channel)

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
            self._pending_permission_chat_index.pop(chat_id, None)
        self._pending_questions = {
            cid: v for cid, v in self._pending_questions.items()
            if v.get("sid") != sid
        }
        self._pending_permissions = {
            rid: v for rid, v in self._pending_permissions.items()
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

    def _persist_files(self, sid: str, temp_paths: list[str]) -> list[str]:
        """Move adapter-downloaded files into the session attachments dir,
        keeping the original filename (collision-safe)."""
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
            dest = attachments_dir / src_path.name
            if dest.exists():
                dest = attachments_dir / f"{src_path.stem}_{uuid.uuid4().hex[:6]}{src_path.suffix}"
            try:
                shutil.move(str(src_path), str(dest))
                moved.append(str(dest))
            except Exception:
                logger.exception("im_bridge: failed to move file %s to attachments", src)
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
            # Keep enabled=True: the user just asked to connect, so a failure
            # should leave the channel retryable (next restart, or a one-click
            # reconnect with the saved credentials) rather than silently
            # disabling it.
            return {"error": "start_failed", "message": str(exc)}
        if self._adapters[name].status().get("state") == "error":
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
