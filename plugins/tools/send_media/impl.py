from __future__ import annotations

import asyncio
import base64
from pathlib import Path
from typing import Any, Dict

from ziva.shared_types import ToolResult, resolve_workspace_cwd

# Raster image extensions we can inline-preview on desktop and send as a
# photo on IM. Mirrors read_file's set (SVG deliberately excluded: it is
# XML text, and as a base64 data URL most providers/adapters reject it).
IMAGE_MIME = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
}


class SendMediaTool:
    """Deliver an image/file to the user's chat.

    This is the explicit, structured replacement for the unstable practice
    of the model embedding image references (e.g. ``attachment://filename``)
    in its reply markdown and hoping each surface resolves them. The model
    calls this tool with the absolute path; the tool then:

    * fires the runtime's ``on_send_media`` callbacks so an IM bridge can
      push the media to the user's IM chat (with an optional caption), and
    * returns a result the desktop renders inline (the frontend renders
      ``args.path`` as an image preview, so it survives both streaming and
      history reload).

    Reading the file and base64-encoding images here means the IM adapter
    receives a ``data:`` URL — the same shape it already handles for other
    tool-produced images (see ``_collect_output_images`` in the bridge).
    """

    def spec(self) -> Dict[str, Any]:
        return {
            "name": "send_media",
            "description": (
                "Send or deliver an image/file to the user. Use this whenever the user "
                "asks you to send, deliver, or show them a generated image or file "
                "(e.g. \"生成一张梵高自画像并发送给我\", \"send it to me\", \"发给我\"). "
                "Pass the absolute path of the file. On IM-connected sessions the media "
                "is pushed to the user's IM chat; on desktop it is shown inline. "
                "Do NOT embed images as attachment:// or any other custom inline "
                "markdown — call this tool instead."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Absolute path of the image/file to send.",
                    },
                    "caption": {
                        "type": "string",
                        "description": "Optional short caption/text accompanying the media.",
                    },
                },
                "required": ["path"],
            },
        }

    async def run(self, input_data: Dict[str, Any], ctx: Any) -> ToolResult:
        raw_path = (input_data.get("path") or "").strip()
        caption = (input_data.get("caption") or "").strip()
        if not raw_path:
            return ToolResult(text="Error: missing_path\n`path` is required.", error=True)

        # Resolve relative to the session workspace (same rule as read_file/shell).
        ws = resolve_workspace_cwd(ctx)
        p = Path(raw_path).expanduser()
        if not p.is_absolute():
            p = Path(ws) / p
        try:
            p = p.resolve()
        except Exception:
            # resolve() can raise on a broken symlink; keep the expanded path.
            pass
        if not p.exists() or not p.is_file():
            return ToolResult(text=f"Error: file_not_found\n{raw_path} does not exist.", error=True)

        ext = p.suffix.lower()
        mime = IMAGE_MIME.get(ext)
        data_url: str | None = None
        if mime:
            try:
                data = await asyncio.to_thread(p.read_bytes)
            except OSError as exc:
                return ToolResult(
                    text=f"Error: read_failed\n{exc.__class__.__name__}: {exc}",
                    error=True,
                )
            b64 = base64.b64encode(data).decode("ascii")
            data_url = f"data:{mime};base64,{b64}"

        # Push to IM if the session is routed through an IM channel. The
        # bridge registers these callbacks via runtime.on_send_media; in a
        # desktop-only session there are none and the media is simply shown
        # inline (the desktop IS the surface).
        runtime = ctx.metadata.get("_runtime") if ctx else None
        delivered = False
        if runtime is not None:
            for cb in getattr(runtime, "_send_media_callbacks", []) or []:
                try:
                    res = cb(ctx.session_id, str(p), data_url, caption)
                    if asyncio.iscoroutine(res):
                        res = await res
                    if res:
                        delivered = True
                except Exception:
                    # One failing callback must not break the tool — the
                    # desktop still shows the media inline.
                    pass

        if delivered:
            text = f"Sent {p.name} to the user's chat."
        else:
            text = f"{p.name} is shown to the user (no IM channel connected)."
        if caption:
            text = f"{text}\nCaption: {caption}"
        return ToolResult(
            text=text,
            metadata={"path": str(p), "delivered": delivered, "mime": mime},
        )
