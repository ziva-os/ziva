"""Shared diff utilities for file editing tools."""
import difflib


def normalize_line_endings(text: str) -> str:
    """Normalize all line endings to LF."""
    return text.replace("\r\n", "\n").replace("\r", "\n")


def trim_diff(diff: str) -> str:
    """Trim diff output to remove header/footer.

    Handles both full diffs (with ---/+++ headers) and short diffs.
    """
    lines = diff.split("\n")
    if len(lines) <= 2:
        return diff

    # Find the start of the diff (first line starting with + or -)
    start_idx = 0
    for i, line in enumerate(lines):
        if line.startswith(("+", "-")):
            start_idx = i
            break

    # Find the end of the diff (last hunk line)
    end_idx = len(lines)
    for i in range(len(lines) - 1, -1, -1):
        if lines[i].startswith(("+", "-", "@@", " ")):
            end_idx = i + 1
            break

    # If we found proper diff content, trim
    if start_idx > 0 or end_idx < len(lines):
        # Include the hunk headers (@@ lines)
        for i in range(start_idx - 1, -1, -1):
            if lines[i].startswith("@@"):
                start_idx = i
                break
        return "\n".join(lines[start_idx:end_idx])

    # Fallback for short diffs - trim header/footer
    if len(lines) > 4:
        return "\n".join(lines[2:-2])

    return diff


def create_diff(old_content: str, new_content: str, filepath: str) -> str:
    """Create a unified diff between two content strings."""
    if not old_content:
        old_lines = []
    else:
        old_lines = normalize_line_endings(old_content).splitlines(keepends=True)

    if not new_content:
        new_lines = []
    else:
        new_lines = normalize_line_endings(new_content).splitlines(keepends=True)

    # Ensure last line has newline
    if old_lines and not old_lines[-1].endswith("\n"):
        old_lines[-1] += "\n"
    if new_lines and not new_lines[-1].endswith("\n"):
        new_lines[-1] += "\n"

    diff = difflib.unified_diff(
        old_lines,
        new_lines,
        fromfile=filepath,
        tofile=filepath,
        lineterm=""
    )
    result = "\n".join(diff)
    return trim_diff(result)
