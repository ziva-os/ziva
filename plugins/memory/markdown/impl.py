from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, List

from ziva_runtime.shared_types import RuntimeContext


class MarkdownMemoryStore:
    def __init__(self, storage_dir: str | None = None):
        if storage_dir is None:
            storage_dir = os.path.expanduser("~/.ziva/memories")
        self._dir = Path(storage_dir)

    def _ensure_dir(self) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)

    def _path_for(self, key: str) -> Path:
        safe_key = key.replace("/", "_").replace("\\", "_")
        return self._dir / f"{safe_key}.md"

    async def put(self, key: str, value: Dict[str, Any], ctx: RuntimeContext) -> None:
        self._ensure_dir()
        path = self._path_for(key)
        # Write as YAML frontmatter + markdown body
        lines = ["---"]
        lines.append(f"key: {json.dumps(key)}")
        lines.append(f"updated_at: {json.dumps(ctx.metadata.get('timestamp', ''))}")
        lines.append("---")
        lines.append("")
        if isinstance(value, dict):
            for k, v in value.items():
                lines.append(f"## {k}")
                lines.append(str(v))
                lines.append("")
        else:
            lines.append(str(value))
        path.write_text("\n".join(lines), encoding="utf-8")

    async def search(self, query: str, limit: int, ctx: RuntimeContext) -> List[Dict[str, Any]]:
        self._ensure_dir()
        if not self._dir.exists():
            return []
        results = []
        query_lower = query.lower()
        for md_file in sorted(self._dir.glob("*.md")):
            if len(results) >= limit:
                break
            content = md_file.read_text(encoding="utf-8")
            key = md_file.stem
            if query_lower in key.lower() or query_lower in content.lower():
                results.append({"key": key, "content": content})
        return results

    async def summarize(self, ctx: RuntimeContext) -> Dict[str, Any]:
        self._ensure_dir()
        if not self._dir.exists():
            return {"total_keys": 0, "keys": []}
        keys = sorted(p.stem for p in self._dir.glob("*.md"))
        return {"total_keys": len(keys), "keys": keys}
