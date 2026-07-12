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
from ziva.transports.im_bridge.adapters.base import BaseAdapter
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

    # -- lifecycle ----------------------------------------------------------

    async def start(self) -> None:
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
            try:
                reply = await self._run_turn(sid, msg.text, image_paths)
            except Exception as exc:
                logger.exception("im_bridge: turn failed for %s", msg.route_key)
                reply = f"[ziva error] {exc}"
            adapter = self._adapters.get(msg.channel)
            if adapter:
                try:
                    await adapter.send_message(OutgoingMessage(chat_id=msg.chat_id, text=reply))
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
            if not arg:
                lines = [f"● {m}" if m == current else f"○ {m}" for m in available]
                return f"可用模型（当前: {current}）：\n" + "\n".join(lines) + "\n\n用法: /model <名称>"
            if arg not in available:
                return f"未知模型: {arg}\n可用: {', '.join(available)}"
            sid = self.config.route_for(msg.route_key)
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
        and do not change session behavior."""
        ws = self.config.default_workspace or str(self.runtime.workspace_root)
        FileStorage.update_session(self.runtime.project_id, sid, {
            "source": "im-bridge",
            "channel": msg.channel,
            "chat_id": msg.chat_id,
            "sender_id": msg.sender_id,
            "sender_name": msg.sender_name,
            "workspace_root": ws,
            "name": f"{msg.sender_name} · {msg.channel}",
            "time": {"created": int(time.time() * 1000), "updated": int(time.time() * 1000)},
        })

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

    async def _run_turn(self, sid: str, text: str, image_paths: list[str] | None = None) -> str:
        """Run one user turn — same path as desktop ``create_turn``.

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
            return _format_reply(result, events)
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
