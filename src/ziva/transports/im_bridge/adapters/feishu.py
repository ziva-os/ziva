"""飞书 (Lark) adapter — lark-oapi WebSocket long connection.

Uses the official ``lark-oapi`` SDK:
  * ``lark.ws.Client`` holds a WebSocket to Lark's cloud (zero public-network
    exposure — no inbound port needed). Inbound message events fire the
    registered handler, which we normalize and forward to the bridge.
  * Replies are sent via ``client.im.v1.message.create`` (HTTP, per-call).

``lark-oapi`` module-level code captures an asyncio loop at import time. To
prevent it from grabbing the already-running main loop, all lark-oapi imports
and the ``ws.Client`` construction happen inside a dedicated daemon thread.
The event handler then schedules the bridge callback onto the main loop with
``run_coroutine_threadsafe``.

``lark-oapi`` is imported lazily so the bridge loads even when the package
isn't installed (telegram/wechat don't need it). Runtime verification with
real app credentials is required — the SDK API is followed per lark-oapi docs.
"""

from __future__ import annotations

import asyncio
import json
import logging
import tempfile
import threading
import uuid
from pathlib import Path
from typing import Any

from ziva.transports.im_bridge.adapters.base import BaseAdapter
from ziva.transports.im_bridge.models import IncomingMessage, OutgoingMessage

logger = logging.getLogger(__name__)


