from __future__ import annotations

import sys
from contextlib import contextmanager
from typing import Any, Generator

from rich.console import Console
from rich.markdown import Markdown
from rich.syntax import Syntax
from rich.panel import Panel
from rich.text import Text


class CLIDisplay:
    def __init__(self) -> None:
        self.console = Console()

    def print_welcome(self, workspace: str, model: str, approval: str) -> None:
        self.console.print(f"\n  [bold]ziva[/bold] | model={model} | approval={approval}")
        self.console.print(f"  [dim]workspace: {workspace}[/dim]")
        self.console.print("  [dim]Type /help for commands, /quit to exit.[/dim]\n")

    def print_delta(self, text: str) -> None:
        sys.stdout.write(text)
        sys.stdout.flush()

    def print_newline(self) -> None:
        sys.stdout.write("\n")
        sys.stdout.flush()

    def print_model_response(self, content: str) -> None:
        self.console.print(Markdown(content))

    def print_tool_start(self, name: str, args: dict[str, Any]) -> None:
        abbrev = self._abbrev_args(args)
        self.console.print(f"  [dim]⚙ {name}[/dim] [dim]{abbrev}[/dim]")

    def print_tool_end(self, name: str, output: dict[str, Any] | Any) -> None:
        if isinstance(output, dict):
            if "error" in output:
                self.console.print(f"  [red]  error: {output['error']}[/red]")
            elif "stdout" in output:
                text = output["stdout"]
                if text:
                    for line in text.splitlines()[:8]:
                        self.console.print(f"  [dim]  {line}[/dim]")
                    if text.count("\n") > 8:
                        self.console.print(f"  [dim]  ... ({text.count(chr(10)) - 8} more lines)[/dim]")
        else:
            s = str(output)[:200]
            self.console.print(f"  [dim]  {s}[/dim]")

    def print_error(self, message: str) -> None:
        self.console.print(f"  [red]Error: {message}[/red]")

    def print_warning(self, message: str) -> None:
        self.console.print(f"  [yellow]{message}[/yellow]")

    def print_diff(self, diff_text: str) -> None:
        if not diff_text:
            self.console.print("  [dim]No changes.[/dim]")
            return
        syntax = Syntax(diff_text, "diff", theme="monokai", line_numbers=False)
        self.console.print(syntax)

    def print_usage(self, usage: dict[str, int] | None, latency_ms: int) -> None:
        if not usage:
            return
        prompt = usage.get("prompt_tokens", 0)
        completion = usage.get("completion_tokens", 0)
        total = prompt + completion
        self.console.print(
            f"  [dim]Tokens: {prompt}+{completion}={total} | {latency_ms}ms[/dim]"
        )

    def print_approval_prompt(self, tool_name: str, args: dict[str, Any], permission: str = "", patterns: list[str] | None = None) -> str:
        abbrev = self._abbrev_args(args)
        detail = f"Tool: [bold]{tool_name}[/bold]"
        if abbrev:
            detail += f"\nArgs: {abbrev}"
        if permission:
            detail += f"\nPermission: {permission}"
        self.console.print(Panel(detail, title="Approval Required", border_style="yellow"))

        try:
            reply = input("  [y]es once / [a]lways / [s]ession / [n]o > ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            return "n"

        return {"y": "once", "a": "always", "s": "always_session"}.get(reply, "reject")

    @contextmanager
    def print_spinner(self, message: str = "") -> Generator[None, None, None]:
        with self.console.status(message or "Thinking...", spinner="dots"):
            yield

    def _abbrev_args(self, args: dict[str, Any]) -> str:
        if not args:
            return ""
        for key in ("command", "file_path", "pattern", "query", "url"):
            if key in args:
                v = str(args[key])
                return v[:60] + ("..." if len(v) > 60 else "")
        return str(args)[:60]
