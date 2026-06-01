import asyncio

from plugins.tools.grep.impl import GrepTool
from ziva_runtime.shared_types import RuntimeContext


def test_grep_match_single_file(tmp_path):
    test_file = tmp_path / "test.txt"
    test_file.write_text("Hello World\nThis is a test\nAnother line\nHello again\n")
    tool = GrepTool()
    ctx = RuntimeContext(session_id="test", config={}, metadata={})

    result = asyncio.run(tool.run({"pattern": "Hello", "path": str(tmp_path)}, ctx))

    assert result["total"] == 2
    assert result["matches"][0]["line"] == 1
    assert result["matches"][1]["line"] == 4


def test_grep_match_multiple_files(tmp_path):
    (tmp_path / "file1.txt").write_text("pattern match here\n")
    (tmp_path / "file2.txt").write_text("no match\npattern again\n")
    (tmp_path / "file3.txt").write_text("nothing here\n")
    tool = GrepTool()
    ctx = RuntimeContext(session_id="test", config={}, metadata={})

    result = asyncio.run(tool.run({"pattern": "pattern", "path": str(tmp_path)}, ctx))

    assert result["total"] == 2
    file_names = {m["file"] for m in result["matches"]}
    assert "file1.txt" in file_names
    assert "file2.txt" in file_names


def test_grep_no_matches(tmp_path):
    (tmp_path / "test.txt").write_text("Hello World\nNo matches here\n")
    tool = GrepTool()
    ctx = RuntimeContext(session_id="test", config={}, metadata={})

    result = asyncio.run(tool.run({"pattern": "nonexistent", "path": str(tmp_path)}, ctx))

    assert result["total"] == 0
    assert len(result["matches"]) == 0
    assert result["truncated"] is False


def test_grep_invalid_regex(tmp_path):
    tool = GrepTool()
    ctx = RuntimeContext(session_id="test", config={}, metadata={})

    result = asyncio.run(tool.run({"pattern": "[invalid(", "path": str(tmp_path)}, ctx))

    assert result["error"] == "invalid_regex"


def test_grep_binary_file_skipping(tmp_path):
    (tmp_path / "text.txt").write_text("pattern match\n")
    (tmp_path / "binary.dat").write_bytes(b"\x00\x01\x02\x03pattern\x04")
    tool = GrepTool()
    ctx = RuntimeContext(session_id="test", config={}, metadata={})

    result = asyncio.run(tool.run({"pattern": "pattern", "path": str(tmp_path)}, ctx))

    assert result["total"] == 1
    assert result["matches"][0]["file"] == "text.txt"


def test_grep_skip_git_directory(tmp_path):
    git_dir = tmp_path / ".git"
    git_dir.mkdir()
    (git_dir / "config").write_text("pattern in git\n")
    (tmp_path / "regular.txt").write_text("pattern regular\n")
    tool = GrepTool()
    ctx = RuntimeContext(session_id="test", config={}, metadata={})

    result = asyncio.run(tool.run({"pattern": "pattern", "path": str(tmp_path)}, ctx))

    assert result["total"] == 1
    assert result["matches"][0]["file"] == "regular.txt"


def test_grep_skip_node_modules(tmp_path):
    nm_dir = tmp_path / "node_modules"
    nm_dir.mkdir()
    (nm_dir / "module.js").write_text("pattern in node_modules\n")
    (tmp_path / "app.js").write_text("pattern regular\n")
    tool = GrepTool()
    ctx = RuntimeContext(session_id="test", config={}, metadata={})

    result = asyncio.run(tool.run({"pattern": "pattern", "path": str(tmp_path)}, ctx))

    assert result["total"] == 1
    assert result["matches"][0]["file"] == "app.js"


def test_grep_max_results_truncation(tmp_path):
    for i in range(10):
        (tmp_path / f"file{i}.txt").write_text(f"pattern {i}\n")
    tool = GrepTool()
    ctx = RuntimeContext(session_id="test", config={}, metadata={})

    result = asyncio.run(tool.run({"pattern": "pattern", "path": str(tmp_path), "max_results": 3}, ctx))

    assert result["total"] == 3
    assert result["truncated"] is True


def test_grep_regex_patterns(tmp_path):
    (tmp_path / "test.txt").write_text("test123\nabc456def\nno digits\n")
    tool = GrepTool()
    ctx = RuntimeContext(session_id="test", config={}, metadata={})

    result = asyncio.run(tool.run({"pattern": r"\d+", "path": str(tmp_path)}, ctx))

    assert result["total"] == 2


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

    assert result["error"] == "path_not_found"
