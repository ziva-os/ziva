from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from ziva_runtime.protocols.acp import ACPServer, serve_stdio
from ziva_runtime.runtime import Runtime
from ziva_runtime.shared_types import ChatMessage
from ziva_runtime.transports.desktop_api.server import DesktopAPIServer
from ziva_runtime.app.display import CLIDisplay


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ziva", description="Ziva runtime CLI")
    parser.add_argument("--version", action="version", version="ziva 0.1.0")

    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="Run a single prompt")
    run.add_argument("message", nargs="+", help="Prompt")
    run.add_argument("--workspace", default=".")
    run.add_argument("--model", help="Override model name")
    run.add_argument("--session", help="Resume an existing session ID")
    run.add_argument("--approval", choices=["suggest", "auto-edit", "full-auto"], default=None)
    run.add_argument("--max-rounds", type=int, default=None, help="Max tool-call rounds")
    run.add_argument("--no-stream", action="store_true", help="Disable streaming output")

    acp = sub.add_parser("acp", help="ACP server")
    acp_sub = acp.add_subparsers(dest="acp_command", required=True)
    acp_serve = acp_sub.add_parser("serve", help="Serve ACP over stdio")
    acp_serve.add_argument("--workspace", default=".")

    desktop = sub.add_parser("desktop", help="Desktop backend API")
    desktop_sub = desktop.add_subparsers(dest="desktop_command", required=True)
    desktop_serve = desktop_sub.add_parser("serve", help="Serve desktop API")
    desktop_serve.add_argument("--workspace", default=".")
    desktop_serve.add_argument("--host", default="127.0.0.1")
    desktop_serve.add_argument("--port", type=int, default=4097)

    repl = sub.add_parser("repl", help="Interactive multi-turn session")
    repl.add_argument("--workspace", default=".")
    repl.add_argument("--model", help="Override model name")
    repl.add_argument("--session", help="Resume an existing session ID")
    repl.add_argument("--approval", choices=["suggest", "auto-edit", "full-auto"], default="suggest")
    repl.add_argument("--max-rounds", type=int, default=None, help="Max tool-call rounds")

    return parser


def _runtime_for_workspace(path: str, session_override: dict | None = None) -> Runtime:
    workspace = Path(path).resolve()
    return Runtime.create(workspace_root=workspace, workspace_config_path=workspace / ".ziva" / "config.yaml", session_override=session_override)


async def _run_streaming(runtime: Runtime, messages: list[ChatMessage], session_id: str | None = None) -> None:
    """Run chat with real-time token streaming to stdout."""
    try:
        async for ev in runtime.chat_streaming(messages, session_id=session_id):
            et = ev.get("type")
            if et == "delta":
                sys.stdout.write(ev.get("content", ""))
                sys.stdout.flush()
            elif et == "model_response":
                sys.stdout.write("\n")
            elif et == "tool_start":
                sys.stdout.write(f"\n  \033[2m[tool: {ev.get('tool')}]\033[0m ")
                sys.stdout.flush()
            elif et == "tool_end":
                output = ev.get("output", {})
                if isinstance(output, dict):
                    if "error" in output:
                        sys.stdout.write(f"\033[31merror: {output['error']}\033[0m")
                    elif "stdout" in output:
                        preview = output["stdout"][:200]
                        if preview:
                            sys.stdout.write(preview)
                sys.stdout.write("\n")
                sys.stdout.flush()
            elif et == "cancelled":
                sys.stdout.write("\n\033[33mCancelled.\033[0m\n")
            elif et == "doom_loop_detected":
                sys.stdout.write(f"\n\033[31mDoom loop detected: {ev.get('tool')}\033[0m\n")
    except (AttributeError, RuntimeError):
        # Fallback to non-streaming for adapters that don't support streaming
        result = await runtime.chat(messages, session_id=session_id)
        sys.stdout.write(result.content)
    sys.stdout.write("\n")


# Backward compat alias for tests
_run_with_events = _run_streaming


