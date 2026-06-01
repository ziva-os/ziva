from __future__ import annotations

from pathlib import Path
from typing import Any, Dict


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

    async def run(self, input_data: Dict[str, Any], ctx: Any) -> Dict[str, Any]:
        path_str = input_data.get("path", ".")
        show_hidden = input_data.get("all", False)

        path = Path(path_str)

        if not path.exists():
            return {"error": "path_not_found", "message": f"Path not found: {path_str}"}

        if not path.is_dir():
            return {"error": "not_a_directory", "message": f"Path is not a directory: {path_str}"}

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

            return {
                "path": str(path.absolute()),
                "entries": entries,
                "total": len(entries),
            }

        except PermissionError as e:
            return {"error": "permission_denied", "message": f"Permission denied: {e}"}
        except Exception as e:
            return {"error": "list_failed", "message": str(e)}
