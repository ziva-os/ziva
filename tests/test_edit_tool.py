import asyncio

from plugins.tools.edit.impl import EditTool
from ziva.shared_types import RuntimeContext


def test_edit_single_replacement(tmp_path):
    test_file = tmp_path / "test.txt"
    test_file.write_text("Hello World\nHello World\nHello World")
    tool = EditTool()
    ctx = RuntimeContext(session_id="test", config={}, metadata={})

    result = asyncio.run(tool.run({
        "file_path": str(test_file),
        "old_string": "Hello World",
        "new_string": "Goodbye World",
        "replace_all": False
    }, ctx))

    assert result["status"] == "ok"
    assert result["existed"] is True
    assert test_file.read_text() == "Goodbye World\nHello World\nHello World"
    assert "diff" in result


def test_edit_replace_all(tmp_path):
    test_file = tmp_path / "test.txt"
    test_file.write_text("Hello World\nHello World\nHello World")
    tool = EditTool()
    ctx = RuntimeContext(session_id="test", config={}, metadata={})

    result = asyncio.run(tool.run({
        "file_path": str(test_file),
        "old_string": "Hello World",
        "new_string": "Goodbye World",
        "replace_all": True
    }, ctx))

    assert result["status"] == "ok"
    assert test_file.read_text() == "Goodbye World\nGoodbye World\nGoodbye World"


def test_edit_create_new_file(tmp_path):
    new_file = tmp_path / "new.txt"
    tool = EditTool()
    ctx = RuntimeContext(session_id="test", config={}, metadata={})

    result = asyncio.run(tool.run({
        "file_path": str(new_file),
        "old_string": "",  # Empty to create new file
        "new_string": "New content",
        "replace_all": False
    }, ctx))

    assert result["status"] == "ok"
    assert result["existed"] is False
    assert new_file.read_text() == "New content"


def test_edit_old_string_not_found(tmp_path):
    test_file = tmp_path / "test.txt"
    test_file.write_text("Hello World")
    tool = EditTool()
    ctx = RuntimeContext(session_id="test", config={}, metadata={})

    result = asyncio.run(tool.run({
        "file_path": str(test_file),
        "old_string": "Goodbye",
        "new_string": "Hello",
        "replace_all": False
    }, ctx))

    assert result["error"] == "replacement_failed"
    assert "not found" in result["message"]


def test_edit_missing_file_with_non_empty_old_string(tmp_path):
    tool = EditTool()
    ctx = RuntimeContext(session_id="test", config={}, metadata={})

    result = asyncio.run(tool.run({
        "file_path": str(tmp_path / "nonexistent.txt"),
        "old_string": "something",
        "new_string": "else",
        "replace_all": False
    }, ctx))

    assert result["error"] == "file_not_found"


def test_edit_identical_strings(tmp_path):
    test_file = tmp_path / "test.txt"
    test_file.write_text("Hello")
    tool = EditTool()
    ctx = RuntimeContext(session_id="test", config={}, metadata={})

    result = asyncio.run(tool.run({
        "file_path": str(test_file),
        "old_string": "Hello",
        "new_string": "Hello",  # Same as old
        "replace_all": False
    }, ctx))

    assert result["error"] == "invalid_input"
    assert "identical" in result["message"]


def test_edit_missing_file_path():
    tool = EditTool()
    ctx = RuntimeContext(session_id="test", config={}, metadata={})

    result = asyncio.run(tool.run({
        "old_string": "a",
        "new_string": "b",
        "replace_all": False
    }, ctx))

    assert result["error"] == "invalid_input"


def test_edit_spec():
    tool = EditTool()
    spec = tool.spec()

    assert spec["name"] == "edit"
    assert "description" in spec
    assert set(spec["input_schema"]["required"]) == {"file_path", "old_string", "new_string"}
    assert spec["input_schema"]["properties"]["replace_all"]["type"] == "boolean"
