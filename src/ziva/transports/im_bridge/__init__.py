"""Ziva IM Bridge — in-process component of ``desktop_api``.

Lets external IM channels (飞书 / 个人微信 / Telegram) trigger ordinary ziva
sessions. An inbound IM message becomes a normal user turn on a normal
session (same ``runtime.chat_with_events`` path the desktop composer uses);
the model's reply is forwarded back to the IM chat. See
``docs/im-bridge.md`` for the full design.
"""

from ziva.transports.im_bridge.bridge import IMBridge
from ziva.transports.im_bridge.models import (
    ChannelConfig,
    ConnectionState,
    IncomingMessage,
    OutgoingMessage,
)

__all__ = [
    "IMBridge",
    "ChannelConfig",
    "ConnectionState",
    "IncomingMessage",
    "OutgoingMessage",
]
