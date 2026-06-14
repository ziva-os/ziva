from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

from ziva_runtime.shared_types import RuntimeContext, ToolResult


def _build_skill_index(config: Dict[str, Any]) -> list[dict]:
    """Scan skill directories on demand.

    Mirrors Runtime.build_skill_index so tools do not depend on a baked-in
    `_skill_index` config key.
    """
    index: list[dict] = []
    for sp in config.get("skill", {}).get("extra_paths", []):
        p = Path(sp).expanduser().resolve()
        if not p.exists():
            continue
        for skill_file in p.rglob("SKILL.md"):
            try:
                raw = skill_file.read_text(encoding="utf-8").strip()
            except OSError:
                continue
            if not raw:
                continue
            name = skill_file.parent.name
            desc = ""
            if raw.startswith("---"):
                end = raw.find("---", 3)
                if end > 0:
                    fm = raw[3:end]
                    for line in fm.splitlines():
                        if line.startswith("description:"):
                            desc = line.split(":", 1)[1].strip().strip('"').strip("'")
                            break
                    if not desc:
                        for line in fm.splitlines():
                            if line.startswith("name:"):
                                name = line.split(":", 1)[1].strip().strip('"').strip("'")
            index.append({"name": name, "description": desc, "path": str(skill_file)})
    return index


class ReadSkillTool:
    """Load the full content of a skill by name."""

    def spec(self) -> Dict[str, Any]:
        return {
            "name": "read_skill",
            "description": (
                "Load the full instructions for a skill by name. "
                "Use this when you decide to use a skill listed in the Available Skills section. "
                "Returns the complete SKILL.md content including scripts, references, and usage details."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "The skill name to load (must match a name from Available Skills)",
                    },
                },
                "required": ["name"],
            },
        }

    async def run(self, input_data: Dict[str, Any], ctx: RuntimeContext) -> ToolResult:
        skill_name = input_data.get("name", "").strip()
        if not skill_name:
            return ToolResult(text="Error: missing_name\nname is required", error=True)

        skill_index = _build_skill_index(ctx.config)
        for skill in skill_index:
            if skill["name"] == skill_name:
                path = Path(skill["path"])
                if path.exists():
                    content = path.read_text(encoding="utf-8").strip()
                    # Resolve {baseDir} placeholder
                    base_dir = str(path.parent)
                    content = content.replace("{baseDir}", base_dir)
                    return ToolResult(text=content, metadata={"name": skill_name})
                else:
                    return ToolResult(text=f"Error: file_not_found\nSkill file not found: {path}", error=True)

        available = [s["name"] for s in skill_index]
        return ToolResult(text=f"Error: skill_not_found\nSkill '{skill_name}' not found. Available: {', '.join(available)}", error=True)
