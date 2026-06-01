import asyncio

from plugins.tools.write_file.impl import WriteFileTool
from ziva_runtime.shared_types import RuntimeContext


def test_write_new_file(tmp_path):
    test_file = tmp_path / "new_file.txt"
    test_content = "Hello, World!\nThis is new content."
    tool = WriteFileTool()
    ctx = RuntimeContext(session_id="test", config={}, metadata={})

    result = asyncio.run(tool.run({"file_path": str(test_file), "content": test_content}, ctx))

    assert result["status"] == "ok"
    assert result["file_path"] == str(test_file)
    assert result["bytes_written"] == len(test_content)
    assert test_file.read_text() == test_content
    # New file should not have diff
    assert "diff" not in result


def test_overwrite_existing_file(tmp_path):
    test_file = tmp_path / "existing.txt"
    test_file.write_text("Original content")
    tool = WriteFileTool()
    ctx = RuntimeContext(session_id="test", config={}, metadata={})

    new_content = "New content"
    result = asyncio.run(tool.run({"file_path": str(test_file), "content": new_content}, ctx))

    assert result["status"] == "ok"
    assert result["bytes_written"] == len(new_content)
    assert test_file.read_text() == new_content
    # Existing file should have diff
    assert "diff" in result
    assert result["file_existed"] is True
    assert "-Original content" in result["diff"] or "- Original content" in result["diff"]


def test_write_file_nested_directory(tmp_path):
    nested_file = tmp_path / "level1" / "level2" / "nested.txt"
    tool = WriteFileTool()
    ctx = RuntimeContext(session_id="test", config={}, metadata={})

    result = asyncio.run(tool.run({"file_path": str(nested_file), "content": "nested"}, ctx))

    assert result["status"] == "ok"
    assert nested_file.exists()
    assert nested_file.read_text() == "nested"


def test_write_file_empty_content(tmp_path):
    test_file = tmp_path / "empty.txt"
    tool = WriteFileTool()
    ctx = RuntimeContext(session_id="test", config={}, metadata={})

    result = asyncio.run(tool.run({"file_path": str(test_file), "content": ""}, ctx))

    assert result["status"] == "ok"
    assert result["bytes_written"] == 0
    assert test_file.read_text() == ""


def test_write_file_spec():
    tool = WriteFileTool()
    spec = tool.spec()

    assert spec["name"] == "write_file"
    assert "description" in spec
    assert set(spec["input_schema"]["required"]) == {"file_path", "content"}


def test_write_file_unicode(tmp_path):
    test_file = tmp_path / "unicode.txt"
    unicode_content = "Hello 世界 🌍 Привет"
    tool = WriteFileTool()
    ctx = RuntimeContext(session_id="test", config={}, metadata={})

    result = asyncio.run(tool.run({"file_path": str(test_file), "content": unicode_content}, ctx))

    assert result["status"] == "ok"
    assert test_file.read_text() == unicode_content
