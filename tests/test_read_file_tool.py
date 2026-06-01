import asyncio

from plugins.tools.read_file.impl import ReadFileTool
from ziva_runtime.shared_types import RuntimeContext


def test_read_existing_file(tmp_path):
    test_file = tmp_path / "test.txt"
    test_content = "Hello, World!\nThis is a test file."
    test_file.write_text(test_content)
    tool = ReadFileTool()
    ctx = RuntimeContext(session_id="test", config={}, metadata={})

    result = asyncio.run(tool.run({"file_path": str(test_file)}, ctx))
    # New format includes line numbers and XML tags
    assert "1: Hello, World!" in result["content"]
    assert "2: This is a test file." in result["content"]
    assert result["metadata"]["type"] == "file"
    assert result["metadata"]["total_lines"] == 2


def test_read_file_not_found(tmp_path):
    nonexistent_file = tmp_path / "does_not_exist.txt"
    tool = ReadFileTool()
    ctx = RuntimeContext(session_id="test", config={}, metadata={})

    result = asyncio.run(tool.run({"file_path": str(nonexistent_file)}, ctx))
    assert result["error"] == "file_not_found"
    assert "File not found" in result["message"]


def test_read_directory(tmp_path):
    tool = ReadFileTool()
    ctx = RuntimeContext(session_id="test", config={}, metadata={})

    result = asyncio.run(tool.run({"file_path": str(tmp_path)}, ctx))
    # New behavior: directories return content with listing
    assert "content" in result
    assert result["metadata"]["type"] == "directory"
    assert "<entries>" in result["content"]


def test_read_file_offset_and_limit(tmp_path):
    test_file = tmp_path / "test_lines.txt"
    lines = [f"Line {i}" for i in range(1, 101)]
    test_file.write_text("\n".join(lines))
    tool = ReadFileTool()
    ctx = RuntimeContext(session_id="test", config={}, metadata={})

    # Test with offset=10, limit=5
    result = asyncio.run(tool.run({"file_path": str(test_file), "offset": 10, "limit": 5}, ctx))
    assert "10: Line 10" in result["content"]
    assert "14: Line 14" in result["content"]
    assert "15: Line 15" not in result["content"]  # Should be truncated
    assert result["metadata"]["truncated"] is True


def test_read_file_line_truncation(tmp_path):
    test_file = tmp_path / "long_line.txt"
    # Create a line longer than MAX_LINE_LENGTH (2000)
    long_line = "x" * 2500
    test_file.write_text(long_line)
    tool = ReadFileTool()
    ctx = RuntimeContext(session_id="test", config={}, metadata={})

    result = asyncio.run(tool.run({"file_path": str(test_file)}, ctx))
    assert "line truncated" in result["content"]


def test_read_binary_file(tmp_path):
    binary_file = tmp_path / "test.zip"
    binary_file.write_bytes(b"\x00\x01\x02\x03\x04\x05")
    tool = ReadFileTool()
    ctx = RuntimeContext(session_id="test", config={}, metadata={})

    result = asyncio.run(tool.run({"file_path": str(binary_file)}, ctx))
    assert result["error"] == "binary_file"


def test_read_file_missing_path():
    tool = ReadFileTool()
    ctx = RuntimeContext(session_id="test", config={}, metadata={})

    result = asyncio.run(tool.run({}, ctx))
    assert result["error"] == "invalid_input"


def test_read_file_invalid_offset():
    tool = ReadFileTool()
    ctx = RuntimeContext(session_id="test", config={}, metadata={})

    result = asyncio.run(tool.run({"file_path": "dummy.txt", "offset": 0}, ctx))
    assert result["error"] == "invalid_input"


def test_read_file_spec():
    tool = ReadFileTool()
    spec = tool.spec()

    assert spec["name"] == "read_file"
    assert "description" in spec
    assert spec["input_schema"]["type"] == "object"
    assert "file_path" in spec["input_schema"]["properties"]
    assert "file_path" in spec["input_schema"]["required"]
    # New parameters
    assert "offset" in spec["input_schema"]["properties"]
    assert "limit" in spec["input_schema"]["properties"]
