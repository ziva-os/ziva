from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Dict

from ziva.shared_types import ToolResult, resolve_workspace_cwd


class SendFileTool:
    """Deliver a file of any kind to the user's chat.

    Explicit, structured replacement for the model embedding image references
    (e.g. ``attachment://filename``) in its reply markdown. The model calls
    this tool with an absolute path; the tool validates the file exists and
    fires the runtime's ``on_send_file`` callbacks. The bridge + adapters
    classify by extension and deliver appropriately (image → photo, video →
    video, archive/pdf/doc → file/document). The desktop renders the path
    inline in the tool card.

    The tool itself stays format-agnostic: it does not base64-encode (a video
    or archive would blow up the payload) — adapters read the file from the
    path via ``decode_image_ref``.
    """

    def spec(self) -> Dict[str, Any]:
        return {
            "name": "send_file",
            "description": (
                "Send a generated file to the user as a downloadable attachment — "
                "any file type works (image, video, archive, PDF, document, "
                "spreadsheet, etc.). Only use this tool when (a) you are connected "
                "to the user via an IM channel AND (b) you have an actual file on "
                "disk to deliver. Do NOT use it for ordinary text replies — write "
                "those directly in your message. Pass the absolute path of the file "
                "to send."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Absolute path of the file to send.",
                    },
                    "media_type": {
                        "type": "string",
                        "enum": ["image", "video", "file"],
                        "description": (
                            "Optional hint for how to deliver the file: "
                            "`image` (send as a photo), `video` (send as a video), "
                            "or `file` (send as a document — archives, PDFs, docs, "
                            "spreadsheets, text, etc.). If omitted, the type is "
                            "inferred from the file extension. Pass it when you "
                            "know the kind and the extension is missing or ambiguous."
                        ),
                    },
                    "caption": {
                        "type": "string",
                        "description": "Optional short caption/text accompanying the file.",
                    },
                },
                "required": ["path"],
            },
        }

    async def run(self, input_data: Dict[str, Any], ctx: Any) -> ToolResult:
        raw_path = (input_data.get("path") or "").strip()
        media_type = (input_data.get("media_type") or "").strip().lower() or None
        caption = (input_data.get("caption") or "").strip()
        if not raw_path:
            return ToolResult(text="Error: missing_path\n`path` is required.", error=True)
        if media_type and media_type not in ("image", "video", "file"):
            return ToolResult(
                text=f"Error: bad_media_type\nmedia_type must be image/video/file, got {media_type!r}.",
                error=True,
            )

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

        # Push to IM if the session is routed through an IM channel. The bridge
        # registers these callbacks via runtime.on_send_file; in a desktop-only
        # session there are none and the file is simply shown inline.
        runtime = ctx.metadata.get("_runtime") if ctx else None
        delivered = False
        if runtime is not None:
            for cb in getattr(runtime, "_send_file_callbacks", []) or []:
                try:
                    res = cb(ctx.session_id, str(p), media_type, caption)
                    if asyncio.iscoroutine(res):
                        res = await res
                    if res:
                        delivered = True
                except Exception:
                    # One failing callback must not break the tool — the
                    # desktop still shows the file inline.
                    pass

        if delivered:
            text = f"Sent {p.name} to the user's chat."
        else:
            text = f"{p.name} is shown to the user (no IM channel connected)."
        if caption:
            text = f"{text}\nCaption: {caption}"
        return ToolResult(text=text, metadata={"path": str(p), "delivered": delivered, "media_type": media_type})
