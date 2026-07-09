from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

from ziva.shared_types import ToolResult, resolve_workspace_cwd


class ListTool:
    """List directory contents with metadata."""

    def spec(self):
        return {
            "name": "list",
            "description": "List directory contents with file metadata (type, size, modified time).",
            "input_schema": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Directory path to list (default '.')"},
                    "all": {"type": "boolean", "description": "Show hidden files (default false)"},
                },
                "required": [],
            },
        }

    async def run(self, input_data: Dict[str, Any], ctx: Any) -> ToolResult:
        path_str = input_data.get("path") or resolve_workspace_cwd(ctx)
        show_hidden = input_data.get("all", False)

        path = Path(path_str)

        if not path.exists():
            return ToolResult(text=f"Error: path_not_found\nPath not found: {path_str}", error=True)

        if not path.is_dir():
            return ToolResult(text=f"Error: not_a_directory\nPath is not a directory: {path_str}", error=True)

        try:
            entries = []

            for item in sorted(path.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
                name = item.name

                # Skip hidden files unless all=True
                if not show_hidden and name.startswith("."):
                    continue

                try:
                    stat = item.stat()
                    entries.append(
                        {
                            "name": name,
                            "type": "dir" if item.is_dir() else "file",
                            "size": stat.st_size if item.is_file() else None,
                            "modified": stat.st_mtime,
                        }
                    )
                except (PermissionError, OSError):
                    # Skip entries we can't stat
                    entries.append(
                        {
                            "name": name,
                            "type": "dir" if item.is_dir() else "file",
                            "size": None,
                            "modified": None,
                            "error": "access_denied",
                        }
                    )

            lines = []
            for e in entries:
                if e.get("type") == "dir":
                    lines.append(f"{e['name']}/ (dir)")
                else:
                    size = e.get("size")
                    size_str = f", {size/1024:.1f}KB" if size else ""
                    lines.append(f"{e['name']} (file{size_str})")
            return ToolResult(text="\n".join(lines), metadata={"path": str(path.absolute()), "entries": entries, "total": len(entries)})

        except PermissionError as e:
            return ToolResult(text=f"Error: permission_denied\nPermission denied: {e}", error=True)
        except Exception as e:
            return ToolResult(text=f"Error: list_failed\n{e}", error=True)
