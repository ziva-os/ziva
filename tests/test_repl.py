import asyncio
import io
import sys
from pathlib import Path
from ziva_runtime.runtime import Runtime
from ziva_runtime.shared_types import ChatMessage, ChatResult


class FakeAdapter:
    def __init__(self, responses=None):
        self.responses = responses or ["Hello!"]
        self._idx = 0

    async def chat(self, messages, model, system_prompt=None, tools=None):
        content = self.responses[self._idx % len(self.responses)]
        self._idx += 1
        return ChatResult(role="assistant", content=content, model=model, usage={}, finish_reason="stop")


def _run_repl_commands(commands, approval="full-auto"):
    """Run a sequence of REPL commands and capture output."""
    root = Path(__file__).resolve().parents[1]
    rt = Runtime.create(workspace_root=root, model_adapter=FakeAdapter())

    captured = io.StringIO()
    original = sys.stdout
    sys.stdout = captured

    inputs = iter(commands)
    import builtins
    original_input = builtins.input

    def fake_input(prompt=""):
        return next(inputs)

    builtins.input = fake_input
    try:
        asyncio.run(_repl_loop_direct(rt, approval))
    except (StopIteration, EOFError):
        pass
    finally:
        builtins.input = original_input
        sys.stdout = original

    return captured.getvalue()


def _repl_loop_direct(runtime, approval_policy):
    from ziva_runtime.app.cli import _repl_loop
    return _repl_loop(runtime, approval_policy)


def test_slash_model_show():
    output = _run_repl_commands(["/model", "/quit"])
    assert "gpt-4.1" in output or "model" in output.lower()


def test_slash_model_switch():
    output = _run_repl_commands(["/model gpt-4o", "/model", "/quit"])
    assert "gpt-4o" in output


def test_slash_status():
    output = _run_repl_commands(["/status", "/quit"])
    assert "model" in output.lower() or "workspace" in output.lower()


def test_slash_compact():
    output = _run_repl_commands(["hello", "/compact", "/quit"])
    assert "message" in output.lower() or "token" in output.lower()


def test_slash_memories():
    output = _run_repl_commands(["/memories", "/quit"])
    # Either shows memory keys or "No memory store"
    assert "memory" in output.lower()


def test_slash_mcp():
    output = _run_repl_commands(["/mcp", "/quit"])
    assert "mcp" in output.lower()


def test_slash_new():
    output = _run_repl_commands(["/new", "/quit"])
    assert "new" in output.lower() or "session" in output.lower()


def test_slash_help():
    output = _run_repl_commands(["/help", "/quit"])
    assert "/model" in output
    assert "/status" in output
    assert "/compact" in output


def test_slash_unknown():
    output = _run_repl_commands(["/unknown", "/quit"])
    assert "unknown" in output.lower()


def test_repl_commands_parse():
    from ziva_runtime.app.cli import build_parser
    parser = build_parser()
    args = parser.parse_args(["repl", "--workspace", ".", "--approval", "full-auto"])
    assert args.command == "repl"
    assert args.approval == "full-auto"


def test_run_with_events_streams_output():
    from ziva_runtime.app.cli import _run_with_events

    captured = io.StringIO()
    original_stdout = sys.stdout
    sys.stdout = captured

    async def _go():
        root = Path(__file__).resolve().parents[1]
        rt = Runtime.create(workspace_root=root, model_adapter=FakeAdapter(["streamed output"]))
        await _run_with_events(rt, [ChatMessage(role="user", content="hi")], session_id="s1")

    try:
        asyncio.run(_go())
    finally:
        sys.stdout = original_stdout

    assert "streamed output" in captured.getvalue()


def test_repl_quit():
    from ziva_runtime.app.cli import _repl_loop

    async def _go():
        root = Path(__file__).resolve().parents[1]
        rt = Runtime.create(workspace_root=root, model_adapter=FakeAdapter(["response"]))

        inputs = iter(["/quit"])
        import builtins
        original_input = builtins.input

        def fake_input(prompt=""):
            return next(inputs)

        builtins.input = fake_input
        try:
            await _repl_loop(rt, "full-auto")
        finally:
            builtins.input = original_input

    asyncio.run(_go())


def test_run_no_stream_flag():
    from ziva_runtime.app.cli import build_parser
    parser = build_parser()
    args = parser.parse_args(["run", "--no-stream", "hello"])
    assert args.no_stream is True
