"""Data types shared across the IM bridge (adapters ↔ IMBridge)."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Awaitable, Callable


class ConnectionState(str, Enum):
    """Lifecycle state of a single adapter connection."""

    DISCONNECTED = "disconnected"
    WAITING_SCAN = "waiting_scan"   # wechat iLink QR pending
    CONNECTING = "connecting"
    CONNECTED = "connected"
    ERROR = "error"


@dataclass
class IncomingMessage:
    """A message received from an IM channel, normalized for the bridge."""

    channel: str          # feishu / wechat / telegram
    account_id: str       # the bot's own id on that channel (app_id / wxid / tg bot id)
    chat_id: str          # conversation id (feishu chat_id / wechat peer wxid / tg chat id)
    sender_id: str        # who sent the message (used for the sender whitelist)
    sender_name: str      # display name, for the session title + sidebar
    text: str
    images: list[str] = None  # local file paths of downloaded images

    def __post_init__(self):
        if self.images is None:
            self.images = []

    @property
    def route_key(self) -> str:
        """Stable key mapping one IM conversation to one ziva session."""
        return f"{self.channel}:{self.account_id}:{self.chat_id}"


@dataclass
class OutgoingMessage:
    """A reply to push back to an IM chat."""

    chat_id: str
    text: str
    images: list[str] = None  # local image paths to send back (future)

    def __post_init__(self):
        if self.images is None:
            self.images = []


@dataclass
class ChannelConfig:
    """Persisted configuration for one channel.

    Only the fields relevant to a channel are filled; the rest stay ``None``.
    """

    enabled: bool = False
    # feishu (lark-oapi)
    app_id: str | None = None
    app_secret: str | None = None
    # wechat (iLink / QClaw / WorkBuddy gateway)
    account_id: str | None = None
    gateway_url: str | None = None
    # telegram (BotFather)
    bot_token: str | None = None
    # optional proxy for channels behind the GFW (e.g. Telegram)
    proxy_url: str | None = None

    def configured(self) -> bool:
        """Whether the channel has the minimum credentials to attempt a start."""
        if self.app_id and self.app_secret:
            return True
        if self.bot_token:
            return True
        if self.account_id:
            return True
        if self.gateway_url:
            return True
        return False

    def secret_fields(self) -> set[str]:
        """Field names that must be redacted before returning to the frontend."""
        return {"app_secret", "bot_token"}


# Adapters call this to hand a normalized inbound message to the bridge.
OnMessage = Callable[[IncomingMessage], Awaitable[None]]
