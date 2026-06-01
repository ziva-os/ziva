import asyncio
import sys
from pathlib import Path

# Add src to path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from plugins.tools.shell.impl import ShellTool
from ziva_runtime.shared_types import RuntimeContext


def test_shell_echo():
    tool = ShellTool()
    ctx = RuntimeContext(session_id="test", config={}, metadata={})
    result = asyncio.run(tool.run({"command": "echo hello"}, ctx))
    assert result["exit_code"] == 0
    assert "hello" in result["stdout"]


def test_shell_failure():
    tool = ShellTool()
    ctx = RuntimeContext(session_id="test", config={}, metadata={})
    result = asyncio.run(tool.run({"command": "exit 1"}, ctx))
    assert result["exit_code"] == 1


def test_shell_workdir(tmp_path):
    tool = ShellTool()
    ctx = RuntimeContext(session_id="test", config={}, metadata={})
    # Create a test file in temp directory
    test_file = tmp_path / "test.txt"
    test_file.write_text("content")

    result = asyncio.run(tool.run({"command": "cat test.txt", "workdir": str(tmp_path)}, ctx))
    assert result["exit_code"] == 0
    assert "content" in result["stdout"]


def test_shell_ansi_stripping():
    tool = ShellTool()
    ctx = RuntimeContext(session_id="test", config={}, metadata={})
    # Command that outputs ANSI codes
    result = asyncio.run(tool.run({"command": "echo '\\e[31mred\\e[0m'"}, ctx))
    assert result["exit_code"] == 0
    # ANSI codes should be stripped
    assert "\x1b" not in result["stdout"]


def test_shell_timeout():
    tool = ShellTool()
    ctx = RuntimeContext(session_id="test", config={}, metadata={})
    result = asyncio.run(tool.run({"command": "sleep 10", "timeout": 1}, ctx))
    assert result["timed_out"] is True


def test_shell_missing_command():
    tool = ShellTool()
    ctx = RuntimeContext(session_id="test", config={}, metadata={})
    result = asyncio.run(tool.run({}, ctx))
    assert result["error"] == "invalid_input"


def test_shell_spec():
    tool = ShellTool()
    spec = tool.spec()
    assert spec["name"] == "shell"
    assert "command" in spec["input_schema"]["required"]
    # New parameter
    assert "workdir" in spec["input_schema"]["properties"]
