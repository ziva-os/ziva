from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import uuid
from pathlib import Path

from rich.console import Console
from rich.markup import escape

from ziva.protocols.acp import ACPServer, serve_stdio
from ziva.runtime import Runtime
from ziva.shared_types import ChatMessage
from ziva.transports.desktop_api.server import DesktopAPIServer
from ziva.transports.desktop_api.stt_warmup import start_stt_warmup


logger = logging.getLogger(__name__)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ziva", description="Ziva runtime CLI")
    parser.add_argument("--version", action="version", version="ziva 0.1.0")

    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="Run a single prompt")
    run.add_argument("message", nargs="+", help="Prompt")
    run.add_argument("--workspace", default=".")
    run.add_argument("--model", help="Override model name")
    run.add_argument("--session", help="Resume an existing session ID")
    run.add_argument("--approval", choices=["suggest", "full-auto"], default=None)
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

    desktop_restart = desktop_sub.add_parser(
        "restart",
        help="Tell the running Ziva desktop to relaunch (menu: Ziva → Restart Ziva)",
    )
    desktop_restart.add_argument(
        "--socket",
        default=None,
        help="Override restart socket path (default: ~/.ziva/restart.sock)",
    )
    desktop_restart.add_argument("--host", default="127.0.0.1", help="Backend host to wait on")
    desktop_restart.add_argument("--port", type=int, default=4097, help="Backend port to wait on")
    desktop_restart.add_argument(
        "--wait-down-timeout",
        type=float,
        default=10.0,
        help="Seconds to wait for the old backend to die (default: 10)",
    )
    desktop_restart.add_argument(
        "--wait-up-timeout",
        type=float,
        default=30.0,
        help="Seconds to wait for the new backend to come up (default: 30)",
    )

    repl = sub.add_parser("repl", help="Interactive multi-turn session")
    repl.add_argument("--workspace", default=".")
    repl.add_argument("--model", help="Override model name")
    repl.add_argument("--session", help="Resume an existing session ID")
    repl.add_argument("--approval", choices=["suggest", "full-auto"], default="full-auto")
    repl.add_argument("--max-rounds", type=int, default=None, help="Max tool-call rounds")

    return parser


def _runtime_for_workspace(path: str, session_override: dict | None = None) -> Runtime:
    # expanduser() must come before resolve(): Path("~/x").resolve() yields
    # "/Users/<u>/~/x" (literal ~ in the middle, because resolve() treats ~
    # as a normal character when the path is relative), which then gets
    # stored verbatim as the session's cwd and surfaces in the system
    # prompt + shell tool subprocess calls.
    workspace = Path(path).expanduser().resolve()
    global_config = Path.home() / ".ziva" / "config.yaml"
    return Runtime.create(
        workspace_root=workspace,
        global_config_path=global_config,
        session_override=session_override,
    )


# ---- Compact tool formatting (reference aicoder, but tighter) ----

def _key_arg(args: dict) -> str:
    for k in ("command", "file_path", "path", "pattern", "query", "url"):
        if k in args:
            v = str(args[k])
            return v[:70] + ("..." if len(v) > 70 else "")
    if args:
        return str(next(iter(args.values())))[:50]
    return ""


def _truncate(text: str, limit: int = 150) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[:limit] + "..."


def _format_tool_start(name: str, args: dict) -> str:
    ka = _key_arg(args)
    if ka:
        return f"[cyan]{escape(name)}[/cyan]({escape(ka)})"
    return f"[cyan]{escape(name)}[/cyan]"


def _format_tool_end(output, success: bool = True) -> str:
    status = "[green]✓[/green]" if success else "[red]✗[/red]"
    if not output:
        return status

    text = ""
    if isinstance(output, dict):
        if "error" in output:
            return f"[red]{escape(str(output['error'])[:150])}[/red] [red]✗[/red]"
        if "stdout" in output and output["stdout"]:
            text = str(output["stdout"])
    else:
        text = str(output)

    if not text:
        return status

    preview = _truncate(text, 200)
    lines = preview.split("\n")
    if len(lines) > 5:
        preview = "\n".join(lines[:5]) + f"... ({len(lines) - 5} more)"
    return f"[dim]{escape(preview)}[/dim]\n  {status}"


