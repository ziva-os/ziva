import asyncio
from plugins.tools.apply_patch.impl import ApplyPatchTool
from ziva_runtime.shared_types import RuntimeContext


def test_add_file(tmp_path):
    tool = ApplyPatchTool()
    ctx = RuntimeContext(session_id="test", config={}, metadata={})
    patch = f"""*** Begin Patch
*** Add File: hello.py
print("hello world")
*** End Patch"""
    result = asyncio.run(tool.run({"patch": patch, "cwd": str(tmp_path)}, ctx))
    assert result["applied"] == 1
    content = (tmp_path / "hello.py").read_text()
    assert 'print("hello world")' in content


def test_update_file(tmp_path):
    test_file = tmp_path / "test.txt"
    test_file.write_text("line1\nline2\nline3\n")
    tool = ApplyPatchTool()
    ctx = RuntimeContext(session_id="test", config={}, metadata={})
    patch = f"""*** Begin Patch
*** Update File: test.txt
@@ -1,3 +1,3 @@
 line1
-line2
+LINE2
 line3
*** End Patch"""
    result = asyncio.run(tool.run({"patch": patch, "cwd": str(tmp_path)}, ctx))
    assert result["applied"] == 1
    content = test_file.read_text()
    assert "LINE2" in content
    assert "line2" not in content


def test_delete_file(tmp_path):
    test_file = tmp_path / "to_delete.txt"
    test_file.write_text("content")
    tool = ApplyPatchTool()
    ctx = RuntimeContext(session_id="test", config={}, metadata={})
    patch = f"""*** Begin Patch
*** Delete File: to_delete.txt
*** End Patch"""
    result = asyncio.run(tool.run({"patch": patch, "cwd": str(tmp_path)}, ctx))
    assert result["applied"] == 1
    assert not test_file.exists()


def test_move_file(tmp_path):
    old_file = tmp_path / "old.txt"
    old_file.write_text("content")
    tool = ApplyPatchTool()
    ctx = RuntimeContext(session_id="test", config={}, metadata={})
    patch = f"""*** Begin Patch
*** Move File: old.txt -> new.txt
*** End Patch"""
    result = asyncio.run(tool.run({"patch": patch, "cwd": str(tmp_path)}, ctx))
    assert result["applied"] == 1
    assert not old_file.exists()
    assert (tmp_path / "new.txt").read_text() == "content"


def test_multi_file_patch(tmp_path):
    tool = ApplyPatchTool()
    ctx = RuntimeContext(session_id="test", config={}, metadata={})
    patch = f"""*** Begin Patch
*** Add File: a.txt
content a
*** Add File: b.txt
content b
*** End Patch"""
    result = asyncio.run(tool.run({"patch": patch, "cwd": str(tmp_path)}, ctx))
    assert result["applied"] == 2
    assert (tmp_path / "a.txt").read_text() == "content a"
    assert (tmp_path / "b.txt").read_text() == "content b"


def test_invalid_patch():
    tool = ApplyPatchTool()
    ctx = RuntimeContext(session_id="test", config={}, metadata={})
    result = asyncio.run(tool.run({"patch": "not a valid patch"}, ctx))
    assert "error" in result
    assert result["error"] == "invalid_patch"


def test_spec():
    tool = ApplyPatchTool()
    spec = tool.spec()
    assert spec["name"] == "apply_patch"
    assert "patch" in spec["input_schema"]["required"]


def test_add_file_with_subdirectory(tmp_path):
    tool = ApplyPatchTool()
    ctx = RuntimeContext(session_id="test", config={}, metadata={})
    patch = f"""*** Begin Patch
*** Add File: subdir/nested/file.py
print("nested")
*** End Patch"""
    result = asyncio.run(tool.run({"patch": patch, "cwd": str(tmp_path)}, ctx))
    assert result["applied"] == 1
    assert (tmp_path / "subdir" / "nested" / "file.py").exists()
    assert (tmp_path / "subdir" / "nested" / "file.py").read_text() == 'print("nested")'


def test_update_file_multiple_hunks(tmp_path):
    test_file = tmp_path / "test.txt"
    test_file.write_text("line1\nline2\nline3\nline4\nline5\n")
    tool = ApplyPatchTool()
    ctx = RuntimeContext(session_id="test", config={}, metadata={})
    patch = f"""*** Begin Patch
*** Update File: test.txt
@@ -1,3 +1,3 @@
 line1
-line2
+LINE2
 line3
@@ -3,3 +3,3 @@
 line3
-line4
+LINE4
 line5
*** End Patch"""
    result = asyncio.run(tool.run({"patch": patch, "cwd": str(tmp_path)}, ctx))
    assert result["applied"] == 1
    content = test_file.read_text()
    assert "LINE2" in content
    assert "LINE4" in content
    assert "line2" not in content
    assert "line4" not in content
