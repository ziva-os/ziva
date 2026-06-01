import asyncio
import os
import re
import shutil


class GrepTool:
    """Search tool that shells out to ripgrep or grep for performance."""

    SKIP_DIRS = {
        ".git", ".svn", ".hg", "node_modules", "__pycache__",
        ".venv", "venv", ".pytest_cache", ".mypy_cache",
        "dist", "build", ".next", "target", "vendor",
        ".idea", ".vscode", ".vs",
    }

    def __init__(self):
        self._rg_path = None
        self._grep_path = None

    def _detect_tools(self):
        """Detect available grep tools."""
        if self._rg_path is None:
            self._rg_path = shutil.which("rg")
        if self._grep_path is None:
            self._grep_path = shutil.which("grep")

    def spec(self):
        return {
            "name": "grep",
            "description": "Search for a regex pattern in text files within a directory",
            "input_schema": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "Regex pattern to search for"},
                    "path": {"type": "string", "description": "Directory to search in (default: current directory)"},
                    "max_results": {"type": "integer", "description": "Maximum matches to return (default: 200)"},
                    "file_pattern": {"type": "string", "description": "Glob pattern to filter files (e.g., '*.py', '*.js')"},
                    "context_lines": {"type": "integer", "description": "Number of context lines to show (like -C in grep)"},
                    "case_insensitive": {"type": "boolean", "description": "Case-insensitive search (-i)"},
                },
                "required": ["pattern"],
            },
        }

    async def run(self, input_data, ctx):
        pattern = input_data.get("pattern", "")
        path = input_data.get("path", ".")
        max_results = input_data.get("max_results", 200)
        file_pattern = input_data.get("file_pattern", "")
        context_lines = input_data.get("context_lines", 0)
        case_insensitive = input_data.get("case_insensitive", False)

        if not pattern:
            return {"error": "invalid_input", "message": "pattern is required"}

        # Validate regex pattern
        try:
            re.compile(pattern)
        except re.error as e:
            return {"error": "invalid_regex", "message": str(e)}

        path = os.path.abspath(path)

        if not os.path.exists(path):
            return {"error": "path_not_found", "message": f"Path not found: {path}"}

        if not os.path.isdir(path):
            return {"error": "not_a_directory", "message": f"Path is not a directory: {path}"}

        self._detect_tools()

        # Try ripgrep first, then grep, then fall back to Python
        if self._rg_path:
            return await self._ripgrep_search(
                pattern, path, max_results, file_pattern, context_lines, case_insensitive
            )
        elif self._grep_path:
            return await self._grep_search(
                pattern, path, max_results, file_pattern, context_lines, case_insensitive
            )
        else:
            return await self._python_search(
                pattern, path, max_results, file_pattern, case_insensitive
            )

    async def _ripgrep_search(self, pattern, path, max_results, file_pattern, context_lines, case_insensitive):
        """Search using ripgrep (rg)."""
        assert self._rg_path is not None, "ripgrep not available"
        cmd = [self._rg_path]

        if case_insensitive:
            cmd.append("-i")

        if context_lines:
            cmd.extend(["-C", str(context_lines)])

        if file_pattern:
            cmd.extend(["-g", file_pattern])

        # Add skip dirs
        for skip_dir in self.SKIP_DIRS:
            cmd.extend(["--glob", f"!{skip_dir}"])

        # Output format: filename:line:content
        cmd.extend(["--no-heading", "--line-number", "--color=never"])

        cmd.append(pattern)
        cmd.append(path)

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)

            if proc.returncode not in (0, 1):  # 0 = matches, 1 = no matches
                return {"error": "search_failed", "message": stderr.decode("utf-8", errors="replace")}

            matches = self._parse_grep_output(stdout.decode("utf-8", errors="replace"), path, max_results)
            return {"matches": matches, "total": len(matches), "truncated": len(matches) >= max_results}

        except asyncio.TimeoutError:
            return {"error": "timeout", "message": "Search timed out"}
        except Exception as e:
            return {"error": "search_failed", "message": str(e)}

    async def _grep_search(self, pattern, path, max_results, file_pattern, context_lines, case_insensitive):
        """Search using system grep."""
        assert self._grep_path is not None, "grep not available"
        cmd = [self._grep_path]

        if case_insensitive:
            cmd.append("-i")

        if context_lines:
            cmd.extend(["-C", str(context_lines)])

        if file_pattern:
            cmd.extend(["--include", file_pattern])

        # Add skip dirs (grep doesn't have --glob, use --exclude-dir)
        for skip_dir in self.SKIP_DIRS:
            cmd.extend(["--exclude-dir", skip_dir])

        # Recursive, line number, with filename
        cmd.extend(["-r", "-n", "-H"])

        cmd.append(pattern)
        cmd.append(path)

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=30)

            if proc.returncode not in (0, 1):
                return {"error": "search_failed", "message": "grep search failed"}

            matches = self._parse_grep_output(stdout.decode("utf-8", errors="replace"), path, max_results)
            return {"matches": matches, "total": len(matches), "truncated": len(matches) >= max_results}

        except asyncio.TimeoutError:
            return {"error": "timeout", "message": "Search timed out"}
        except Exception as e:
            return {"error": "search_failed", "message": str(e)}

    async def _python_search(self, pattern, path, max_results, file_pattern, case_insensitive):
        """Fallback Python implementation with improved features."""
        import re
        from fnmatch import fnmatch

        try:
            flags = re.IGNORECASE if case_insensitive else 0
            regex = re.compile(pattern, flags)
        except re.error as e:
            return {"error": "invalid_regex", "message": str(e)}

        matches = []

        try:
            for root, dirs, files in os.walk(path):
                # Skip directories
                dirs[:] = [d for d in dirs if d not in self.SKIP_DIRS]

                for filename in files:
                    if len(matches) >= max_results:
                        return {"matches": matches, "total": len(matches), "truncated": True}

                    filepath = os.path.join(root, filename)

                    if not os.path.isfile(filepath):
                        continue

                    # Check file pattern
                    if file_pattern and not fnmatch(filename, file_pattern):
                        continue

                    if self._is_binary(filepath):
                        continue

                    try:
                        with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
                            for line_num, line in enumerate(f, 1):
                                try:
                                    if regex.search(line):
                                        relpath = os.path.relpath(filepath, path)
                                        matches.append({
                                            "file": relpath,
                                            "line": line_num,
                                            "content": line.rstrip("\n\r"),
                                        })
                                        if len(matches) >= max_results:
                                            return {"matches": matches, "total": len(matches), "truncated": True}
                                except re.error:
                                    pass
                    except (IOError, OSError):
                        pass
        except OSError:
            pass

        return {"matches": matches, "total": len(matches), "truncated": False}

    def _parse_grep_output(self, output, base_path, max_results):
        """Parse grep/ripgrep output into match objects."""
        matches = []
        for line in output.split("\n"):
            if not line or len(matches) >= max_results:
                break

            # Format: filename:line:content
            parts = line.split(":", 2)
            if len(parts) >= 3:
                try:
                    relpath = os.path.relpath(parts[0], base_path)
                    matches.append({
                        "file": relpath,
                        "line": int(parts[1]),
                        "content": parts[2],
                    })
                except (ValueError, OSError):
                    continue

        return matches

    def _is_binary(self, filepath):
        """Check if a file is binary."""
        try:
            with open(filepath, "rb") as f:
                chunk = f.read(8192)
                return b"\x00" in chunk
        except (IOError, OSError):
            return False
