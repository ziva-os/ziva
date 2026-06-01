from __future__ import annotations

import asyncio
import shutil
from pathlib import Path
from typing import Any, Dict


class GlobTool:
    """Find files matching a glob pattern. Uses ripgrep when available for performance."""

    # Directories to skip during glob searches
    SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", ".env", "dist", "build"}
    MAX_RESULTS = 100
    MAX_OUTPUT_CHARS = 100_000

    def spec(self):
        return {
            "name": "glob",
            "description": "Find files matching a glob pattern. Uses ripgrep for performance on large repos. Returns relative paths sorted by modification time (oldest first).",
            "input_schema": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "Glob pattern (e.g., '**/*.py', 'src/**/*.ts')"},
                    "path": {"type": "string", "description": "Root directory to search (default '.')"},
                },
                "required": ["pattern"],
            },
        }

    async def run(self, input_data: Dict[str, Any], ctx: Any) -> Dict[str, Any]:
        pattern = input_data.get("pattern")
        if not pattern:
            return {"error": "invalid_input", "message": "pattern is required"}

        root_path = Path(input_data.get("path", ".")).resolve()

        if not root_path.exists():
            return {"error": "path_not_found", "message": f"Path not found: {root_path}"}

        if not root_path.is_dir():
            return {"error": "not_a_directory", "message": f"Path is not a directory: {root_path}"}

        try:
            # Prefer ripgrep for performance
            rg_path = shutil.which("rg")
            if rg_path:
                results = await self._rg_glob(rg_path, pattern, root_path)
            else:
                results = self._python_glob(pattern, root_path)

            # Sort by modification time (oldest first) to surface stable files
            results.sort(key=lambda p: (root_path / p).stat().st_mtime if (root_path / p).exists() else 0)

            truncated = len(results) >= self.MAX_RESULTS
            if truncated:
                results = results[:self.MAX_RESULTS]

            # Build output and check size
            output = {
                "matches": results,
                "total": len(results),
                "truncated": truncated,
            }
            out_str = str(output)
            if truncated:
                output["note"] = f"Results are truncated to {self.MAX_RESULTS} matches. Narrow your pattern if needed."
            if len(out_str) > self.MAX_OUTPUT_CHARS:
                output["matches"] = results[:50]
                output["total"] = len(output["matches"])
                output["truncated"] = True
                output["note"] = f"Output too large; limited to 50 matches. Use a more specific pattern."

            return output

        except PermissionError as e:
            return {"error": "permission_denied", "message": f"Permission denied: {e}"}
        except Exception as e:
            return {"error": "glob_failed", "message": str(e)}

    async def _rg_glob(self, rg_path: str, pattern: str, root_path: Path) -> list[str]:
        """Use ripgrep --files --glob for fast file listing."""
        cmd = [
            rg_path,
            "--files",
            "--glob", pattern,
            "--sort", "modified",
            "--no-ignore",
            "--hidden",
            "-g", "!.git",
        ]
        for d in self.SKIP_DIRS:
            cmd.extend(["-g", f"!{d}"])
        cmd.append(str(root_path))

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _stderr = await asyncio.wait_for(proc.communicate(), timeout=20)

        results = []
        for line in stdout.decode("utf-8", errors="ignore").splitlines():
            line = line.strip()
            if not line:
                continue
            # Convert to relative path under root_path
            p = Path(line)
            try:
                rel = p.relative_to(root_path)
                results.append(str(rel))
            except ValueError:
                results.append(line)
            if len(results) >= self.MAX_RESULTS * 2:  # collect a bit more for sorting
                break
        return results

    def _python_glob(self, pattern: str, root_path: Path) -> list[str]:
        """Fallback to pathlib glob."""
        results = []
        count = 0
        limit = self.MAX_RESULTS * 2

        if pattern.startswith("**"):
            search_pattern = pattern.replace("**", "").lstrip("/")
            iterator = root_path.rglob(search_pattern)
        else:
            iterator = root_path.glob(pattern)

        for match in iterator:
            if count >= limit:
                break
            if self._should_include(match):
                try:
                    rel = match.relative_to(root_path)
                    results.append(str(rel))
                except ValueError:
                    results.append(str(match))
                count += 1
        return results

    def _should_include(self, path: Path) -> bool:
        """Check if a path should be included in results."""
        for part in path.parts:
            if part in self.SKIP_DIRS:
                return False
        return True
