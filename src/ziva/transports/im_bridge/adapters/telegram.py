"""Telegram adapter — official Bot API via long polling (no SDK, aiohttp only).

Flow:
  * ``start()`` spawns a background task looping ``getUpdates`` (long poll).
  * Each text message becomes an :class:`IncomingMessage` handed to the bridge.
  * ``send_message`` POSTs to ``sendMessage``.

Requires a bot token from @BotFather. Telegram long polling blocks the
request for up to ``timeout`` seconds waiting for updates, so it runs in its
own task; ``stop()`` cancels that task.
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

from ziva.transports.im_bridge.adapters.base import BaseAdapter, decode_image_ref, _bytesio
from ziva.transports.im_bridge.models import IncomingMessage, OutgoingMessage

logger = logging.getLogger(__name__)

API_BASE = "https://api.telegram.org/bot{token}/{method}"
POLL_TIMEOUT = 30  # seconds the long-poll holds open


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

    @property
    def account_id(self) -> str:
        # The bot's own user id — discovered on start via getMe — is the
        # stable identity used in route keys.
        return self._bot_user_id or self.config.account_id or ""

    async def start(self) -> None:
        if not self._token:
            self._set_state("error", "missing bot_token")
            return
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
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.warning("telegram getUpdates failed: %s", exc)
                    await asyncio.sleep(3)
                    continue
                for upd in updates or []:
                    self._offset = max(self._offset, int(upd.get("update_id", 0)) + 1)
                    await self._handle_update(upd)
        except asyncio.CancelledError:
            pass

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

        if not text and not image_paths:
            return  # Nothing useful to process.

        incoming = IncomingMessage(
            channel=self.channel,
            account_id=self.account_id,
            chat_id=str(chat.get("id", "")),
            sender_id=str(sender.get("id", "")),
            sender_name=sender.get("first_name") or sender.get("username") or "tg",
            text=text,
            images=image_paths,
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
