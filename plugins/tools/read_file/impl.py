import asyncio
import base64
from pathlib import Path

from ziva.shared_types import ToolResult, resolve_workspace_cwd

IMAGE_EXTENSIONS = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    # 注意：故意不包含 .svg。SVG 作为 base64 data URL 投递给模型 API 会被拒
    # （"image/svg+xml" not supported, err 2013）。SVG 本质是 XML 文本，按文本
    # 读出来模型反而能直接读懂结构/路径。
}


class ReadFileTool:
    """Enhanced read file tool with line numbers, offset, limit, and image support."""

    DEFAULT_LIMIT = 2000

    def spec(self):
        return {
            "name": "read_file",
            "description": "Read the contents of a file or directory. Returns content with line numbers prefixed.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "Path to the file to read"
                    },
                    "offset": {
                        "type": "integer",
                        "description": "The line number to start reading from (1-indexed, default 1)",
                        "default": 1,
                        "minimum": 1
                    },
                    "limit": {
                        "type": "integer",
                        "description": "The maximum number of lines to read (defaults to 2000)",
                        "default": 2000
                    }
                },
                "required": ["file_path"],
            },
        }

    def _is_image_file(self, path: Path) -> str | None:
        """Return MIME type if the file is an image, else None."""
        return IMAGE_EXTENSIONS.get(path.suffix.lower())

    @staticmethod
    def _read_image_sync(path: Path, mime: str) -> ToolResult:
        """Synchronous image read + base64 encode — run via to_thread()."""
        data = path.read_bytes()
        b64 = base64.b64encode(data).decode("ascii")
        data_url = f"data:{mime};base64,{b64}"
        return ToolResult(
            text=f"Image: {path.name} ({len(data)} bytes, {mime})",
            images=[data_url],
            metadata={"path": str(path), "size": len(data), "mime": mime},
        )

    @staticmethod
    def _read_text_sync(path: Path, offset: int, limit: int) -> dict:
        """Synchronous text file read — run via to_thread()."""
        lines = []
        has_more_lines = False
        line_count = 0
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for text in f:
                line_count += 1
                if line_count < offset:
                    continue
                if len(lines) >= limit:
                    has_more_lines = True
                    continue
                processed_line = text.rstrip("\r\n")
                lines.append(f"{line_count}: {processed_line}")
        return {
            "lines": lines,
            "last_read_line": offset + len(lines) - 1 if lines else offset - 1,
            "total_lines": line_count,
            "truncated": has_more_lines,
        }

    def _is_binary_file(self, path: Path) -> bool:
        """Check if a file is binary using null-byte detection and extension check."""
        binary_extensions = {
            '.zip', '.tar', '.gz', '.7z', '.exe', '.dll', '.so', '.dylib', '.bin', '.dat',
            '.pyc', '.pyo', '.class', '.jar', '.war', '.obj', '.o', '.a', '.lib',
            '.ico', '.wasm'
        }
        if path.suffix.lower() in binary_extensions or path.suffix.lower() in IMAGE_EXTENSIONS:
            return True

        # Sample check for null bytes
        try:
            if not path.exists():
                return False
            with open(path, 'rb') as f:
                chunk = f.read(4096)
                if not chunk:
                    return False
                if b'\x00' in chunk:
                    return True
                non_printable = sum(1 for b in chunk if b < 9 or (13 < b < 32))
                if non_printable / len(chunk) > 0.3:
                    return True
        except Exception:
            pass
        return False


    async def run(self, input_data, ctx):
        file_path = input_data.get("file_path")
        offset = input_data.get("offset", 1)
        limit = input_data.get("limit", self.DEFAULT_LIMIT)

        if not file_path:
            return ToolResult(text="Error: invalid_input\nfile_path is required", error=True)

        if offset < 1:
            return ToolResult(text="Error: invalid_input\noffset must be >= 1", error=True)

        path = Path(file_path)
        # Resolve relative paths against the session's workspace, not the
        # backend process's os.getcwd(). Absolute paths are untouched.
        if not path.is_absolute():
            path = Path(resolve_workspace_cwd(ctx)) / path

        try:
            # Check if path exists
            if not path.exists():
                return ToolResult(text=f"Error: file_not_found\nFile not found: {file_path}", error=True)

            # Directory Handling - list entries sorted
            if path.is_dir():
                entries = sorted([
                    f"{entry.name}{'/' if entry.is_dir() else ''}"
                    for entry in path.iterdir()
                ])

                start = offset - 1
                end = start + limit
                sliced = entries[start:end]
                truncated = end < len(entries)

                lines = list(sliced)
                if truncated:
                    lines.append(f"\n(Showing {len(sliced)} of {len(entries)} entries)")
                else:
                    lines.append(f"\n({len(entries)} entries)")
                return ToolResult(text="\n".join(lines), metadata={"type": "directory", "truncated": truncated})

            # Check for image file — read as base64 data URL (offloaded to thread)
            mime = self._is_image_file(path)
            if mime:
                return await asyncio.to_thread(self._read_image_sync, path, mime)

            # Check for binary file
            if self._is_binary_file(path):
                return ToolResult(text=f"Error: binary_file\nCannot read binary file: {file_path}", error=True)

            # Text File Reading with line numbers (offloaded to thread for large files)
            result = await asyncio.to_thread(self._read_text_sync, path, offset, limit)

            lines = result["lines"]
            if result["truncated"]:
                lines.append(f"\n(Showing lines {offset}-{result['last_read_line']}. Use offset={result['last_read_line']+1} to continue.)")
            else:
                lines.append(f"\n(End of file - {result['total_lines']} lines)")
            return ToolResult(text="\n".join(lines), metadata={"type": "file", "total_lines": result["total_lines"], "truncated": result["truncated"]})

        except PermissionError:
            return ToolResult(text=f"Error: permission_denied\nPermission denied: {file_path}", error=True)
        except IsADirectoryError:
            return ToolResult(text=f"Error: is_directory\nPath is a directory: {file_path}", error=True)
        except Exception as e:
            return ToolResult(text=f"Error: read_failed\nFailed to read file: {e}", error=True)