async def _run_streaming(runtime: Runtime, messages: list[ChatMessage], session_id: str | None = None) -> None:
    """Non-interactive run: auto-approves, outputs final answer only."""
    # Auto-approve all permissions in non-interactive mode
    try:
        from ziva.permissions import get_permission_manager
        perm_manager = get_permission_manager()

        def _on_pending(req):
            perm_manager.reply(req.id, "always_session")

        perm_manager.on_pending(_on_pending)
    except Exception:
        pass

    last_response = ""
    async for ev in runtime.chat_streaming(messages, session_id=session_id):
        if ev.get("type") == "model_response":
            last_response = ev.get("content", "")

    if last_response:
        # Strip <think/> blocks
        import re
        cleaned = re.sub(r'<think[^>]*>.*?</think\s*>', '', last_response, flags=re.DOTALL).strip()
        if cleaned:
            sys.stdout.write(cleaned + "\n")
            sys.stdout.flush()


# Backward compat alias for tests
_run_with_events = _run_streaming

def _parse_user_choice(raw: str, options: list, multi: bool) -> str:
    """Parse user input for ask_user options.

    Accepts: numbers ("1"), comma/space separated ("1 3"), or free text.
    Maps valid numbers to option labels; passes free text through as-is.
    """
    if not raw:
        return raw

    def _label(opt) -> str:
        return opt.get("label", "") if isinstance(opt, dict) else str(opt)

    # Try splitting by comma or space to get tokens
    tokens = [t for t in raw.replace(",", " ").split() if t]

    # Attempt to map all tokens to option indices
    mapped = []
    all_numeric = True
    for t in tokens:
        try:
            idx = int(t) - 1
            if 0 <= idx < len(options):
                mapped.append(_label(options[idx]))
            else:
                all_numeric = False
                break
        except ValueError:
            all_numeric = False
            break

    if all_numeric and mapped:
        return ", ".join(mapped)

    # Free text — return as-is
    return raw


