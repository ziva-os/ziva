"""Telegram adapter — official Bot API via long polling (no SDK, aiohttp only).

Flow:
  * ``start()`` spawns a background task looping ``getUpdates`` (long poll).
  * Each text message becomes an :class:`IncomingMessage` handed to the bridge.
  * ``send_message`` POSTs to ``sendMessage``.

Requires a bot token from @BotFather. Telegram long polling blocks the
request for up to ``timeout`` seconds waiting for updates, so it runs in its
own task; ``stop()`` cancels that task.

Auto-reconnect (we own the loop, unlike Feishu which delegates to lark-oapi):
after ``RECONNECT_AFTER_FAILURES`` consecutive ``getUpdates`` failures, the
poll loop exits and ``_reconnect()`` probes ``getMe`` up to
``RECONNECT_MAX_ATTEMPTS`` times at a fixed ``RECONNECT_INTERVAL`` (no
backoff). On success a fresh poll loop is started; on exhaustion the state
goes to ``error`` and the user reconnects manually.
"""

from __future__ import annotations

import asyncio
import logging
import tempfile
import uuid
from pathlib import Path
from typing import Any, Dict

import aiohttp
from aiohttp import ClientConnectorError

from ziva.transports.im_bridge.adapters.base import BaseAdapter, decode_image_ref, _bytesio, classify_media, _safe_filename
from ziva.transports.im_bridge.models import IncomingMessage, OutgoingMessage

logger = logging.getLogger(__name__)

API_BASE = "https://api.telegram.org/bot{token}/{method}"
POLL_TIMEOUT = 30  # seconds the long-poll holds open

# Auto-reconnect knobs. We don't use exponential backoff — a fixed 3s
# interval matches the original retry cadence and is enough to ride out
# brief network blips without piling up requests against an already-sick
# connection.
RECONNECT_AFTER_FAILURES = 5   # consecutive getUpdates failures → reconnect
RECONNECT_MAX_ATTEMPTS = 6     # getMe probes per reconnect session
RECONNECT_INTERVAL = 3.0       # seconds between probes (fixed, no backoff)


