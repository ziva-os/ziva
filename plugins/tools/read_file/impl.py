import asyncio
import base64
from pathlib import Path

IMAGE_EXTENSIONS = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".svg": "image/svg+xml",
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
    def _read_image_sync(path: Path, mime: str) -> dict:
        """Synchronous image read + base64 encode — run via to_thread()."""
        data = path.read_bytes()
        b64 = base64.b64encode(data).decode("ascii")
        data_url = f"data:{mime};base64,{b64}"
        return {
            "type": "image",
            "image_url": data_url,
            "metadata": {"path": str(path), "size": len(data), "mime": mime},
        }

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
            return {"error": "invalid_input", "message": "file_path is required"}

        if offset < 1:
            return {"error": "invalid_input", "message": "offset must be >= 1"}

        path = Path(file_path)

        try:
            # Check if path exists
            if not path.exists():
                return {"error": "file_not_found", "message": f"File not found: {file_path}"}

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

                output = [
                    f"<path>{path}</path>",
                    "<type>directory</type>",
                    "<entries>",
                    "\n".join(sliced),
                    f"\n(Showing {len(sliced)} of {len(entries)} entries. Use 'offset' parameter to read beyond entry {offset + len(sliced)})" if truncated else f"\n({len(entries)} entries)",
                    "</entries>"
                ]
                return {
                    "content": "\n".join(output),
                    "metadata": {"type": "directory", "truncated": truncated}
                }

            # Check for image file — read as base64 data URL (offloaded to thread)
            mime = self._is_image_file(path)
            if mime:
                return await asyncio.to_thread(self._read_image_sync, path, mime)

            # Check for binary file
            if self._is_binary_file(path):
                return {"error": "binary_file", "message": f"Cannot read binary file: {file_path}"}

            # Text File Reading with line numbers (offloaded to thread for large files)
            result = await asyncio.to_thread(self._read_text_sync, path, offset, limit)

            output = [f"<path>{path}</path>", "<type>file</type>", "<content>"]
            output.append("\n".join(result["lines"]))

            last_read_line = result["last_read_line"]
            next_offset = last_read_line + 1

            if result["truncated"]:
                output.append(f"\n(Showing lines {offset}-{last_read_line}. Use offset={next_offset} to continue.)")
            else:
                output.append(f"\n(End of file - total {result['total_lines']} lines)")

            output.append("</content>")

            return {
                "content": "\n".join(output),
                "metadata": {"type": "file", "total_lines": result["total_lines"], "truncated": result["truncated"]}
            }

        except PermissionError:
            return {"error": "permission_denied", "message": f"Permission denied: {file_path}"}
        except IsADirectoryError:
            return {"error": "is_directory", "message": f"Path is a directory: {file_path}"}
        except Exception as e:
            return {"error": "read_failed", "message": f"Failed to read file: {e}"}