async def _repl_loop(runtime: Runtime, approval_policy: str, session_id: str | None = None) -> None:
    """Interactive REPL loop."""
    # Register terminal approval callback for suggest mode
    if approval_policy == "suggest":
        try:
            from ziva_runtime.permissions import get_permission_manager
            perm_manager = get_permission_manager()
            display_for_approval = CLIDisplay()
            def _on_pending(req):
                tool_name = req.tool.get("name", "unknown") if req.tool else "unknown"
                args = req.tool.get("arguments", {}) if req.tool else {}
                reply = display_for_approval.print_approval_prompt(tool_name, args, req.permission, req.patterns)
                perm_manager.reply(req.id, reply)
            perm_manager.on_pending(_on_pending)
        except Exception:
            pass

    history: list[ChatMessage] = []

    display = CLIDisplay()
    model_name = runtime.config.get("model", {}).get("name", "unknown")
    display.print_welcome(str(runtime.workspace_root), model_name, approval_policy)

    while True:
        try:
            line = await asyncio.get_event_loop().run_in_executor(None, lambda: input("ziva> "))
        except (EOFError, KeyboardInterrupt):
            print("\nBye.")
            break

        line = line.strip()
        if not line:
            continue

        if line == "/quit":
            print("Bye.")
            break
        elif line == "/help":
            print("  /quit          Exit REPL")
            print("  /tools         List available tools")
            print("  /approval N    Change approval policy (suggest/auto-edit/full-auto)")
            print("  /plan          Show current plan")
            print("  /history       Show conversation history")
            print("  /clear         Clear conversation history")
            print("  /model [name]  Show or switch model")
            print("  /new           Start fresh session (clear history)")
            print("  /compact       Show conversation summary (messages, tokens)")
            print("  /diff          Show git diff --stat")
            print("  /status        Show runtime status")
            print("  /memories      Show memory store keys")
            print("  /mcp           Show MCP server status")
            continue
        elif line == "/tools":
            for spec in runtime.list_tools():
                print(f"  {spec['name']}: {spec.get('description', '')}")
            continue
        elif line.startswith("/approval "):
            new_policy = line.split(" ", 1)[1].strip()
            if new_policy in ("suggest", "auto-edit", "full-auto"):
                runtime.config["approval"]["policy"] = new_policy
                print(f"  Approval policy set to: {new_policy}")
            else:
                print(f"  Unknown policy: {new_policy}")
            continue
        elif line == "/plan":
            plan = runtime.registry.list_kind("tool")
            # Check if update_plan tool has stored plan data
            print("  (plan tracking via update_plan tool)")
            continue
        elif line == "/history":
            for msg in history:
                role = msg.role
                preview = msg.content[:80].replace("\n", " ")
                print(f"  [{role}] {preview}")
            continue
        elif line == "/clear":
            history = []
            print("  History cleared.")
            continue
        elif line == "/model":
            # Show current model
            model_name = runtime.config.get("model", {}).get("name", "unknown")
            print(f"  Current model: {model_name}")
            continue
        elif line.startswith("/model "):
            # Switch model
            new_model = line.split(" ", 1)[1].strip()
            runtime.config["model"]["name"] = new_model
            print(f"  Model switched to: {new_model}")
            continue
        elif line == "/new":
            # Clear conversation history and create fresh session
            history = []
            import uuid
            session_id = str(uuid.uuid4())
            print(f"  New session started: {session_id}")
            continue
        elif line == "/compact":
            # Show conversation summary
            num_messages = len(history)
            # Estimate tokens: ~4 characters per token
            total_chars = sum(len(msg.content) for msg in history)
            estimated_tokens = total_chars // 4
            sid_display = session_id if session_id else "none"
            print(f"  {num_messages} messages | ~{estimated_tokens} tokens | session: {sid_display}")
            continue
        elif line == "/diff":
            # Run git diff --stat HEAD
            proc = await asyncio.create_subprocess_shell(
                "git diff --stat HEAD",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(runtime.workspace_root)
            )
            stdout, stderr = await proc.communicate()
            if stdout:
                for line_out in stdout.decode().splitlines():
                    print(f"  {line_out}")
            if stderr:
                for line_err in stderr.decode().splitlines():
                    print(f"  {line_err}")
            continue
        elif line == "/status":
            # Show runtime status
            model_name = runtime.config.get("model", {}).get("name", "unknown")
            approval = runtime.config.get("approval", {}).get("policy", "unknown")
            workspace = str(runtime.workspace_root)
            tools = runtime.list_tools()
            num_tools = len(tools)
            # Count plugins by checking for MCP or other plugin configs
            num_plugins = 0
            if runtime.config.get("mcp"):
                num_plugins = len(runtime.config.get("mcp", {}))
            print(f"  Model: {model_name}")
            print(f"  Approval: {approval}")
            print(f"  Workspace: {workspace}")
            print(f"  Tools: {num_tools}")
            print(f"  Plugins: {num_plugins}")
            continue
        elif line == "/memories":
            # Show memory store keys if loaded
            if hasattr(runtime, 'memory_store') and runtime.memory_store:
                try:
                    summary = runtime.memory_store.summarize()
                    print(f"  Memory keys: {list(summary.keys())}")
                except Exception as e:
                    print(f"  Error reading memory: {e}")
            else:
                print("  No memory store active")
            continue
        elif line == "/mcp":
            mcp_config = runtime.config.get("mcp", {})
            servers_list = mcp_config.get("servers", []) if isinstance(mcp_config, dict) else []
            if servers_list:
                names = [s.get("name", "unknown") + f" ({s.get('transport', 'stdio')})" for s in servers_list if isinstance(s, dict)]
                print(f"  MCP servers: {', '.join(names)}")
            else:
                print("  No MCP servers configured")
            continue
        elif line.startswith("/"):
            print(f"  Unknown command: {line}")
            continue

        history.append(ChatMessage(role="user", content=line))
        last_assistant = ""
        try:
            async for ev in runtime.chat_streaming(history, session_id=session_id):
                et = ev.get("type")
                if et == "delta":
                    sys.stdout.write(ev.get("content", ""))
                    sys.stdout.flush()
                elif et == "model_response":
                    last_assistant = ev.get("content", "")
                    sys.stdout.write("\n")
                elif et == "tool_start":
                    sys.stdout.write(f"\n  \033[2m[tool: {ev.get('tool')}]\033[0m ")
                    sys.stdout.flush()
                elif et == "tool_end":
                    output = ev.get("output", {})
                    if isinstance(output, dict):
                        if "error" in output:
                            sys.stdout.write(f"\033[31merror: {output['error']}\033[0m")
                        elif "stdout" in output and output["stdout"]:
                            sys.stdout.write(output["stdout"][:200])
                    sys.stdout.write("\n")
                    sys.stdout.flush()
                elif et == "cancelled":
                    sys.stdout.write("\n\033[33mCancelled.\033[0m\n")
                elif et == "doom_loop_detected":
                    sys.stdout.write(f"\n\033[31mDoom loop: {ev.get('tool')}\033[0m\n")
        except (AttributeError, RuntimeError):
            result = await runtime.chat(history, session_id=session_id)
            last_assistant = result.content
            sys.stdout.write(result.content)

        if last_assistant:
            history.append(ChatMessage(role="assistant", content=last_assistant))
        print()