async def _repl_loop(runtime: Runtime, approval_policy: str, session_id: str | None = None) -> None:
    """Interactive REPL loop with Rich TUI."""
    # Suppress non-warning logs in REPL
    logging.getLogger("ziva").setLevel(logging.WARNING)

    console = Console()

    # Try prompt_toolkit for better input; fall back to bare input()
    try:
        from prompt_toolkit import PromptSession
        input_session = PromptSession()
        use_prompt_toolkit = True
    except ImportError:
        use_prompt_toolkit = False

    # Register terminal approval callback for suggest mode
    if approval_policy == "suggest":
        try:
            from ziva.permissions import get_permission_manager
            perm_manager = get_permission_manager()

            def _on_pending(req):
                tool_name = req.tool.get("name", "unknown") if req.tool else "unknown"
                args = req.tool.get("arguments", {}) if req.tool else {}
                ka = _key_arg(args)
                console.print(f"\n[yellow]Allow [bold]{escape(tool_name)}[/bold]?[/yellow]" + (f" [dim]{escape(ka)}[/dim]" if ka else ""))
                try:
                    reply = input("  (y)es / (a)lways / (s)ession / [n]o > ").strip().lower()
                except (EOFError, KeyboardInterrupt):
                    reply = "n"
                result = {"y": "once", "a": "always", "s": "always_session"}.get(reply, "reject")
                perm_manager.reply(req.id, result)

            perm_manager.on_pending(_on_pending)
        except Exception:
            pass

    # Register ask_user callback so REPL can prompt inline
    _ask_user_lock = asyncio.Lock()

    async def _on_ask_user(sid, question, options, call_id, **kw):
        async with _ask_user_lock:
            loop = asyncio.get_running_loop()
            multi = kw.get("multi_select", False)
            console.print(f"\n[cyan bold]?[/cyan bold] [white]{escape(question)}[/white]")
            if options:
                for i, opt in enumerate(options):
                    if isinstance(opt, dict):
                        label = opt.get("label", "")
                        desc = opt.get("description", "")
                    else:
                        label = str(opt)
                        desc = ""
                    marker = "[cyan]◇[/cyan]" if multi else "[cyan]○[/cyan]"
                    line_out = f"  {marker} [cyan]{i+1}.[/cyan] {escape(label)}"
                    if desc:
                        line_out += f" [dim]— {escape(desc)}[/dim]"
                    console.print(line_out)
                if multi:
                    prompt_text = "  Choices (comma/space separated, or free text): "
                else:
                    prompt_text = "  Choice (number or text): "
                raw = await loop.run_in_executor(None, lambda: input(prompt_text).strip())
                answer = _parse_user_choice(raw, options, multi)
            else:
                answer = await loop.run_in_executor(None, lambda: input("  Answer: ").strip())
            runtime.set_user_answer(sid, answer, call_id)

    runtime.on_ask_user(_on_ask_user)

    history: list[ChatMessage] = []

    model_name = runtime.config.get("model", {}).get("name", "unknown")
    console.print(f"\n  [bold]ziva[/bold] [dim]model={model_name} approval={approval_policy}[/dim]")
    console.print(f"  [dim]workspace: {runtime.workspace_root}[/dim]")
    console.print("  [dim]Type /help for commands, /quit to exit.[/dim]\n")

    while True:
        # Collect input
        try:
            if use_prompt_toolkit:
                line = await input_session.prompt_async("❯ ")
            else:
                line = await asyncio.get_event_loop().run_in_executor(None, lambda: input("❯ "))
        except (EOFError, KeyboardInterrupt):
            console.print("\n[dim]Bye.[/dim]")
            break

        line = line.strip()
        if not line:
            continue

        # ---- Slash commands ----
        if line == "/quit":
            console.print("[dim]Bye.[/dim]")
            break
        elif line == "/help":
            console.print("  [bold]/quit[/bold]          Exit REPL")
            console.print("  [bold]/tools[/bold]         List available tools")
            console.print("  [bold]/approval N[/bold]    Change approval policy")
            console.print("  [bold]/history[/bold]       Show conversation history")
            console.print("  [bold]/clear[/bold]         Clear conversation history")
            console.print("  [bold]/model [name][/bold]  Show or switch model")
            console.print("  [bold]/new[/bold]           Start fresh session")
            console.print("  [bold]/compact[/bold]       Show conversation summary")
            console.print("  [bold]/diff[/bold]          Show git diff --stat")
            console.print("  [bold]/status[/bold]        Show runtime status")
            console.print("  [bold]/memories[/bold]      Show MEMORY.md content")
            console.print("  [bold]/mcp[/bold]           Show MCP server status")
            continue
        elif line == "/tools":
            for spec in runtime.list_tools():
                console.print(f"  [cyan]{spec['name']}[/cyan]: {spec.get('description', '')}")
            continue
        elif line.startswith("/approval "):
            new_policy = line.split(" ", 1)[1].strip()
            if new_policy in ("suggest", "full-auto"):
                runtime.config["approval"]["policy"] = new_policy
                console.print(f"  Approval policy → [cyan]{new_policy}[/cyan]")
            else:
                console.print(f"  [red]Unknown policy: {new_policy}[/red]")
            continue
        elif line == "/history":
            for msg in history:
                role_color = "cyan" if msg.role == "user" else "green"
                preview = msg.content[:80].replace("\n", " ")
                console.print(f"  [{role_color}]{msg.role}[/{role_color}] {escape(preview)}")
            continue
        elif line == "/clear":
            history = []
            console.print("  [dim]History cleared.[/dim]")
            continue
        elif line == "/model":
            mcfg = runtime.config.get("model", {})
            name = mcfg.get("name", "unknown")
            provider = mcfg.get("provider_name")
            shown = f"{provider}:{name}" if provider else name
            console.print(f"  Current model: [cyan]{shown}[/cyan]")
            avail = [f"{p.get('name')}:{m.get('name')}"
                     for p in (runtime.config.get("providers") or [])
                     for m in (p.get("models") or [])]
            if avail:
                console.print(f"  [dim]Available: {', '.join(avail[:24])}{'…' if len(avail) > 24 else ''}[/dim]")
            continue
        elif line.startswith("/model "):
            arg = line.split(" ", 1)[1].strip()
            # Accept "provider:model" (exact) or bare "model" (first-wins).
            provider_name: str | None = None
            if ":" in arg:
                provider_name, new_model = arg.split(":", 1)
                provider_name, new_model = provider_name.strip(), new_model.strip()
            else:
                new_model = arg
            providers = runtime.config.get("providers") or []
            match = None
            for p in providers:
                if provider_name and p.get("name") != provider_name:
                    continue
                for m in p.get("models") or []:
                    if (m.get("name") or "").lower() == new_model.lower():
                        match = (p.get("name"), m.get("name"))
                        break
                if match:
                    break
            if not match:
                avail = [f"{p.get('name')}:{m.get('name')}" for p in providers for m in (p.get("models") or [])]
                console.print(f"  [red]Unknown model '{arg}'. Available: {', '.join(avail[:24])}[/red]")
                continue
            runtime.config["model"]["name"] = match[1]
            runtime.config["model"]["provider_name"] = match[0]
            console.print(f"  Model → [cyan]{match[0]}:{match[1]}[/cyan]")
            continue
        elif line == "/effort" or line.startswith("/effort "):
            _EFFORTS = ("disabled", "low", "medium", "high", "xhigh", "max")
            arg = line.split(" ", 1)[1].strip() if line.startswith("/effort ") else ""
            if not arg:
                cur = runtime.config.get("model", {}).get("thinking_mode", "disabled")
                console.print(f"  Current effort: [cyan]{cur}[/cyan]")
                console.print(f"  [dim]Options: {', '.join(_EFFORTS)}[/dim]")
            elif arg in _EFFORTS:
                runtime.config["model"]["thinking_mode"] = arg
                console.print(f"  Effort → [cyan]{arg}[/cyan]")
            else:
                console.print(f"  [red]Unknown effort '{arg}'. Options: {', '.join(_EFFORTS)}[/red]")
            continue
        elif line == "/new":
            history = []
            session_id = str(uuid.uuid4())
            console.print(f"  New session: [dim]{session_id[:8]}[/dim]")
            continue
        elif line == "/compact":
            num_messages = len(history)
            total_chars = sum(len(msg.content) for msg in history)
            estimated_tokens = total_chars // 4
            sid_display = session_id[:8] if session_id else "none"
            console.print(f"  {num_messages} messages | ~{estimated_tokens} tokens | session: {sid_display}")
            continue
        elif line == "/diff":
            proc = await asyncio.create_subprocess_shell(
                "git diff --stat HEAD",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(runtime.workspace_root)
            )
            stdout, stderr = await proc.communicate()
            if stdout:
                console.print(escape(stdout.decode()))
            if stderr:
                console.print(f"[dim]{escape(stderr.decode())}[/dim]")
            continue
        elif line == "/status":
            model_name = runtime.config.get("model", {}).get("name", "unknown")
            approval = runtime.config.get("approval", {}).get("policy", "unknown")
            tools = runtime.list_tools()
            console.print(f"  Model: [cyan]{model_name}[/cyan]")
            console.print(f"  Approval: [cyan]{approval}[/cyan]")
            console.print(f"  Workspace: [dim]{runtime.workspace_root}[/dim]")
            console.print(f"  Tools: {len(tools)}")
            continue
        elif line == "/memories":
            from pathlib import Path
            mem_path = Path.home() / ".ziva" / "memories" / "MEMORY.md"
            if mem_path.exists():
                content = mem_path.read_text(encoding="utf-8")
                lines_count = len(content.splitlines())
                console.print(f"  Memory file: [dim]{mem_path}[/dim] ({lines_count} lines)")
                # Show first 30 lines as preview
                for ln in content.splitlines()[:30]:
                    console.print(f"  [dim]{ln}[/dim]")
                if lines_count > 30:
                    console.print(f"  [dim]... ({lines_count - 30} more lines)[/dim]")
            else:
                console.print("  [dim]No MEMORY.md yet[/dim]")
            continue
        elif line == "/mcp":
            mcp_config = runtime.config.get("mcp", {})
            servers_list = mcp_config.get("servers", []) if isinstance(mcp_config, dict) else []
            if servers_list:
                names = [s.get("name", "?") + f" ({s.get('transport', 'stdio')})" for s in servers_list if isinstance(s, dict)]
                console.print(f"  MCP servers: {', '.join(names)}")
            else:
                console.print("  [dim]No MCP servers configured[/dim]")
            continue
        elif line.startswith("/"):
            console.print(f"  [red]Unknown command: {escape(line)}[/red]")
            continue

        # ---- Normal message ----
        history.append(ChatMessage(role="user", content=line))
        last_assistant = ""
        console.print()
        try:
            async for ev in runtime.chat_streaming(history, session_id=session_id):
                et = ev.get("type")
                if et == "delta":
                    console.print(ev.get("content", ""), end="", markup=False)
                elif et == "model_response":
                    last_assistant = ev.get("content", "")
                    console.print()
                elif et == "tool_start":
                    console.print(_format_tool_start(ev.get("tool", ""), ev.get("arguments", {})))
                elif et == "tool_end":
                    output = ev.get("output", {})
                    success = not (isinstance(output, dict) and "error" in output)
                    console.print(_format_tool_end(output, success))
                elif et == "cancelled":
                    console.print("\n[yellow]Cancelled.[/yellow]")
                elif et == "doom_loop_detected":
                    console.print(f"\n[red]Doom loop: {ev.get('tool')}[/red]")
        except KeyboardInterrupt:
            console.print("\n[yellow]Interrupted.[/yellow]")
            continue

        if last_assistant:
            history.append(ChatMessage(role="assistant", content=last_assistant))
        console.print()


