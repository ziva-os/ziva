from __future__ import annotations
from pathlib import Path

# System-prompt instruction sources, loaded in order and concatenated.
# Only these two are read — the global file first, then the workspace's
# `.ziva/AGENTS.md`. A `Path` entry is absolute (resolved once, at import);
# a `str` entry is resolved relative to the workspace root at load time.
# Project-root `AGENTS.md` and `CLAUDE.md` are intentionally NOT read — a
# workspace's instructions live under `.ziva/`.
INSTRUCTION_FILES = [
    Path.home() / ".ziva" / "AGENTS.md",
    ".ziva/AGENTS.md",
]


def load_layered_instructions(workspace_root: Path) -> str:
    sections = []
    for location in INSTRUCTION_FILES:
        path = location if isinstance(location, Path) else workspace_root / location
        if path.exists() and path.is_file():
            content = path.read_text(encoding="utf-8").strip()
            if content:
                sections.append(content)
    return "\n\n".join(sections)
