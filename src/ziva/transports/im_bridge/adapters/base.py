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

from abc import ABC, abstractmethod
from typing import Any, Dict

from ziva.transports.im_bridge.models import (
    ChannelConfig,
    ConnectionState,
    OnMessage,
    OutgoingMessage,
)


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