class TelegramAdapter(BaseAdapter):
    channel = "telegram"

    def __init__(self, config, on_message) -> None:
        super().__init__(config, on_message)
        self._token = config.bot_token or ""
        self._proxy = config.proxy_url or None
        self._session: aiohttp.ClientSession | None = None
        self._task: asyncio.Task | None = None
        self._offset = 0
        self._bot_user_id = ""
        # Per-chat typing-indicator loops. send_typing spawns one, stop_typing
        # cancels it. Kept on the adapter so multiple chats can each have
        # their own typing task.
        self._typing_tasks: Dict[str, asyncio.Task] = {}
        # Auto-reconnect bookkeeping.
        self._consecutive_failures = 0
        self._stop_flag = False

    @property
    def account_id(self) -> str:
        # The bot's own user id — discovered on start via getMe — is the
        # stable identity used in route keys.
        return self._bot_user_id or self.config.account_id or ""

    async def start(self) -> None:
        if not self._token:
            self._set_state("error", "missing bot_token")
            return
        self._stop_flag = False
        self._consecutive_failures = 0
        self._session = aiohttp.ClientSession(trust_env=True)
        self._set_state("connecting")
        try:
            # Initial identity check with a timeout long enough for slow VPN /
            # proxy paths, but short enough that the UI doesn't wait forever if
            # Telegram is unreachable.
            me = await self._call("getMe", http_timeout=30)
            self._bot_user_id = str(me.get("id", ""))
            self.config.account_id = self._bot_user_id
            self._set_state("connected")
        except asyncio.TimeoutError:
            self._set_state("error", "连接 Telegram 超时，请检查本机网络是否能访问 api.telegram.org")
            await self._session.close()
            self._session = None
            return
        except ClientConnectorError:
            self._set_state("error", "无法连接到 Telegram 服务器，请检查网络或代理设置")
            await self._session.close()
            self._session = None
            return
        except RuntimeError as exc:
            # RuntimeError comes from _call when Telegram returns ok=false.
            err = str(exc).lower()
            if "not found" in err or "unauthorized" in err:
                self._set_state("error", "Bot Token 无效，请检查从 @BotFather 获取的 Token")
            else:
                self._set_state("error", f"Telegram 错误: {exc}")
            await self._session.close()
            self._session = None
            return
        except Exception as exc:
            self._set_state("error", f"连接 Telegram 失败: {exc}")
            await self._session.close()
            self._session = None
            return
        self._task = asyncio.create_task(self._poll_loop())

    async def stop(self) -> None:
        self._stop_flag = True
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None
        if self._session:
            await self._session.close()
            self._session = None
        self._set_state("disconnected")

    async def send_message(self, msg: OutgoingMessage) -> None:
        # Telegram caps messages at 4096 chars; chunk long replies. Skip when
        # the text is empty (e.g. a reply that was only an inline image, which
        # arrives separately via msg.images) so we don't send a blank bubble.
        text = (msg.text or "").strip()
        for i in range(0, len(text), 4000):
            await self._call("sendMessage", {
                "chat_id": msg.chat_id,
                "text": text[i:i + 4000],
            })
        # Send each image as a separate photo message via multipart upload.
        # Tool outputs arrive as data: URLs (see read_file / MCP), so decode
        # to bytes and upload a real file — passing the URL string would make
        # _call_multipart send it as a text field and Telegram would reject it.
        for image_ref in msg.images or []:
            if not image_ref:
                continue
            decoded = decode_image_ref(image_ref)
            if not decoded:
                logger.warning("telegram: could not decode image ref for send")
                continue
            data, filename = decoded
            try:
                await self._call_multipart("sendPhoto", {
                    "chat_id": str(msg.chat_id),
                    "photo": _bytesio(data, filename),
                })
            except Exception:
                logger.exception("telegram: sendPhoto failed for %s", filename)
        # Send each non-image file (video / archive / document) as a separate
        # message. Videos use sendVideo (Telegram auto-generates a thumbnail);
        # everything else uses sendDocument (preserves the file as-is).
        for file_path in msg.files or []:
            if not file_path:
                continue
            decoded = decode_image_ref(file_path)
            if not decoded:
                logger.warning("telegram: could not read file for send: %s", file_path)
                continue
            data, filename = decoded
            is_video = classify_media(file_path) == "video"
            method = "sendVideo" if is_video else "sendDocument"
            field = "video" if is_video else "document"
            try:
                await self._call_multipart(method, {
                    "chat_id": str(msg.chat_id),
                    field: _bytesio(data, filename),
                })
            except Exception:
                logger.exception("telegram: %s failed for %s", method, filename)

    async def send_typing(self, chat_id: str, message_id: str = "") -> None:
        """Best-effort "typing…" indicator.

        Telegram's ``sendChatAction`` expires after ~5s, so the bridge
        drives it as a background task that keeps re-sending every 4
        seconds until cancelled by ``stop_typing``. The task is
        intentionally swallow-all on errors — Telegram being unavailable
        should never surface as a typing-related failure in the IM bridge.
        """
        if not chat_id:
            return

        async def _loop() -> None:
            try:
                while True:
                    try:
                        await self._call(
                            "sendChatAction",
                            {"chat_id": chat_id, "action": "typing"},
                            http_timeout=10,
                        )
                    except Exception:
                        pass
                    await asyncio.sleep(4)
            except asyncio.CancelledError:
                return

        task = asyncio.create_task(_loop())
        self._typing_tasks[chat_id] = task

    async def stop_typing(self, chat_id: str) -> None:
        """Cancel the per-chat typing loop started by ``send_typing``."""
        task = self._typing_tasks.pop(chat_id, None)
        if task and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

    async def _call_multipart(self, method: str, fields: Dict[str, Any]) -> Any:
        """Call a Telegram Bot API method with multipart/form-data.

        Telegram's Bot API accepts a local file path (e.g. ``photo``,
        ``document``) via multipart, which we can't reach through the
        regular ``_call`` (which posts JSON). Fields whose value is a
        string is sent as a text part; anything else is treated as a
        file-like object (or a path that gets opened for reading).
        """
        assert self._session is not None
        url = API_BASE.format(token=self._token, method=method)
        form = aiohttp.FormData()
        for key, value in fields.items():
            if isinstance(value, str):
                form.add_field(key, value)
            else:
                # Treat as file: either a path or an open file object.
                if hasattr(value, "read"):
                    form.add_field(key, value, filename=getattr(value, "name", key))
                else:
                    form.add_field(key, open(str(value), "rb"), filename=str(value).rsplit("/", 1)[-1])
        async with self._session.post(
            url,
            data=form,
            timeout=aiohttp.ClientTimeout(total=60),
            proxy=self._proxy,
        ) as r:
            data = await r.json()
            if not data.get("ok"):
                raise RuntimeError(f"telegram {method}: {data.get('description')}")
            return data.get("result")

    # -- internals ----------------------------------------------------------

    async def _poll_loop(self) -> None:
        assert self._session is not None
        try:
            while True:
                try:
                    updates = await self._call("getUpdates", {
                        "offset": self._offset,
                        "timeout": POLL_TIMEOUT,
                    }, http_timeout=POLL_TIMEOUT + 10)
                    self._consecutive_failures = 0
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self._consecutive_failures += 1
                    logger.warning(
                        "telegram getUpdates failed (%d in a row): %s",
                        self._consecutive_failures, exc,
                    )
                    if self._consecutive_failures >= RECONNECT_AFTER_FAILURES:
                        logger.warning(
                            "telegram: %d consecutive failures, attempting reconnect",
                            self._consecutive_failures,
                        )
                        asyncio.create_task(self._reconnect())
                        return
                    await asyncio.sleep(3)
                    continue
                for upd in updates or []:
                    self._offset = max(self._offset, int(upd.get("update_id", 0)) + 1)
                    await self._handle_update(upd)
        except asyncio.CancelledError:
            pass

    async def _reconnect(self) -> None:
        """Tear down the aiohttp session and probe getMe up to
        RECONNECT_MAX_ATTEMPTS times at RECONNECT_INTERVAL. On success,
        restart the poll loop. On exhaustion, surface ``error`` so the user
        can reconnect manually.
        """
        if self._stop_flag:
            return
        self._set_state("connecting", "Telegram 连接中断，正在重连…")
        # Tear down old session; close failures are non-fatal.
        try:
            if self._session and not self._session.closed:
                await self._session.close()
        except Exception:
            pass
        self._session = None
        last_err = ""
        for attempt in range(1, RECONNECT_MAX_ATTEMPTS + 1):
            if self._stop_flag:
                return
            await asyncio.sleep(RECONNECT_INTERVAL)
            if self._stop_flag:
                return
            try:
                self._session = aiohttp.ClientSession(trust_env=True)
                me = await self._call("getMe", http_timeout=15)
                self._bot_user_id = str(me.get("id", ""))
                self.config.account_id = self._bot_user_id
                self._consecutive_failures = 0
                self._set_state("connected")
                logger.info(
                    "telegram: reconnected on attempt %d/%d",
                    attempt, RECONNECT_MAX_ATTEMPTS,
                )
                self._task = asyncio.create_task(self._poll_loop())
                return
            except Exception as exc:
                last_err = str(exc)
                logger.warning(
                    "telegram: reconnect attempt %d/%d failed: %s",
                    attempt, RECONNECT_MAX_ATTEMPTS, exc,
                )
                try:
                    if self._session and not self._session.closed:
                        await self._session.close()
                except Exception:
                    pass
                self._session = None
        self._set_state(
            "error",
            f"Telegram 重连失败: {last_err or '未知错误'}，请检查网络/代理后重新连接",
        )

    async def _handle_update(self, update: Dict[str, Any]) -> None:
        msg = update.get("message") or update.get("edited_message")
        if not msg:
            return
        text = msg.get("text") or msg.get("caption") or ""
        chat = msg.get("chat") or {}
        sender = msg.get("from") or {}
        image_paths: list[str] = []

        # Download the largest available photo if present.
        photos = msg.get("photo") or []
        if photos:
            largest = max(photos, key=lambda p: p.get("file_size", 0) or 0)
            file_id = largest.get("file_id", "")
            if file_id:
                path = await self._download_photo(file_id)
                if path:
                    image_paths.append(path)
                else:
                    logger.warning("telegram: could not download photo %s", file_id)

        # Non-image files (document / video / audio / voice / animation).
        # Telegram attaches a file_id to each; download and surface as files.
        file_paths: list[str] = []
        _kind_ext = {"video": "mp4", "animation": "mp4", "voice": "ogg",
                     "audio": "mp3", "video_note": "mp4"}
        for kind in ("document", "video", "audio", "voice", "animation", "video_note"):
            ent = msg.get(kind)
            if not ent or not isinstance(ent, dict):
                continue
            file_id = ent.get("file_id", "")
            if not file_id:
                continue
            fname = ent.get("file_name", "") or ""
            ext = (Path(fname).suffix.lstrip(".") if fname else "") or _kind_ext.get(kind, "bin")
            path = await self._download_file(file_id, ext, fname)
            if path:
                file_paths.append(path)
            else:
                logger.warning("telegram: could not download %s %s", kind, file_id)

        if not text and not image_paths and not file_paths:
            return  # Nothing useful to process.

        incoming = IncomingMessage(
            channel=self.channel,
            account_id=self.account_id,
            chat_id=str(chat.get("id", "")),
            sender_id=str(sender.get("id", "")),
            sender_name=sender.get("first_name") or sender.get("username") or "tg",
            text=text,
            images=image_paths,
            files=file_paths,
            message_id=str(msg.get("message_id", "")),
        )
        # Fire-and-forget so the poll loop isn't blocked while the model runs.
        asyncio.create_task(self._on_message(incoming))

    async def _download_photo(self, file_id: str) -> str | None:
        """Download a Telegram photo by file_id in the current event loop."""
        if not self._session:
            return None
        assert self._session is not None
        # 1. Resolve file path.
        file_meta = await self._call("getFile", {"file_id": file_id})
        file_path = file_meta.get("file_path", "")
        if not file_path:
            return None
        # 2. Download binary.
        url = f"https://api.telegram.org/file/bot{self._token}/{file_path}"
        async with self._session.get(url, proxy=self._proxy) as r:
            if r.status != 200:
                return None
            data = await r.read()
        if not data:
            return None
        ext = Path(file_path).suffix.lower().lstrip(".") or "jpg"
        if ext not in {"png", "jpg", "jpeg", "gif", "webp"}:
            ext = "jpg"
        tmp_dir = Path(tempfile.gettempdir()) / "ziva-im-bridge"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        save_path = tmp_dir / f"{uuid.uuid4().hex}.{ext}"
        save_path.write_bytes(data)
        return str(save_path)

    async def _download_file(self, file_id: str, ext: str, filename: str = "") -> str | None:
        """Download any Telegram file by file_id (document/video/audio/...).

        Like ``_download_photo`` but keeps the real extension and the original
        filename (sanitized) instead of forcing jpg / a uuid name.
        """
        if not self._session:
            return None
        file_meta = await self._call("getFile", {"file_id": file_id})
        file_path = file_meta.get("file_path", "")
        if not file_path:
            return None
        url = f"https://api.telegram.org/file/bot{self._token}/{file_path}"
        async with self._session.get(url, proxy=self._proxy) as r:
            if r.status != 200:
                return None
            data = await r.read()
        if not data:
            return None
        ext = (ext or Path(file_path).suffix.lower().lstrip(".") or "bin").lower()
        base = _safe_filename(filename) if filename else ""
        save_name = base if base else f"{uuid.uuid4().hex}.{ext}"
        if not Path(save_name).suffix:
            save_name = f"{save_name}.{ext}"
        tmp_dir = Path(tempfile.gettempdir()) / "ziva-im-bridge"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        save_path = tmp_dir / save_name
        save_path.write_bytes(data)
        return str(save_path)

    async def _call(self, method: str, params: Dict[str, Any] | None = None, http_timeout: float = 30) -> Any:
        assert self._session is not None
        url = API_BASE.format(token=self._token, method=method)
        async with self._session.post(
            url,
            json=params or {},
            timeout=aiohttp.ClientTimeout(total=http_timeout),
            proxy=self._proxy,
        ) as r:
            data = await r.json()
            if not data.get("ok"):
                raise RuntimeError(f"telegram {method}: {data.get('description')}")
            return data.get("result")
