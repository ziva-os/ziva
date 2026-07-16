"""``BaseAdapter`` — the uniform interface every IM channel implements.

An adapter:
  * connects to its platform on ``start()`` (WebSocket / long poll / scan),
  * normalizes inbound messages into :class:`IncomingMessage` and hands them
    to ``self._on_message`` (the IMBridge callback),
  * pushes replies back via ``send_message``.

``status()`` returns a JSON-serializable dict the HTTP API surfaces to the
frontend "连接手机" modal.
"""

from __future__ import annotations

import base64
import io
import os
from abc import ABC, abstractmethod
from typing import Any, Dict

from ziva.transports.im_bridge.models import (
    ChannelConfig,
    ConnectionState,
    OnMessage,
    OutgoingMessage,
)


def decode_image_ref(ref: str) -> tuple[bytes, str] | None:
    """Resolve an outbound image reference into ``(raw_bytes, filename)``.

    Tool outputs (``read_file`` on an image, MCP image content) arrive as
    ``data:<mime>;base64,...`` URLs — *not* local file paths. Both IM
    adapters need real bytes to upload (Feishu ``image.create`` multipart,
    Telegram ``sendPhoto`` multipart), so this decodes a data URL into bytes
    and also accepts an existing local file path. Returns ``None`` when the
    reference can't be decoded/read so the caller can skip it without taking
    down the whole reply.
    """
    if not ref or not isinstance(ref, str):
        return None
    if ref.startswith("data:"):
        try:
            header, payload = ref.split(",", 1)
            mime = header[len("data:"):].split(";", 1)[0] or "image/png"
            ext = mime.rsplit("/", 1)[-1].split("+", 1)[0].lower() or "png"
            if ext == "jpeg":
                ext = "jpg"
            data = base64.b64decode(payload)
        except Exception:
            return None
        return data, f"image.{ext}"
    # Already a local file path.
    try:
        with open(ref, "rb") as f:
            data = f.read()
    except Exception:
        return None
    return data, os.path.basename(ref) or "image"


def _bytesio(data: bytes, filename: str) -> io.BytesIO:
    """Wrap bytes in a BytesIO with a ``.name`` so multipart encoders
    (httpx for Feishu, aiohttp for Telegram) upload it with a real filename
    and the platform can infer the image type from the extension."""
    buf = io.BytesIO(data)
    buf.name = filename  # type: ignore[attr-defined]
    return buf


class BaseAdapter(ABC):
    #: ``feishu`` / ``wechat`` / ``telegram`` — matches IncomingMessage.channel
    channel: str = ""

    def __init__(self, config: ChannelConfig, on_message: OnMessage) -> None:
        self.config = config
        self._on_message = on_message
        self._state: ConnectionState = ConnectionState.DISCONNECTED
        self._error: str | None = None
        self._qr_data: str | None = None
        self._scan_status: str | None = None

    @abstractmethod
    async def start(self) -> None:
        """Connect to the platform and begin receiving messages."""

    @abstractmethod
    async def stop(self) -> None:
        """Disconnect and release resources."""

    @abstractmethod
    async def send_message(self, msg: OutgoingMessage) -> None:
        """Push a reply text back to the IM chat identified by ``msg.chat_id``."""

    async def send_typing(self, chat_id: str, message_id: str = "") -> None:
        """Best-effort "agent is processing" indicator.

        Default: no-op. Channels with a typing primitive (Telegram
        ``sendChatAction``, Feishu "OnIt" reaction, etc.) override this.
        ``message_id`` is the inbound message's native id, used by channels
        that anchor the indicator to the user's message (e.g. a Feishu
        reaction). The bridge never blocks on a typing indicator.
        """

    async def stop_typing(self, chat_id: str) -> None:
        """Tear down whatever ``send_typing`` set up. Default: no-op."""

    @property
    def account_id(self) -> str:
        """This bot's own id on the channel (used in the route key)."""
        return self.config.account_id or ""

    @property
    def display_name(self) -> str:
        return self.channel

    def status(self) -> Dict[str, Any]:
        return {
            "channel": self.channel,
            "state": self._state.value,
            "display_name": self.display_name,
            "account_id": self.account_id,
            "error": self._error,
            "qr": self._qr_data,
            "scan_status": self._scan_status,
        }

    # -- helpers for subclasses ---------------------------------------------

    def _set_state(self, state: ConnectionState | str, error: str | None = None) -> None:
        self._state = ConnectionState(state) if isinstance(state, str) else state
        self._error = error
