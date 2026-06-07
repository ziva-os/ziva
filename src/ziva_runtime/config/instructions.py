from __future__ import annotations
from pathlib import Path

INSTRUCTION_FILES = [
    ("global", Path.home() / ".ziva" / "AGENTS.md"),
    ("project", "AGENTS.md"),
    ("ziva", ".ziva/AGENTS.md"),
    ("claude", "CLAUDE.md"),
]


def load_layered_instructions(workspace_root: Path) -> str:
    sections = []
    for label, location in INSTRUCTION_FILES:
        if isinstance(location, Path):
            path = location
        else:
            path = workspace_root / location
        if path.exists() and path.is_file():
            content = path.read_text(encoding="utf-8").strip()
            if content:
                sections.append(content)
    return "\n\n".join(sections)
