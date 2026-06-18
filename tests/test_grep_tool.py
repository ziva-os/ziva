import asyncio

from plugins.tools.grep.impl import GrepTool
from ziva_runtime.shared_types import RuntimeContext


def test_grep_match_single_file(tmp_path):
    test_file = tmp_path / "test.txt"
    test_file.write_text("Hello World\nThis is a test\nAnother line\nHello again\n")
    tool = GrepTool()
    ctx = RuntimeContext(session_id="test", config={}, metadata={})

    result = asyncio.run(tool.run({"pattern": "Hello", "path": str(tmp_path)}, ctx))

    assert result.metadata["total"] == 2
    assert result.metadata["matches"][0]["line"] == 1
    assert result.metadata["matches"][1]["line"] == 4


def test_grep_match_multiple_files(tmp_path):
    (tmp_path / "file1.txt").write_text("pattern match here\n")
    (tmp_path / "file2.txt").write_text("no match\npattern again\n")
    (tmp_path / "file3.txt").write_text("nothing here\n")
    tool = GrepTool()
    ctx = RuntimeContext(session_id="test", config={}, metadata={})

    result = asyncio.run(tool.run({"pattern": "pattern", "path": str(tmp_path)}, ctx))

    assert result.metadata["total"] == 2
    file_names = {m["file"] for m in result.metadata["matches"]}
    assert "file1.txt" in file_names
    assert "file2.txt" in file_names


def test_grep_no_matches(tmp_path):
    (tmp_path / "test.txt").write_text("Hello World\nNo matches here\n")
    tool = GrepTool()
    ctx = RuntimeContext(session_id="test", config={}, metadata={})

    result = asyncio.run(tool.run({"pattern": "nonexistent", "path": str(tmp_path)}, ctx))

    assert result.metadata["total"] == 0
    assert len(result.metadata["matches"]) == 0
    assert result.metadata["truncated"] is False


def test_grep_invalid_regex(tmp_path):
    tool = GrepTool()
    ctx = RuntimeContext(session_id="test", config={}, metadata={})

    result = asyncio.run(tool.run({"pattern": "[invalid(", "path": str(tmp_path)}, ctx))

    assert result.text.startswith("Error: invalid_regex")


def test_grep_binary_file_skipping(tmp_path):
    (tmp_path / "text.txt").write_text("pattern match\n")
    (tmp_path / "binary.dat").write_bytes(b"\x00\x01\x02\x03pattern\x04")
    tool = GrepTool()
    ctx = RuntimeContext(session_id="test", config={}, metadata={})

    result = asyncio.run(tool.run({"pattern": "pattern", "path": str(tmp_path)}, ctx))

    assert result.metadata["total"] == 1
    assert result.metadata["matches"][0]["file"] == "text.txt"


def test_grep_skip_git_directory(tmp_path):
    git_dir = tmp_path / ".git"
    git_dir.mkdir()
    (git_dir / "config").write_text("pattern in git\n")
    (tmp_path / "regular.txt").write_text("pattern regular\n")
    tool = GrepTool()
    ctx = RuntimeContext(session_id="test", config={}, metadata={})

    result = asyncio.run(tool.run({"pattern": "pattern", "path": str(tmp_path)}, ctx))

    assert result.metadata["total"] == 1
    assert result.metadata["matches"][0]["file"] == "regular.txt"


def test_grep_skip_node_modules(tmp_path):
    nm_dir = tmp_path / "node_modules"
    nm_dir.mkdir()
    (nm_dir / "module.js").write_text("pattern in node_modules\n")
    (tmp_path / "app.js").write_text("pattern regular\n")
    tool = GrepTool()
    ctx = RuntimeContext(session_id="test", config={}, metadata={})

    result = asyncio.run(tool.run({"pattern": "pattern", "path": str(tmp_path)}, ctx))

    assert result.metadata["total"] == 1
    assert result.metadata["matches"][0]["file"] == "app.js"


def test_grep_head_limit_truncation(tmp_path):
    for i in range(10):
        (tmp_path / f"file{i}.txt").write_text(f"pattern {i}\n")
    tool = GrepTool()
    ctx = RuntimeContext(session_id="test", config={}, metadata={})

    result = asyncio.run(tool.run({"pattern": "pattern", "path": str(tmp_path), "head_limit": 3}, ctx))

    assert result.metadata["total"] == 3
    assert result.metadata["truncated"] is True


def test_grep_regex_patterns(tmp_path):
    (tmp_path / "test.txt").write_text("test123\nabc456def\nno digits\n")
    tool = GrepTool()
    ctx = RuntimeContext(session_id="test", config={}, metadata={})

    result = asyncio.run(tool.run({"pattern": r"\d+", "path": str(tmp_path)}, ctx))

    assert result.metadata["total"] == 2


def test_grep_spec():
    tool = GrepTool()
    spec = tool.spec()

    assert spec["name"] == "grep"
    assert "pattern" in spec["input_schema"]["properties"]
    assert "pattern" in spec["input_schema"]["required"]


def test_grep_nonexistent_path():
    tool = GrepTool()
    ctx = RuntimeContext(session_id="test", config={}, metadata={})

    result = asyncio.run(tool.run({"pattern": "pattern", "path": "/nonexistent/path"}, ctx))

    assert result.text.startswith("Error: path_not_found")


# ---- File-path mode (Claude Code / Codex parity) ----

