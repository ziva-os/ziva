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

from ziva.transports.im_bridge.adapters.base import BaseAdapter
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
        # Telegram caps messages at 4096 chars; chunk long replies.
        text = msg.text or ""
        for i in range(0, max(len(text), 1), 4000):
            await self._call("sendMessage", {
                "chat_id": msg.chat_id,
                "text": text[i:i + 4000],
            })

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