def _build_session_override(args) -> dict | None:
    override: dict = {}
    if getattr(args, "model", None):
        override.setdefault("model", {})["name"] = args.model
    if getattr(args, "approval", None):
        override.setdefault("approval", {})["policy"] = args.approval
    if getattr(args, "max_rounds", None):
        override.setdefault("tool", {})["max_rounds"] = args.max_rounds
    return override or None


async def run_async(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.command == "run":
        session_override = _build_session_override(args)
        runtime = _runtime_for_workspace(args.workspace, session_override=session_override)
        prompt = " ".join(args.message)
        messages = [ChatMessage(role="user", content=prompt)]
        session_id = getattr(args, "session", None)

        if args.no_stream:
            result = await runtime.chat(messages, session_id=session_id)
            print(result.content)
        else:
            await _run_streaming(runtime, messages, session_id=session_id)
        return 0

    if args.command == "repl":
        session_override = _build_session_override(args)
        runtime = _runtime_for_workspace(args.workspace, session_override=session_override)
        await _repl_loop(runtime, args.approval, session_id=getattr(args, "session", None))
        return 0

    if args.command == "acp" and args.acp_command == "serve":
        runtime = _runtime_for_workspace(args.workspace)
        server = ACPServer(runtime)
        return await serve_stdio(server)

    if args.command == "desktop" and args.desktop_command == "serve":
        runtime = _runtime_for_workspace(args.workspace)
        server = DesktopAPIServer(runtime)
        import signal
        stop_event = asyncio.Event()
        loop = asyncio.get_running_loop()
        # Suppress benign anyio cancel-scope errors from MCP stdio
        # cleanup — these fire in background tasks when an MCP server
        # process exits and the async generator is GC'd in a different
        # task. The MCP SDK's own cleanup() handles it; this catches
        # the stragglers that escape to the event loop.
        _default_handler = loop.get_exception_handler()

        def _suppress_anyio_cancel_scope(loop, context):
            msg = context.get("message", "")
            exc = context.get("exception")
            if exc and "cancel scope" in str(exc):
                return
            if "cancel scope" in msg:
                return
            if _default_handler:
                _default_handler(loop, context)
            else:
                loop.default_exception_handler(context)

        loop.set_exception_handler(_suppress_anyio_cancel_scope)
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop_event.set)
        await server.start(host=args.host, port=args.port)
        # Pre-connect MCP servers at startup so the first turn
        # doesn't block on MCP initialization.
        try:
            await runtime._connect_mcp_if_needed()
        except Exception:
            pass
        try:
            await stop_event.wait()
        finally:
            for sig in (signal.SIGINT, signal.SIGTERM):
                loop.remove_signal_handler(sig)
            await server.stop()
        return 0

    raise RuntimeError("Unsupported command")


def main() -> None:
    import sys
    try:
        sys.exit(asyncio.run(run_async()))
    except (KeyboardInterrupt, SystemExit):
        sys.exit(0)


if __name__ == "__main__":
    main()
