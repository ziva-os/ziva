import os
from pathlib import Path
from plugins.tools._shared.diff_utils import create_diff
from ziva_runtime.shared_types import ToolResult


class WriteFileTool:
    """Enhanced write file tool with diff output."""

    def spec(self):
        return {
            "name": "write_file",
            "description": "Write content to a file. Returns diff of changes for existing files.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "file_path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["file_path", "content"],
            },
        }

    async def run(self, input_data, ctx):
        file_path = input_data.get("file_path")
        content = input_data.get("content", "")

        if not file_path:
            return ToolResult(text="Error: invalid_input\nfile_path is required", error=True)

        try:
            path = Path(file_path)

            # Create parent directories if they don't exist
            parent_dir = path.parent
            if parent_dir and not parent_dir.exists():
                parent_dir.mkdir(parents=True, exist_ok=True)

            # Read old content for diff
            old_content = ""
            file_existed = path.exists()
            if file_existed:
                with open(path, "r", encoding="utf-8") as f:
                    old_content = f.read()

            # Generate diff
            diff = create_diff(old_content, content, str(path))

            # Write content to file
            with open(path, "w", encoding="utf-8") as f:
                bytes_written = f.write(content)

            if not file_existed:
                line_count = content.count("\n") + 1
                size_kb = bytes_written / 1024
                return ToolResult(
                    text=f"Wrote {line_count} lines ({size_kb:.1f}KB) to {path}",
                    metadata={"file_path": str(path), "bytes_written": bytes_written, "file_existed": False}
                )
            else:
                return ToolResult(
                    text=f"Updated {path}",
                    metadata={"file_path": str(path), "bytes_written": bytes_written, "file_existed": True, "diff": diff}
                )

        except PermissionError:
            return ToolResult(text=f"Error: permission_denied\nPermission denied writing to {file_path}", error=True)
        except OSError as e:
            return ToolResult(text=f"Error: write_failed\nFailed to write file: {e}", error=True)
