from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

from ziva.shared_types import RuntimeContext, ToolResult


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

        runtime = ctx.metadata.get("_runtime")
        if not runtime:
            return ToolResult(text="Error: runtime_unavailable\nRuntime is required for read_skill", error=True)

        skill = runtime.get_skill(skill_name)
        if skill is None:
            available = sorted((runtime._skill_by_name or {}).keys())
            return ToolResult(text=f"Error: skill_not_found\nSkill '{skill_name}' not found. Available: {', '.join(available)}", error=True)

        path = Path(skill["path"])
        if not path.exists():
            return ToolResult(text=f"Error: file_not_found\nSkill file not found: {path}", error=True)

        content = path.read_text(encoding="utf-8").strip()
        content = content.replace("{baseDir}", str(path.parent))
        return ToolResult(text=content, metadata={"name": skill_name})