def _build_session_override(args) -> dict | None:
    override: dict = {}
    if getattr(args, "model", None):
        override.setdefault("model", {})["name"] = args.model
    if getattr(args, "approval", None):
        override.setdefault("approval", {})["policy"] = args.approval
    if getattr(args, "max_rounds", None):
        override.setdefault("tool", {})["max_rounds"] = args.max_rounds
    return override or None


def _suppress_anyio_cancel_scope():
    """Suppress benign anyio cancel-scope errors from MCP stdio cleanup.

    MCP SDK's stdio_client async generators get GC'd in a different task
    than they were created, causing "exit cancel scope in different task".
    This is harmless — the MCP SDK handles its own cleanup.
    """
    loop = asyncio.get_running_loop()
    _default_handler = loop.get_exception_handler()

    def _handler(loop, context):
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

    loop.set_exception_handler(_handler)


def _port_open(host: str, port: int, timeout: float = 0.2) -> bool:
    """True if a TCP connect to host:port succeeds within `timeout`."""
    import socket as _socket
    with _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        try:
            s.connect((host, port))
            return True
        except OSError:
            return False


def _backend_healthy(host: str, port: int, timeout: float = 1.0) -> bool:
    """True if the desktop backend's /status endpoint returns 200."""
    import urllib.request
    try:
        with urllib.request.urlopen(f"http://{host}:{port}/status", timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


def cmd_desktop_restart(args) -> int:
    """Synchronous handler for `ziva desktop restart`.

    Talks to the running Ziva desktop over its unix socket. UX mirrors the
    IM-bridge ``/restart`` slash command (see
    ``src/ziva/transports/im_bridge/bridge.py::_handle_restart``):

    * On dispatch: print "Restart scheduled; new process will send
      confirmation." — same line the IM bridge returns.
    * After the new backend is reachable on :4097: print "✅ Restarted in Xs"
      — same line the IM bridge's new process sends back to the chat.

    Returns 0 on success, 2 when no desktop is listening, 3 when the new
    backend fails to come back within ``--wait-up-timeout``.
    """
    import socket as _socket
    import time

    sock_path = (
        Path(args.socket).expanduser()
        if args.socket
        else Path.home() / ".ziva" / "restart.sock"
    )
    host = args.host
    port = args.port
    console = Console()

    # No socket → no Ziva desktop running.
    if not sock_path.exists():
        console.print(
            f"[red]No Ziva desktop is listening on {sock_path}.[/red]\n"
            f"  Open the Ziva desktop app first (the socket is created when "
            f"Electron starts), or use the menu: [bold]Ziva → Restart Ziva[/bold]."
        )
        return 2

    requested_at = time.time()
    try:
        with _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM) as s:
            s.settimeout(5.0)
            s.connect(str(sock_path))
            s.sendall(b"restart\n")
    except OSError as exc:
        console.print(f"[red]Failed to send restart: {exc}[/red]")
        return 2

    # Mirrors the IM-bridge handler's return text. See
    # bridge.py:504 — _handle_restart returns the same string when it can't
    # execvp synchronously.
    console.print("Restart scheduled; new process will send confirmation.")

    # Wait for the old backend to release :4097. A short bounded wait is
    # enough: the Electron before-quit handler (main.ts) blocks on
    # waitForBackendGone so :4097 frees promptly.
    t0 = time.time()
    while time.time() - t0 < args.wait_down_timeout:
        if not _port_open(host, port):
            break
        time.sleep(0.1)
    else:
        console.print(
            f"[yellow]backend still up after {args.wait_down_timeout}s; "
            f"continuing anyway[/yellow]"
        )

    # Wait for the new backend to come back healthy. When it does, print the
    # same "✅ Restarted in Xs" line the IM-bridge new process sends to chat.
    t0 = time.time()
    while time.time() - t0 < args.wait_up_timeout:
        if _backend_healthy(host, port):
            duration = max(0, int(time.time() - requested_at))
            console.print(f"[green]✅ Restarted in {duration}s[/green]")
            return 0
        time.sleep(0.2)

    console.print(
        f"[red]new backend did not come back up in {args.wait_up_timeout}s[/red]"
    )
    console.print("  Check [bold]~/.ziva/backend.log[/bold] for the new instance's stderr.")
    return 3


