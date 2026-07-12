"""个人微信 (WeChat) adapter — QR scan via a gateway WebSocket.

This adapter talks to a pluggable WeChat gateway (QClaw / JPRX / WorkBuddy
style) over WebSocket. The gateway is responsible for the actual WeChat
protocol; Ziva only needs a gateway URL.

Gateway protocol (JSON over WebSocket):
  Client → Server:
    {"action": "login", "type": "wechat"}
    {"action": "send", "to_wxid": "...", "content": "..."}

  Server → Client:
    {"event": "qr", "data": "data:image/png;base64,..."}
    {"event": "scan", "status": "waiting|confirmed|timeout"}
    {"event": "login", "account_id": "wxid_xxx", "account_name": "..."}
    {"event": "message", "data": {"from_wxid": "...", "from_name": "...", "content": "..."}}
    {"event": "error", "message": "..."}

Status lifecycle:
  waiting_scan → (qr shown, user scans) → connecting → connected.

> 注意：个人微信没有官方 Bot API。此实现依赖外部网关，存在账号风险，请仅
> 用于测试或已获授权的账号。
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import aiohttp
from aiohttp import WSServerHandshakeError

from ziva.transports.im_bridge.adapters.base import BaseAdapter
from ziva.transports.im_bridge.models import IncomingMessage, OutgoingMessage

logger = logging.getLogger(__name__)

DEFAULT_GATEWAY = ""


class WechatAdapter(BaseAdapter):
    channel = "wechat"

    def __init__(self, config, on_message) -> None:
        super().__init__(config, on_message)
        self._gateway_url = (config.gateway_url or DEFAULT_GATEWAY).strip()
        self._session: aiohttp.ClientSession | None = None
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._reader_task: asyncio.Task | None = None
        self._account_wxid = config.account_id or ""
        self._account_name = ""

    @property
    def account_id(self) -> str:
        return self._account_wxid or self.config.account_id or ""

    async def start(self) -> None:
        if not self._gateway_url:
            self._set_state("disconnected", "未配置微信网关，请在连接时填写网关地址")
            return

        self._session = aiohttp.ClientSession()
        self._set_state("waiting_scan")
        self._qr_data = None
        self._scan_status = "waiting"

        try:
            self._ws = await self._session.ws_connect(self._gateway_url, heartbeat=30.0)
        except WSServerHandshakeError as exc:
            logger.exception("wechat: failed to connect gateway %s", self._gateway_url)
            hint = ""
            if exc.status == 404:
                hint = "网关未找到，请确认网关地址路径正确且网关服务已启动"
            elif exc.status in (401, 403):
                hint = "网关拒绝连接，请检查认证信息"
            self._set_state("error", f"无法连接微信网关 ({exc.status}): {hint or str(exc)}")
            await self._close_session()
            return
        except Exception as exc:
            logger.exception("wechat: failed to connect gateway %s", self._gateway_url)
            self._set_state("error", f"无法连接微信网关: {exc}")
            await self._close_session()
            return

        # Ask the gateway to start a WeChat login session.
        await self._send_json({"action": "login", "type": "wechat"})
        self._reader_task = asyncio.create_task(self._reader_loop())

    async def stop(self) -> None:
        if self._reader_task:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except (asyncio.CancelledError, Exception):
                pass
            self._reader_task = None

        if self._ws:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None

        await self._close_session()
        self._qr_data = None
        self._scan_status = None
        self._set_state("disconnected")

    async def send_message(self, msg: OutgoingMessage) -> None:
        if not self._ws or self._ws.closed:
            logger.warning("wechat: send_message called but websocket is closed")
            return
        await self._send_json({
            "action": "send",
            "to_wxid": msg.chat_id,
            "content": msg.text or "",
        })

    # -- internals ----------------------------------------------------------

    async def _close_session(self) -> None:
        if self._session:
            await self._session.close()
            self._session = None

    async def _send_json(self, data: dict[str, Any]) -> None:
        if self._ws and not self._ws.closed:
            await self._ws.send_str(json.dumps(data, ensure_ascii=False))

    async def _reader_loop(self) -> None:
        assert self._ws is not None
        try:
            async for msg in self._ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    try:
                        payload = json.loads(msg.data)
                    except json.JSONDecodeError:
                        logger.warning("wechat: non-JSON gateway message: %s", msg.data[:200])
                        continue
                    try:
                        await self._handle_event(payload)
                    except Exception:
                        logger.exception("wechat: failed to handle gateway event: %s", payload)
                elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                    break
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("wechat: reader loop error")
            self._set_state("error", f"网关连接异常: {exc}")
        finally:
            # Connection lost. If we were still waiting for a scan, treat it as
            # an error so the user can reconnect.
            if self._state in ("waiting_scan", "connecting"):
                self._set_state("error", "网关连接已断开")

    async def _handle_event(self, payload: dict[str, Any]) -> None:
        event = payload.get("event")
        if event == "qr":
            self._qr_data = payload.get("data")
            self._scan_status = "waiting"
            self._set_state("waiting_scan")
        elif event == "scan":
            self._scan_status = payload.get("status", "waiting")
            if self._scan_status == "confirmed":
                self._set_state("connecting")
            elif self._scan_status == "timeout":
                self._set_state("error", "二维码已过期，请重新连接")
        elif event == "login":
            self._account_wxid = payload.get("account_id") or self._account_wxid
            self._account_name = payload.get("account_name") or self._account_name
            self.config.account_id = self._account_wxid
            self._qr_data = None
            self._scan_status = None
            self._set_state("connected")
        elif event == "message":
            data = payload.get("data") or {}
            await self._on_message(self._normalize(data))
        elif event == "error":
            err = payload.get("message", "unknown gateway error")
            self._set_state("error", err)
        else:
            logger.debug("wechat: unhandled gateway event: %s", event)

    def _normalize(self, data: dict[str, Any]) -> IncomingMessage:
        """Normalize a gateway message event → IncomingMessage."""
        return IncomingMessage(
            channel=self.channel,
            account_id=self.account_id,
            chat_id=str(data.get("from_wxid", "")),
            sender_id=str(data.get("from_wxid", "")),
            sender_name=data.get("from_name") or "wechat",
            text=str(data.get("content", "")),
        )
