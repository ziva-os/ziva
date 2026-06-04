import os
import re
from pathlib import Path


class ApplyPatchTool:
    """Apply file changes using Codex CLI compatible patch format.

    Supports three input styles:
    1. Legacy Ziva multi-file patch string (*** Begin Patch ... *** End Patch)
    2. Standard unified diff string
    3. Codex CLI style single operation object
    """

    def spec(self):
        return {
            "name": "edit_file",
            "description": (
                "Apply file changes using unified diffs. "
                "Can create, delete, or update files. "
                "Prefer this tool for all file edits."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "patch": {
                        "type": "string",
                        "description": "Patch content: either a unified diff or a legacy multi-file patch",
                    },
                    "operation": {
                        "type": "object",
                        "description": "Single file operation (Codex CLI style)",
                        "properties": {
                            "type": {
                                "type": "string",
                                "enum": ["create_file", "delete_file", "update_file"],
                                "description": "Operation type",
                            },
                            "path": {
                                "type": "string",
                                "description": "File path relative to cwd",
                            },
                            "diff": {
                                "type": "string",
                                "description": "Unified diff content (for update_file) or full file content (for create_file)",
                            },
                        },
                        "required": ["type", "path"],
                    },
                    "cwd": {
                        "type": "string",
                        "description": "Current working directory for resolving paths (default: os.getcwd())",
                    },
                },
            },
        }

    async def run(self, input_data, _ctx):
        cwd = input_data.get("cwd", os.getcwd())
        base_dir = Path(cwd)

        # Codex CLI style single operation
        operation = input_data.get("operation")
        if operation:
            return self._apply_operation(operation, base_dir)

        patch_content = input_data.get("patch", "")
        if not patch_content:
            return {"error": "invalid_patch", "message": "patch or operation is required"}

        # Legacy Ziva format
        if "*** Begin Patch" in patch_content and "*** End Patch" in patch_content:
            return self._parse_and_apply_legacy(patch_content, base_dir)

        # Standard unified diff
        if patch_content.lstrip().startswith("---"):
            return self._parse_and_apply_unified(patch_content, base_dir)

        return {"error": "invalid_patch", "message": "Unrecognized patch format"}

    # ---------- Codex CLI style single operation ----------

    def _apply_operation(self, op, base_dir):
        op_type = op.get("type")
        path = base_dir / op.get("path", "")

        if op_type == "create_file":
            content = op.get("diff", "")
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content, encoding="utf-8")
                return {"applied": 1, "files_changed": [str(path.relative_to(base_dir))]}
            except Exception as e:
                return {"error": "write_failed", "message": str(e)}

        if op_type == "delete_file":
            if not path.exists():
                return {"error": "file_not_found", "message": f"File not found: {path}"}
            try:
                path.unlink()
                return {"applied": 1, "files_changed": [str(path.relative_to(base_dir))]}
            except Exception as e:
                return {"error": "delete_failed", "message": str(e)}

        if op_type == "update_file":
            if not path.exists():
                return {"error": "file_not_found", "message": f"File not found: {path}"}
            diff_text = op.get("diff", "")
            return self._apply_unified_diff_to_file(diff_text, path, base_dir)

        return {"error": "unknown_operation", "message": f"Unknown operation type: {op_type}"}

    # ---------- Legacy Ziva format ----------

    def _parse_and_apply_legacy(self, patch_content, base_dir):
        match = re.search(r"\*\*\* Begin Patch\s+(.*?)\s+\*\*\* End Patch", patch_content, re.DOTALL)
        if not match:
            return {"error": "invalid_patch", "message": "Invalid legacy patch format"}

        body = match.group(1)
        applied = 0
        files_changed = []
        errors = []

        for op in self._parse_legacy_operations(body):
            try:
                result = self._apply_legacy_operation(op, base_dir)
                if result.get("error"):
                    errors.append(result)
                else:
                    applied += 1
                    if result.get("file"):
                        files_changed.append(result["file"])
            except Exception as e:
                errors.append({"error": "operation_failed", "message": str(e)})

        if errors:
            return {"applied": applied, "files_changed": files_changed, "errors": errors}
        return {"applied": applied, "files_changed": files_changed}

    def _parse_legacy_operations(self, body):
        operations = []
        lines = body.split("\n")
        i = 0
        while i < len(lines):
            line = lines[i].strip()
            if line.startswith("*** Add File:"):
                path = line[len("*** Add File:"):].strip()
                content_lines = []
                i += 1
                while i < len(lines) and not lines[i].strip().startswith("***"):
                    content_lines.append(lines[i])
                    i += 1
                operations.append({"type": "add", "path": path, "content": "\n".join(content_lines)})
                continue
            elif line.startswith("*** Delete File:"):
                path = line[len("*** Delete File:"):].strip()
                operations.append({"type": "delete", "path": path})
                i += 1
            elif line.startswith("*** Move File:"):
                rest = line[len("*** Move File:"):].strip()
                if " -> " in rest:
                    old_path, new_path = rest.split(" -> ", 1)
                    operations.append({"type": "move", "old_path": old_path.strip(), "new_path": new_path.strip()})
                i += 1
            elif line.startswith("*** Update File:"):
                path = line[len("*** Update File:"):].strip()
                hunks = []
                i += 1
                while i < len(lines):
                    if lines[i].strip().startswith("***"):
                        break
                    hunk_match = re.match(r"^@@ -(\d+),?(\d*)\s+\+?(\d+),?(\d*)\s+@@", lines[i])
                    if hunk_match:
                        old_start = int(hunk_match.group(1))
                        old_count = int(hunk_match.group(2)) if hunk_match.group(2) else 1
                        new_start = int(hunk_match.group(3))
                        new_count = int(hunk_match.group(4)) if hunk_match.group(4) else 1
                        hunk_lines = []
                        i += 1
                        while i < len(lines):
                            if lines[i].startswith("@@") or lines[i].strip().startswith("***"):
                                break
                            hunk_lines.append(lines[i])
                            i += 1
                        hunks.append({"old_start": old_start, "old_count": old_count, "new_start": new_start, "new_count": new_count, "lines": hunk_lines})
                        continue
                    i += 1
                operations.append({"type": "update", "path": path, "hunks": hunks})
            else:
                i += 1
        return operations

    def _apply_legacy_operation(self, op, base_dir):
        t = op["type"]
        if t == "add":
            p = base_dir / op["path"]
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(op.get("content", ""))
            return {"file": str(p.relative_to(base_dir))}
        if t == "delete":
            p = base_dir / op["path"]
            if p.exists():
                p.unlink()
            return {"file": str(p.relative_to(base_dir))}
        if t == "move":
            old = base_dir / op["old_path"]
            new = base_dir / op["new_path"]
            new.parent.mkdir(parents=True, exist_ok=True)
            old.rename(new)
            return {"file": str(new.relative_to(base_dir))}
        if t == "update":
            return self._apply_legacy_update(op, base_dir)
        return {"error": "unknown_operation", "message": f"Unknown: {t}"}

    def _apply_legacy_update(self, op, base_dir):
        path = base_dir / op["path"]
        if not path.exists():
            return {"error": "file_not_found", "message": f"File not found: {op['path']}"}
        original_lines = path.read_text().split("\n")
        new_lines = original_lines.copy()
        errors = []
        for hunk in reversed(op["hunks"]):
            try:
                new_lines = self._apply_legacy_hunk(new_lines, hunk)
            except ValueError as e:
                errors.append({"error": "hunk_mismatch", "message": str(e)})
        if errors:
            return {"error": "hunk_failed", "message": "Failed to apply one or more hunks", "errors": errors}
        path.write_text("\n".join(new_lines))
        return {"file": str(path.relative_to(base_dir))}

    def _apply_legacy_hunk(self, lines, hunk):
        old_start = hunk["old_start"] - 1
        hunk_lines = hunk["lines"]
        old_lines = []
        new_lines = []
        for hl in hunk_lines:
            if hl.startswith("-"):
                old_lines.append(hl[1:])
            elif hl.startswith("+"):
                new_lines.append(hl[1:])
            elif hl.startswith(" "):
                old_lines.append(hl[1:])
                new_lines.append(hl[1:])
        if old_start + len(old_lines) > len(lines):
            raise ValueError(f"Hunk start {hunk['old_start']} exceeds file length")
        expected = lines[old_start:old_start + len(old_lines)]
        if expected != old_lines:
            raise ValueError(f"Context mismatch at line {hunk['old_start']}")
        return lines[:old_start] + new_lines + lines[old_start + len(old_lines):]

    # ---------- Standard unified diff ----------

    def _parse_and_apply_unified(self, patch_content, base_dir):
        files_changed = []
        errors = []
        applied = 0

        # Split into per-file diffs
        file_diffs = self._split_unified_diff(patch_content)
        for diff_text, target_path in file_diffs:
            result = self._apply_unified_diff_to_file(diff_text, target_path, base_dir)
            if result.get("error"):
                errors.append(result)
            else:
                applied += 1
                files_changed.append(result.get("file", str(target_path)))

        if errors:
            return {"applied": applied, "files_changed": files_changed, "errors": errors}
        return {"applied": applied, "files_changed": files_changed}

    def _split_unified_diff(self, text):
        """Split a unified diff into (diff_text, target_path) pairs."""
        diffs = []
        lines = text.splitlines()
        i = 0
        while i < len(lines):
            if lines[i].startswith("--- "):
                old_file = lines[i][4:].split("\t")[0].strip()
                if i + 1 < len(lines) and lines[i + 1].startswith("+++ "):
                    new_file = lines[i + 1][4:].split("\t")[0].strip()
                    target = new_file.lstrip("b/")
                    diff_lines = [lines[i], lines[i + 1]]
                    i += 2
                    while i < len(lines) and not lines[i].startswith("--- "):
                        diff_lines.append(lines[i])
                        i += 1
                    diffs.append(("\n".join(diff_lines), target))
                    continue
            i += 1
        return diffs

    def _apply_unified_diff_to_file(self, diff_text, target_path, base_dir):
        path = base_dir / target_path
        if not path.exists():
            # Treat as create if file doesn't exist
            path.parent.mkdir(parents=True, exist_ok=True)
            original = ""
        else:
            original = path.read_text(encoding="utf-8")

        try:
            new_content = self._apply_unified_diff_text(original, diff_text)
        except ValueError as e:
            return {"error": "diff_failed", "message": str(e), "file": str(target_path)}

        path.write_text(new_content, encoding="utf-8")
        return {"file": str(path.relative_to(base_dir))}

    def _apply_unified_diff_text(self, original, diff_text):
        """Apply a unified diff to original text."""
        original_lines = original.splitlines() if original else []
        result_lines = list(original_lines)

        # Parse hunks
        lines = diff_text.splitlines()
        i = 0
        # Skip header lines (--- and +++)
        while i < len(lines) and not lines[i].startswith("@@"):
            i += 1

        hunks = []
        while i < len(lines):
            if lines[i].startswith("@@"):
                m = re.match(r"@@ -(\d+),?(\d*)\s+\+(\d+),?(\d*)\s+@@", lines[i])
                if m:
                    old_start = int(m.group(1))
                    old_count = int(m.group(2)) if m.group(2) else 1
                    new_start = int(m.group(3))
                    new_count = int(m.group(4)) if m.group(4) else 1
                    hunk_lines = []
                    i += 1
                    while i < len(lines) and not lines[i].startswith("@@") and not lines[i].startswith("---"):
                        hunk_lines.append(lines[i])
                        i += 1
                    hunks.append((old_start, old_count, new_start, new_count, hunk_lines))
                    continue
            i += 1

        # Apply hunks in reverse order (bottom-up) to preserve line numbers
        for old_start, old_count, new_start, new_count, hunk_lines in reversed(hunks):
            old_lines = []
            new_lines = []
            for hl in hunk_lines:
                if hl.startswith("-"):
                    old_lines.append(hl[1:])
                elif hl.startswith("+"):
                    new_lines.append(hl[1:])
                elif hl.startswith(" "):
                    old_lines.append(hl[1:])
                    new_lines.append(hl[1:])
                elif hl == "\\ No newline at end of file":
                    pass
                else:
                    # Context line without leading space (sometimes happens)
                    old_lines.append(hl)
                    new_lines.append(hl)

            idx = old_start - 1
            if idx > len(result_lines):
                raise ValueError(f"Hunk start {old_start} exceeds file length {len(result_lines)}")
            actual = result_lines[idx:idx + len(old_lines)]
            if actual != old_lines:
                raise ValueError(f"Context mismatch at line {old_start}: expected {old_lines[:2]}, got {actual[:2]}")
            result_lines = result_lines[:idx] + new_lines + result_lines[idx + len(old_lines):]

        return "\n".join(result_lines)