async def run_async(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.command == "run":
        _suppress_anyio_cancel_scope()
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
        _suppress_anyio_cancel_scope()
        session_override = _build_session_override(args)
        runtime = _runtime_for_workspace(args.workspace, session_override=session_override)
        await _repl_loop(runtime, args.approval, session_id=getattr(args, "session", None))
        return 0

    if args.command == "acp" and args.acp_command == "serve":
        runtime = _runtime_for_workspace(args.workspace)
        server = ACPServer(runtime)
        return await serve_stdio(server)

    if args.command == "desktop" and args.desktop_command == "restart":
        # Pure-sync handler; run_async just awaits the (trivially-fast) return.
        return cmd_desktop_restart(args)

    if args.command == "desktop" and args.desktop_command == "serve":
        _suppress_anyio_cancel_scope()
        runtime = _runtime_for_workspace(args.workspace)
        # Start the STT model warmup as early as possible — immediately
        # after Runtime is constructed, before DesktopAPIServer is even
        # instantiated. The warmup loads the 461 MB whisper weights +
        # JIT-compiles Metal kernels, which on a packaged Electron backend
        # takes 10–15 s on a cold start. Starting it here means the cost
        # overlaps with Electron's createWindow / loadURL / UI render,
        # so by the time the user sees the mic button the model is most
        # likely already loaded — instead of the first mic click paying
        # the full warmup cost. Failures are logged but never crash.
        start_stt_warmup(runtime)
        # Pre-import the IM-bridge adapters so the 30+ s lark_oapi cold
        # import (PyInstaller frozen-import tax) lands during process
        # startup, not inside ``feishu.start()``. Without this the IM
        # ``/restart`` ack path races the import and silently drops the
        # message. We run this concurrently with STT warmup so the two
        # ~10–30 s tasks overlap (each does its own blocking work in a
        # worker thread — STT loads whisper weights, warmup imports
        # lark_oapi + telegram's Bot). After both finish, bridge.start()
        # can call ``feishu.start()`` knowing ``_send_client`` will build
        # in < 1 ms instead of timing out at 30 s.
        async def _warmup_im_adapters_async() -> None:
            try:
                await asyncio.to_thread(_do_warmup)
            except Exception:
                logger.exception("im-bridge adapter warmup failed (non-fatal)")

        def _do_warmup() -> None:
            from ziva.transports.im_bridge.adapters import feishu as _feishu
            from ziva.transports.im_bridge.adapters import telegram as _telegram
            # Force the import; lazy internals (lark_oapi, telegram's
            # Bot) trigger now. _feishu.FeishuAdapter / _telegram.* evaluate
            # attribute access, which is enough to materialize module-level
            # imports (the top-level ``try: import lark_oapi`` block in
            # feishu.py is what actually costs ~30 s on a frozen binary).
            _feishu.FeishuAdapter  # noqa: B018
            _telegram.TelegramAdapter  # noqa: B018

        await _warmup_im_adapters_async()
        server = DesktopAPIServer(runtime)
        import signal
        stop_event = asyncio.Event()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop = asyncio.get_running_loop()
            loop.add_signal_handler(sig, stop_event.set)
        await server.start(host=args.host, port=args.port)
        print(f"Ziva desktop backend running on http://{args.host}:{args.port}", flush=True)
        try:
            await runtime._connect_mcp_if_needed()
        except Exception:
            pass
        try:
            await stop_event.wait()
        finally:
            for sig in (signal.SIGINT, signal.SIGTERM):
                asyncio.get_running_loop().remove_signal_handler(sig)
            # Graceful teardown so a SIGTERM/SIGINT exits cleanly instead of
            # dying dirty: cancel automations, shut the runtime down (MCP
            # clients), and clean up the aiohttp runner so :4097 is released
            # and child processes (e.g. the multiprocessing resource_tracker)
            # are reaped. Bounded so a stuck cleanup can't hang the quit.
            try:
                await asyncio.wait_for(server.stop(), timeout=10)
            except Exception:
                pass
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