class FeishuAdapter(BaseAdapter):
    channel = "feishu"

    def __init__(self, config, on_message) -> None:
        super().__init__(config, on_message)
        self._app_id = config.app_id or ""
        self._app_secret = config.app_secret or ""
        self._loop: asyncio.AbstractEventLoop | None = None
        self._ws_thread: threading.Thread | None = None
        self._health_task: asyncio.Task | None = None
        self._stop_flag = False
        self._send_client: Any = None  # built in the lark-oapi thread
        self._connect_error: str | None = None

    @property
    def account_id(self) -> str:
        return self._app_id or self.config.account_id or ""

    async def start(self) -> None:
        if not (self._app_id and self._app_secret):
            self._set_state("error", "missing app_id/app_secret")
            return

        self._loop = asyncio.get_running_loop()
        self._set_state("connecting")
        self._connect_error = None

        # We avoid importing lark_oapi in the main thread because its
        # ws/client.py module captures an asyncio loop at import time. Running
        # the import and Client construction inside a dedicated thread lets it
        # capture the thread's own loop, avoiding "This event loop is already
        # running" errors.
        def _build_and_run_ws() -> None:
            import lark_oapi as lark  # type: ignore
            import lark_oapi.api.im.v1 as im  # type: ignore

            # Outbound client (HTTP per-call, built in the same thread so its
            # module imports happen in this thread's import context).
            self._send_client = (
                lark.Client.builder()
                .app_id(self._app_id)
                .app_secret(self._app_secret)
                .build()
            )
            self._send_module = im

            def _on_receive(event: Any) -> None:
                try:
                    self._dispatch_event(event)
                except Exception:
                    logger.exception("feishu: failed to dispatch inbound event")

            handler = (
                lark.EventDispatcherHandler.builder("", "")
                .register_p2_im_message_receive_v1(_on_receive)
                .build()
            )
            ws_client = lark.ws.Client(
                self._app_id,
                self._app_secret,
                event_handler=handler,
                log_level=lark.LogLevel.INFO,
            )
            try:
                ws_client.start()
            except Exception as exc:  # noqa: BLE001
                self._connect_error = str(exc)
                logger.exception("feishu: websocket runner failed")

        self._ws_thread = threading.Thread(target=_build_and_run_ws, daemon=True)
        self._ws_thread.start()

        # Give the SDK a moment to fail on bad credentials / network.
        await asyncio.sleep(0.5)
        if self._connect_error:
            self._set_state("error", self._connect_error)
            self._ws_thread = None
            return
        if not self._ws_thread.is_alive():
            self._set_state("error", "飞书 WebSocket 已停止")
            self._ws_thread = None
            return
        # No immediate failure — assume the long-running socket is up.
        self._set_state("connected")
        self._health_task = asyncio.create_task(self._health_loop())

    async def _health_loop(self) -> None:
        """Periodically check whether the dedicated ws thread is still alive.

        If lark-oapi exits (network drop, auth expiry, etc.), surface it.
        """
        while True:
            try:
                await asyncio.sleep(5)
            except asyncio.CancelledError:
                return
            if self._stop_flag:
                return
            thread = self._ws_thread
            if thread is None or not thread.is_alive():
                if self._state != "error":
                    self._set_state("error", self._connect_error or "飞书 WebSocket 已断开")
                return

    async def stop(self) -> None:
        self._stop_flag = True
        if self._health_task:
            self._health_task.cancel()
            try:
                await self._health_task
            except (asyncio.CancelledError, Exception):
                pass
            self._health_task = None
        # lark-oapi ws.Client has no public stop(); we can only drop our
        # reference to the thread. The daemon thread will be killed on process
        # exit.
        self._ws_thread = None
        self._send_client = None
        self._send_module = None
        self._set_state("disconnected")

    async def send_message(self, msg: OutgoingMessage) -> None:
        if not self._send_client:
            return
        im = self._send_module
        assert im is not None

        req = (
            im.CreateMessageRequest.builder()
            .receive_id_type("chat_id")
            .request_body(
                im.CreateMessageRequestBody.builder()
                .receive_id(msg.chat_id)
                .msg_type("text")
                .content(json.dumps({"text": msg.text or ""}, ensure_ascii=False))
                .build()
            )
            .build()
        )
        # The SDK call is synchronous HTTP; offload so we don't block the loop.
        await asyncio.to_thread(self._send_client.im.v1.message.create, req)

    # -- internals ----------------------------------------------------------

    def _dispatch_event(self, event: Any) -> None:
        """Normalize a lark P2ImMessageReceiveV1 event → IncomingMessage.

        Called from the ws thread; schedules the bridge callback on the loop.
        """
        event_type = type(event).__name__ if event is not None else "None"
        logger.debug("feishu: received event %s", event_type)
        try:
            evt = event.event
            message = evt.message
            sender = evt.sender.sender_id
        except AttributeError as exc:
            logger.warning("feishu: dropped event %s due to missing field: %s", event_type, exc)
            return

        if not message:
            logger.warning("feishu: event %s has no message", event_type)
            return

        chat_id = getattr(message, "chat_id", "") or ""
        msg_type = getattr(message, "message_type", "") or ""
        message_id = getattr(message, "message_id", "") or ""
        content_raw = getattr(message, "content", "") or "{}"
        try:
            content = json.loads(content_raw)
        except Exception:
            content = {}

        text = ""
        image_paths: list[str] = []

        if msg_type == "text":
            text = content.get("text", "")
            # Strip the @bot mention prefix lark inserts in group chats.
            text = text.replace("@_user_1", "").strip()
        elif msg_type == "image":
            # 飞书 2024-12 之后推送的图片消息,`content.image_key` 实际上是
            # V3 file_key (例如 `img_v3_0213h_xxx`). 老接口
            # `/open-apis/im/v1/images/:image_key` 直接返回 234001
            # Invalid request param —— 必须用新接口
            # `/open-apis/im/v1/messages/:message_id/resources/:file_key?type=image`
            # 才下得到图片. Lark SDK 把新接口暴露在 `im.v1.message_resource.get`.
            image_key = content.get("image_key", "")
            if image_key:
                image_paths = self._download_image(message_id, image_key)
            if not image_paths:
                # Don't drop the message silently — surface a short notice
                # so the user knows their image was rejected. Common cause is
                # message_id missing on the inbound event (some event shapes
                # carry it under `event.message.message_id`; we fall through
                # to the old `image.get` API, which Lark has since retired
                # for V3 file_keys).
                logger.warning(
                    "feishu: could not download image msg=%s key=%s in chat %s",
                    message_id, image_key, chat_id,
                )
                text = "[图片加载失败：无法从飞书下载该图片]"
        else:
            logger.debug("feishu: ignoring non-text/image message type: %s", msg_type)
            return

        if not text and not image_paths:
            logger.debug("feishu: empty message, ignoring")
            return

        sender_id = self._extract_sender_id(sender)
        sender_name = "feishu"
        if sender_id:
            logger.info("feishu: received %s from %s in chat %s", msg_type, sender_id, chat_id)
        else:
            logger.warning("feishu: could not determine sender_id for message in chat %s", chat_id)
        incoming = IncomingMessage(
            channel=self.channel,
            account_id=self.account_id,
            chat_id=chat_id,
            sender_id=sender_id,
            sender_name=sender_name,
            text=text,
            images=image_paths,
        )
        assert self._loop is not None
        asyncio.run_coroutine_threadsafe(self._on_message(incoming), self._loop)
        self._set_state("connected")

    # Magic-byte signatures for the image formats we accept. The Lark API
    # sometimes returns a JSON error payload (e.g. ``{"code":234001,...}``)
    # with HTTP 200 OK on cached error responses, or with a non-image
    # Content-Type — feeding those bytes straight into the LLM triggers
    # ``invalid image content: decode image config: image: unknown format
    # (2013)``. Checking the first few bytes is the cheapest way to refuse
    # the bad payload before it ever reaches the runtime.
    _IMAGE_MAGIC: tuple[tuple[str, bytes], ...] = (
        ("jpg", b"\xff\xd8\xff"),
        ("png", b"\x89PNG\r\n\x1a\n"),
        ("gif", b"GIF87a"),
        ("gif", b"GIF89a"),
        ("webp", b"RIFF"),
    )

    def _looks_like_image(self, data: bytes) -> bool:
        if not data:
            return False
        for _, sig in self._IMAGE_MAGIC:
            if data.startswith(sig):
                # RIFF is the WEBP container header — confirm the WEBP
                # marker at bytes 8..12 before accepting. AVI / WAV also
                # start with RIFF, so the WEBP fourcc is what makes this
                # image-shaped.
                if sig == b"RIFF":
                    return len(data) >= 12 and data[8:12] == b"WEBP"
                return True
        return False

    def _download_image(self, message_id: str, file_key: str) -> list[str]:
        """Download a Feishu image.

        As of 2024-12, image-bearing messages carry a V3 ``image_key``
        (actually a ``file_key``) that the legacy
        ``/open-apis/im/v1/images/{image_key}`` endpoint refuses with
        ``code=234001 Invalid request param``. The supported endpoint is
        ``GET /open-apis/im/v1/messages/{message_id}/resources/{file_key}``
        with a ``type=image`` query string. The SDK exposes this as
        ``client.im.v1.message_resource.get``.

        ``message_id`` is required — without it the new endpoint can't be
        reached and the legacy one will 234001 for V3 keys. We fail loud
        (WARNING) rather than try a known-broken fallback, so a regression
        in the dispatcher that drops the field is immediately visible.
        """
        if not self._send_client or not self._send_module:
            logger.warning("feishu: send client not ready, cannot download image")
            return []
        if not message_id:
            logger.warning(
                "feishu: cannot download image without message_id "
                "(file_key=%s); check _dispatch_event is extracting "
                "event.message.message_id",
                file_key,
            )
            return []
        im = self._send_module

        try:
            req = (
                im.GetMessageResourceRequest.builder()
                .type("image")
                .message_id(message_id)
                .file_key(file_key)
                .build()
            )
            resp = self._send_client.im.v1.message_resource.get(req)
        except Exception:
            logger.exception(
                "feishu: message_resource.get failed for msg=%s key=%s",
                message_id, file_key,
            )
            return []

        if not getattr(resp, "success", lambda: True)():
            code = getattr(resp, "code", "?")
            msg = getattr(resp, "msg", "")
            log_id = getattr(resp, "get_log_id", lambda: None)()
            logger.warning(
                "feishu: message_resource.get failed for msg=%s key=%s "
                "(code=%s msg=%s log_id=%s)",
                message_id, file_key, code, msg, log_id,
            )
            return []

        data = b""
        # The SDK has returned the raw bytes in a few different shapes
        # depending on the version: resp.file, resp.data, resp.body, or raw.content.
        for attr in ("file", "data", "body"):
            try:
                val = getattr(resp, attr, None)
                if val is not None:
                    if hasattr(val, "read"):
                        data = val.read()
                    elif isinstance(val, (bytes, bytearray)):
                        data = bytes(val)
                    elif isinstance(val, str):
                        data = val.encode("utf-8")
                    if data:
                        break
            except Exception:
                pass
        if not data:
            logger.warning("feishu: empty image data for msg=%s key=%s", message_id, file_key)
            return []

        # Last-resort defense: even with `success()` True, the body could
        # still be a non-image payload (e.g. a misconfigured CDN returning
        # an HTML error page with 200 OK). Refuse anything that doesn't
        # look like a known image format so the runtime never sees a
        # broken / corrupt attachment.
        if not self._looks_like_image(data):
            logger.warning(
                "feishu: image payload for msg=%s key=%s failed magic-byte check "
                "(first 8 bytes: %s); dropping",
                message_id, file_key, data[:8].hex(),
            )
            return []

        ext = self._image_ext_from_response(resp) or "jpg"
        # Trust the magic bytes over the filename-derived extension: a
        # cached `file_name: foo.jpg` against a WEBP body would otherwise
        # route the bytes through the jpg MIME in the runtime.
        head = data[:8]
        for known_ext, sig in self._IMAGE_MAGIC:
            if head.startswith(sig) and not (sig == b"RIFF" and data[8:12] != b"WEBP"):
                ext = known_ext
                break
        return [self._save_temp_image(data, ext)]

    def _image_ext_from_response(self, resp: Any) -> str | None:
        """Guess image extension from SDK response file_name or raw headers."""
        try:
            file_name = getattr(resp, "file_name", None) or ""
            if file_name:
                ext = Path(file_name).suffix.lower().lstrip(".")
                if ext in {"png", "jpg", "jpeg", "gif", "webp"}:
                    return ext
        except Exception:
            pass
        try:
            raw = getattr(resp, "raw", None)
            if raw:
                ct = raw.headers.get("Content-Type", "")
                return {
                    "image/png": "png",
                    "image/jpeg": "jpg",
                    "image/webp": "webp",
                    "image/gif": "gif",
                }.get(ct)
        except Exception:
            pass
        return None

    @staticmethod
    def _save_temp_image(data: bytes, ext: str) -> str:
        """Save image bytes to a temp file and return the absolute path."""
        tmp_dir = Path(tempfile.gettempdir()) / "ziva-im-bridge"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        path = tmp_dir / f"{uuid.uuid4().hex}.{ext}"
        path.write_bytes(data)
        return str(path)

    @staticmethod
    def _extract_sender_id(sender_id_obj: Any) -> str:
        """Best-effort sender ID: open_id → union_id → user_id."""
        if sender_id_obj is None:
            return ""
        for attr in ("open_id", "union_id", "user_id"):
            val = getattr(sender_id_obj, attr, None)
            if val:
                return str(val)
        return ""