def test_grep_search_single_file_path(tmp_path):
    """`path` may be a file, not just a directory — mirrors the
    Claude Code / Codex grep tool, which also accepts a single file."""
    (tmp_path / "a.txt").write_text("alpha\nbeta\ngamma\n")
    (tmp_path / "b.txt").write_text("delta\nbeta\n")
    target = tmp_path / "a.txt"
    tool = GrepTool()
    ctx = RuntimeContext(session_id="test", config={}, metadata={})

    result = asyncio.run(tool.run({"pattern": "beta", "path": str(target)}, ctx))

    assert result.error is False
    assert result.metadata["total"] == 1
    # The match file should be the absolute path (or the basename) —
    # both rg and grep return the file we asked about, never b.txt.
    match_file = result.metadata["matches"][0]["file"]
    assert "b.txt" not in match_file
    assert "a.txt" in match_file


def test_grep_search_file_path_ignores_include(tmp_path):
    """When `path` is a file, `include` is silently dropped (a glob
    only makes sense for a directory of files)."""
    (tmp_path / "actual.py").write_text("pattern hit\n")
    (tmp_path / "other.txt").write_text("pattern hit\n")
    target = tmp_path / "actual.py"
    tool = GrepTool()
    ctx = RuntimeContext(session_id="test", config={}, metadata={})

    result = asyncio.run(tool.run(
        {"pattern": "pattern", "path": str(target), "include": "*.py"},
        ctx,
    ))

    # Should hit actual.py; include didn't filter it out.
    assert result.metadata["total"] == 1


# ---- output_mode ----

def test_grep_output_mode_files_with_matches(tmp_path):
    (tmp_path / "a.py").write_text("hit here\n")
    (tmp_path / "b.py").write_text("no\n")
    (tmp_path / "c.py").write_text("another hit\n")
    tool = GrepTool()
    ctx = RuntimeContext(session_id="test", config={}, metadata={})

    result = asyncio.run(tool.run(
        {"pattern": "hit", "path": str(tmp_path), "output_mode": "files_with_matches"},
        ctx,
    ))

    assert result.metadata["mode"] == "files_with_matches"
    assert result.metadata["total"] == 2
    assert set(result.metadata["files"]) == {"a.py", "c.py"}
    # The text shouldn't contain per-line content
    assert "hit here" not in result.text.split("\n", 2)[2]


def test_grep_output_mode_count(tmp_path):
    (tmp_path / "a.py").write_text("x = 1\nx = 2\n")
    (tmp_path / "b.py").write_text("y = 1\n")
    (tmp_path / "c.py").write_text("x = 9\nx = 8\nx = 7\n")
    tool = GrepTool()
    ctx = RuntimeContext(session_id="test", config={}, metadata={})

    result = asyncio.run(tool.run(
        {"pattern": r"\bx\b", "path": str(tmp_path), "output_mode": "count"},
        ctx,
    ))

    assert result.metadata["mode"] == "count"
    assert result.metadata["total"] == 5  # 2 in a.py, 0 in b.py, 3 in c.py
    assert result.metadata["files"] == {"a.py": 2, "c.py": 3}


# ---- head_limit ----

def test_grep_head_limit_truncation_alt(tmp_path):
    for i in range(10):
        (tmp_path / f"file{i}.txt").write_text(f"pattern {i}\n")
    tool = GrepTool()
    ctx = RuntimeContext(session_id="test", config={}, metadata={})

    result = asyncio.run(tool.run(
        {"pattern": "pattern", "path": str(tmp_path), "head_limit": 4},
        ctx,
    ))

    assert result.metadata["total"] == 4
    assert result.metadata["truncated"] is True


# ---- include (Claude Code's name) ----

def test_grep_include_filter(tmp_path):
    (tmp_path / "a.py").write_text("pattern hit\n")
    (tmp_path / "b.txt").write_text("pattern hit\n")
    (tmp_path / "c.py").write_text("pattern hit\n")
    tool = GrepTool()
    ctx = RuntimeContext(session_id="test", config={}, metadata={})

    result = asyncio.run(tool.run(
        {"pattern": "pattern", "path": str(tmp_path), "include": "*.py"},
        ctx,
    ))

    files = {m["file"] for m in result.metadata["matches"]}
    assert "a.py" in files
    assert "c.py" in files
    assert "b.txt" not in files


# ---- multiline ----

def test_grep_multiline_pattern(tmp_path):
    (tmp_path / "code.py").write_text(
        "class Foo:\n"
        "    def bar(self):\n"
        "        return 42\n"
        "\n"
        "class Baz:\n"
        "    def qux(self):\n"
        "        pass\n"
    )
    tool = GrepTool()
    ctx = RuntimeContext(session_id="test", config={}, metadata={})

    # Pattern matches a class definition + its first method together.
    # Without multiline this would be impossible because `.` doesn't
    # cross line boundaries.
    result = asyncio.run(tool.run(
        {
            "pattern": r"class \w+:\n\s+def \w+\(",
            "path": str(tmp_path),
            "multiline": True,
        },
        ctx,
    ))

    assert result.error is False
    assert result.metadata["total"] >= 2


def test_grep_multiline_does_not_break_simple_patterns(tmp_path):
    """`multiline: true` should still match single-line patterns
    correctly — it's a superset of the non-multiline behavior."""
    (tmp_path / "test.py").write_text("foo\nbar\n")
    tool = GrepTool()
    ctx = RuntimeContext(session_id="test", config={}, metadata={})

    result = asyncio.run(tool.run(
        {"pattern": "foo", "path": str(tmp_path), "multiline": True},
        ctx,
    ))

    assert result.metadata["total"] == 1

