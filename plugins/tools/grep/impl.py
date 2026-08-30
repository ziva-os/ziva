import asyncio
import os
import re
import shutil

from ziva.shared_types import ToolResult, resolve_tool_path


# Matches Claude Code's Grep tool interface. Key behaviour:
#   - `path` accepts either a file or a directory (the model passes a
#     specific file when it already knows which one to look at; this
#     mirrors how Claude Code / Codex behave)
#   - `output_mode` switches between `content` (lines with matches,
#     the default), `files_with_matches` (just file names), and `count`
#     (per-file match count)
#   - `include` (Claude Code's name) is the file-filter glob; the
#     older `glob` / `include` aliases have been removed.
#   - `multiline` makes `.` match newlines so multi-line patterns work
#     (e.g. `struct \{[\s\S]*?field`)
#   - `line_number` toggles the "file:line:" prefix in content mode
#     (default true, matches Claude Code's `-n`).
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
            "description": (
                "Search for a regex pattern in files. `path` may be either a "
                "directory (searched recursively) or a single file (searched "
                "directly, no recursion). Output format is controlled by "
                "`output_mode`: 'content' shows matching lines with line "
                "numbers, 'files_with_matches' lists only the file paths, "
                "'count' shows a per-file match count."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "pattern": {
                        "type": "string",
                        "description": "Regex pattern to search for.",
                    },
                    "path": {
                        "type": "string",
                        "description": (
                            "File or directory to search in. Defaults to the "
                            "session's workspace directory. When a file is given, "
                            "only that file is searched and `glob` is ignored."
                        ),
                    },
                    "output_mode": {
                        "type": "string",
                        "enum": ["content", "files_with_matches", "count"],
                        "description": (
                            "Output format. 'content' (default) returns the "
                            "matching lines; 'files_with_matches' returns the "
                            "paths of files that contain at least one match; "
                            "'count' returns a per-file match count."
                        ),
                    },
                    "head_limit": {
                        "type": "integer",
                        "description": "Maximum number of matches / lines to return. Default 200.",
                    },
                    "include": {
                        "type": "string",
                        "description": (
                            "Glob pattern to filter files (e.g. '*.py', "
                            "'!**/test_*'). Only applies when `path` is a "
                            "directory. Matches Claude Code's `include` "
                            "parameter naming."
                        ),
                    },
                    "context_lines": {
                        "type": "integer",
                        "description": "Number of context lines to show around each match (-C).",
                    },
                    "case_insensitive": {
                        "type": "boolean",
                        "description": "Case-insensitive search (-i).",
                    },
                    "multiline": {
                        "type": "boolean",
                        "description": (
                            "If true, treat the pattern as multi-line: `^` and "
                            "`$` match line boundaries and `.` matches "
                            "newlines. Useful for searching multi-line code "
                            "blocks."
                        ),
                    },
                    "line_number": {
                        "type": "boolean",
                        "description": (
                            "Show line numbers in the output (only applies to "
                            "output_mode: 'content'). Default true. Set to "
                            "false to get just the matching text, without "
                            "the 'file:line:content' prefix — useful when "
                            "embedding results into a downstream prompt."
                        ),
                        "default": True,
                    },
                },
                "required": ["pattern"],
            },
        }

    async def run(self, input_data, ctx):
        pattern = input_data.get("pattern", "")
        path = input_data.get("path")
        head_limit = input_data.get("head_limit", 200)
        include = input_data.get("include", "")
        context_lines = input_data.get("context_lines", 0)
        case_insensitive = input_data.get("case_insensitive", False)
        output_mode = input_data.get("output_mode", "content")
        multiline = input_data.get("multiline", False)
        line_number = input_data.get("line_number", True)

        if output_mode not in ("content", "files_with_matches", "count"):
            return ToolResult(
                text=f"Error: invalid_input\noutput_mode must be one of content, files_with_matches, count (got {output_mode!r})",
                error=True,
            )

        if not pattern:
            return ToolResult(text="Error: invalid_input\npattern is required", error=True)

        # Validate regex pattern (Python-side; ripgrep / grep will validate
        # again at exec time). For multi-line we set DOTALL so `.` matches
        # newlines and use multiline mode for `^`/`$` semantics.
        flags = re.DOTALL if multiline else 0
        if case_insensitive:
            flags |= re.IGNORECASE
        try:
            re.compile(pattern, flags)
        except re.error as e:
            return ToolResult(text=f"Error: invalid_regex\n{e}", error=True)

        path = os.path.abspath(resolve_tool_path(ctx, path))

        if not os.path.exists(path):
            return ToolResult(
                text=f"Error: path_not_found\nPath not found: {path}",
                error=True,
            )

        is_file = os.path.isfile(path)
        is_dir = os.path.isdir(path)
        if not (is_file or is_dir):
            return ToolResult(
                text=f"Error: not_a_path\nPath is neither a file nor a directory: {path}",
                error=True,
            )

        # A glob doesn't apply when searching a single file — silently
        # drop it so callers can pass a glob unconditionally and still
        # get a sane result if `path` happens to be a file.
        if is_file:
            include = ""

        self._detect_tools()

        if self._rg_path:
            return await self._ripgrep_search(
                pattern, path, is_file, head_limit, include,
                context_lines, case_insensitive, multiline, output_mode,
                line_number,
            )
        elif self._grep_path:
            return await self._grep_search(
                pattern, path, is_file, head_limit, include,
                context_lines, case_insensitive, multiline, output_mode,
                line_number,
            )
        else:
            return await self._python_search(
                pattern, path, is_file, head_limit, include,
                case_insensitive, multiline, output_mode, line_number,
            )

    # ---- ripgrep ----
    async def _ripgrep_search(
        self, pattern, path, is_file, head_limit, include,
        context_lines, case_insensitive, multiline, output_mode, line_number,
    ):
        assert self._rg_path is not None, "ripgrep not available"
        cmd = [self._rg_path]

        if case_insensitive:
            cmd.append("-i")

        if multiline:
            cmd.append("--multiline")
            # --multiline requires --multiline-dotall to make `.` cross
            # newlines; without it the flag is essentially a no-op for
            # our use case.
            cmd.append("--multiline-dotall")

        if context_lines:
            cmd.extend(["-C", str(context_lines)])

        # output_mode: rg has direct flags for two of the three modes.
        if output_mode == "files_with_matches":
            cmd.append("--files-with-matches")
        elif output_mode == "count":
            cmd.append("--count-matches")
        else:
            # content (default). --no-heading keeps the
            # "file:line:content" shape on one line. --line-number is
            # gated by `line_number` so callers can request bare
            # matching text when they don't need to cite specific lines.
            cmd.extend(["--no-heading", "--color=never"])
            if line_number:
                cmd.append("--line-number")

        if include:
            cmd.extend(["-g", include])

        # For dir searches, skip the usual noise dirs. For single files
        # there's no recursion so this is wasted.
        if not is_file:
            for skip_dir in self.SKIP_DIRS:
                cmd.extend(["--glob", f"!{skip_dir}"])

        cmd.append(pattern)
        cmd.append(path)

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout, stderr = await proc.communicate()
            except asyncio.CancelledError:
                proc.kill()
                try: await proc.wait()
                except Exception: pass
                raise

            if proc.returncode not in (0, 1):  # 0 = matches, 1 = no matches
                return ToolResult(
                    text=f"Error: search_failed\n{stderr.decode('utf-8', errors='replace')}",
                    error=True,
                )

            text, metadata = self._format_output(
                stdout.decode("utf-8", errors="replace"),
                output_mode,
                path,
                head_limit,
                is_file=is_file,
                numbered=line_number,
            )
            return ToolResult(text=text, metadata=metadata)

        except asyncio.TimeoutError:
            return ToolResult(text="Error: timeout\nSearch timed out", error=True)
        except Exception as e:
            return ToolResult(text=f"Error: search_failed\n{e}", error=True)

    # ---- grep ----
    async def _grep_search(
        self, pattern, path, is_file, head_limit, include,
        context_lines, case_insensitive, multiline, output_mode,
        line_number=True,
    ):
        assert self._grep_path is not None, "grep not available"
        cmd = [self._grep_path]

        if case_insensitive:
            cmd.append("-i")

        if context_lines:
            cmd.extend(["-C", str(context_lines)])

        if output_mode == "files_with_matches":
            cmd.append("-l")  # list files only
        elif output_mode == "count":
            cmd.append("-c")  # count per file
        else:
            # content. -H always shows the filename; -n is gated by
            # `line_number` (the parser is told via `numbered=`).
            cmd.append("-H")
            if line_number:
                cmd.append("-n")

        if include:
            cmd.extend(["--include", include])

        # Multi-line: GNU grep uses `-P` (PCRE) with the (?s) / (?m)
        # modifiers via -z (null-delimited input) to treat the file as
        # one big record. Without -P / -z the pattern still works on a
        # per-line basis, which is the safer fallback.
        if multiline:
            cmd.append("-Pz")

        if not is_file:
            cmd.append("-r")
            for skip_dir in self.SKIP_DIRS:
                cmd.extend(["--exclude-dir", skip_dir])

        cmd.extend(["-e", pattern])
        cmd.append(path)

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            try:
                stdout, _ = await proc.communicate()
            except asyncio.CancelledError:
                proc.kill()
                try: await proc.wait()
                except Exception: pass
                raise

            if proc.returncode not in (0, 1):
                return ToolResult(text="Error: search_failed\ngrep search failed", error=True)

            text, metadata = self._format_output(
                stdout.decode("utf-8", errors="replace"),
                output_mode,
                path,
                head_limit,
                is_file=is_file,
                numbered=line_number,
            )
            return ToolResult(text=text, metadata=metadata)

        except asyncio.TimeoutError:
            return ToolResult(text="Error: timeout\nSearch timed out", error=True)
        except Exception as e:
            return ToolResult(text=f"Error: search_failed\n{e}", error=True)

    # ---- Python fallback ----
    async def _python_search(
        self, pattern, path, is_file, head_limit, include,
        case_insensitive, multiline, output_mode, line_number=True,
    ):
        try:
            flags = re.IGNORECASE if case_insensitive else 0
            if multiline:
                flags |= re.DOTALL | re.MULTILINE
            regex = re.compile(pattern, flags)
        except re.error as e:
            return ToolResult(text=f"Error: invalid_regex\n{e}", error=True)

        # The Python fallback only needs the file list; each file is
        # then read in full and regex.searched across it. For
        # files_with_matches / count we short-circuit as soon as we know
        # a file has at least one hit.
        files: list[tuple[str, int]] = []  # (path, line_count) for total
        file_hits: dict[str, int] = {}  # path -> match count (count mode)
        match_lines: list[tuple[str, int, str]] = []  # (rel, line, content)
        truncated = False

        if is_file:
            candidates = [(path, os.path.basename(path))]
        else:
            candidates = []
            for root, dirs, fs in os.walk(path):
                dirs[:] = [d for d in dirs if d not in self.SKIP_DIRS]
                for fn in fs:
                    if include and not self._fnmatch(fn, include):
                        continue
                    fp = os.path.join(root, fn)
                    if not os.path.isfile(fp):
                        continue
                    candidates.append((fp, os.path.relpath(fp, path)))

        def finish():
            if output_mode == "count":
                total = sum(file_hits.values())
                keys = sorted(file_hits.keys())
                lines = [f"Found {total} matches in {len(keys)} files:", ""]
                for fp in keys:
                    lines.append(f"{fp}:{file_hits[fp]}")
                if truncated:
                    lines.append(f"\n(Showing {len(keys)} of more files)")
                return ToolResult(
                    text="\n".join(lines),
                    metadata={
                        "mode": "count",
                        "total": total,
                        "files": dict(file_hits),
                        "truncated": truncated,
                    },
                )
            if output_mode == "files_with_matches":
                keys = sorted(file_hits.keys())
                lines = [f"Found {len(keys)} files with matches:", ""]
                lines.extend(keys)
                return ToolResult(
                    text="\n".join(lines),
                    metadata={
                        "mode": "files_with_matches",
                        "total": len(keys),
                        "files": keys,
                        "truncated": truncated,
                    },
                )
            # content
            total = len(match_lines)
            lines = [f"Found {total} matches in {len(file_hits)} files:", ""]
            for fp, ln, content in match_lines:
                if line_number:
                    lines.append(f"{fp}:{ln}: {content}")
                else:
                    lines.append(f"{fp}: {content}")
            if truncated:
                lines.append(f"\n(Showing {len(match_lines)} of more results)")
            return ToolResult(
                text="\n".join(lines),
                metadata={
                    "mode": "content",
                    "matches": [
                        {"file": fp, "line": ln, "content": content}
                        for fp, ln, content in match_lines
                    ],
                    "total": total,
                    "truncated": truncated,
                },
            )

        for fp, rel in candidates:
            if self._is_binary(fp):
                continue
            try:
                with open(fp, "r", encoding="utf-8", errors="ignore") as f:
                    body = f.read()
            except (IOError, OSError):
                continue

            if multiline:
                hits = list(regex.finditer(body))
                if not hits:
                    continue
                file_hits[rel] = file_hits.get(rel, 0) + len(hits)
                if output_mode == "content":
                    for m in hits:
                        # Convert byte offset → line number. We do a
                        # prefix count up to the match start. The
                        # "content" we show is the line containing the
                        # match start (multi-line matches are shown
                        # anchored at their first line, with `...` if
                        # they span more).
                        start = m.start()
                        line_no = body.count("\n", 0, start) + 1
                        # The line itself: text from start-of-line to
                        # end-of-line at the match start.
                        line_start = body.rfind("\n", 0, start) + 1
                        line_end = body.find("\n", start)
                        if line_end == -1:
                            line_end = len(body)
                        content = body[line_start:line_end].rstrip("\r")
                        if m.end() > line_end + 1 or "\n" in m.group(0):
                            content = content + " …"
                        match_lines.append((rel, line_no, content))
                        if len(match_lines) >= head_limit:
                            truncated = True
                            return finish()
                # For files_with_matches / count we already updated
                # file_hits above. Bail out for files_with_matches
                # (we have a hit, no need to keep counting), but
                # continue for count so the count is accurate.
                if output_mode == "files_with_matches":
                    # Don't re-enter this branch for this file
                    continue
            else:
                # Per-line mode: iterate, tracking line numbers
                count = 0
                for line_no, line in enumerate(body.splitlines(), 1):
                    if regex.search(line):
                        count += 1
                        if output_mode == "content":
                            match_lines.append((rel, line_no, line.rstrip("\r")))
                            if len(match_lines) >= head_limit:
                                truncated = True
                                file_hits[rel] = file_hits.get(rel, 0) + count
                                return finish()
                if count:
                    file_hits[rel] = file_hits.get(rel, 0) + count
                if output_mode == "files_with_matches" and count:
                    # No need to read further lines from this file
                    continue

        return finish()

    # ---- helpers ----
    def _format_output(self, output, output_mode, base_path, head_limit, is_file: bool = False, numbered: bool = True):
        """Format ripgrep/grep output according to output_mode.

        Returns (text, metadata) so callers can put both into the
        ToolResult. Metadata is consistent across output modes (the
        `mode` field tells you how to interpret the rest). All file
        paths are normalized to be relative to `base_path` so the
        caller sees a stable shape regardless of whether they passed
        an absolute or relative `path` in. When `is_file=True`, the
        target file is reported by its basename instead of "." (which
        is what `os.path.relpath(file, file)` returns). `numbered`
        tells the content parser whether the tool emitted line numbers
        (rg `--line-number` / grep `-n`) — unnumbered output is
        "file:content" (dir search) or bare "content" (single file).
        """
        def _rel(p: str) -> str:
            if not p:
                return p
            if is_file:
                # In single-file mode the only file we'll ever see is
                # the target itself; report it by basename for clarity.
                return os.path.basename(base_path) or base_path
            try:
                return os.path.relpath(p, base_path)
            except (ValueError, OSError):
                return p

        if output_mode == "count":
            # rg --count-matches and grep -c both print "file:count"
            file_hits: dict[str, int] = {}
            for line in output.split("\n"):
                if not line:
                    continue
                # Both rg and grep print "<file>:<count>" (grep omits
                # the file in single-file mode — handle that by
                # attributing to base_path).
                if ":" in line:
                    fp, _, cnt = line.rpartition(":")
                    try:
                        file_hits[_rel(fp)] = int(cnt)
                    except ValueError:
                        continue
                else:
                    try:
                        file_hits[_rel(base_path)] = int(line)
                    except ValueError:
                        continue
            total = sum(file_hits.values())
            keys = sorted(file_hits.keys())
            lines = [f"Found {total} matches in {len(keys)} files:", ""]
            for fp in keys:
                lines.append(f"{fp}:{file_hits[fp]}")
            text = "\n".join(lines)
            return text, {
                "mode": "count",
                "total": total,
                "files": dict(file_hits),
                "truncated": False,
            }

        if output_mode == "files_with_matches":
            keys = []
            for line in output.split("\n"):
                if line:
                    keys.append(_rel(line))
            keys.sort()
            lines = [f"Found {len(keys)} files with matches:", ""]
            lines.extend(keys)
            text = "\n".join(lines)
            return text, {
                "mode": "files_with_matches",
                "total": len(keys),
                "files": keys,
                "truncated": False,
            }

        # content: parse "file:line:content" (dir search, numbered) or
        # "line:content" (single file, numbered). When the tool ran
        # without line numbers (numbered=False), dir-search lines are
        # "file:content" and single-file lines are bare content.
        matches = []
        for line in output.split("\n"):
            if not line:
                continue
            if not numbered:
                if is_file:
                    matches.append((_rel(base_path), 0, line))
                else:
                    fp, sep, content = line.partition(":")
                    if sep:
                        matches.append((_rel(fp), 0, content))
                    else:
                        # A file with no ":" in its path prints as bare
                        # content — can't distinguish; attribute it.
                        matches.append((line, 0, line))
            else:
                parts = line.split(":", 2)
                if len(parts) >= 3:
                    # dir search: file:line:content
                    try:
                        matches.append((_rel(parts[0]), int(parts[1]), parts[2]))
                    except ValueError:
                        continue
                elif len(parts) == 2:
                    # single-file search: line:content
                    try:
                        matches.append((_rel(base_path), int(parts[0]), parts[1]))
                    except ValueError:
                        continue
                # else: malformed line, skip
            if len(matches) >= head_limit:
                break

        truncated = len(matches) >= head_limit
        file_set = sorted({m[0] for m in matches})
        lines = [f"Found {len(matches)} matches in {len(file_set)} files:", ""]
        for fp, ln, content in matches:
            if ln:
                lines.append(f"{fp}:{ln}: {content}")
            else:
                lines.append(f"{fp}: {content}")
        if truncated:
            lines.append(f"\n(Showing {len(matches)} of more results)")
        text = "\n".join(lines)
        return text, {
            "mode": "content",
            "matches": [
                {"file": fp, "line": ln, "content": content}
                for fp, ln, content in matches
            ],
            "total": len(matches),
            "truncated": truncated,
        }

    def _fnmatch(self, filename: str, pattern: str) -> bool:
        # Lazy import — fnmatch is stdlib but we don't need it for the
        # fast paths.
        from fnmatch import fnmatch
        # Support `!**/foo` style exclusion by negating on a leading "!"
        if pattern.startswith("!"):
            return not fnmatch(filename, pattern[1:])
        return fnmatch(filename, pattern)

    def _is_binary(self, filepath: str) -> bool:
        try:
            with open(filepath, "rb") as f:
                chunk = f.read(8192)
                return b"\x00" in chunk
        except (IOError, OSError):
            return False
