import asyncio
import os
import shutil
import signal

from ziva.shared_types import ToolResult, resolve_tool_path


class ShellTool:
    """Shell tool with workdir support and process-group cleanup."""

    MAX_TIMEOUT = 600  # seconds (increased to 10 minutes)

    def spec(self):
        return {
            "name": "shell",
            "description": "Execute a shell command in the working directory. Supports workdir parameter and automatically strips ANSI codes.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "Shell command to execute"},
                    "timeout": {"type": "integer", "description": "Timeout in seconds (default 30, max 600)"},
                    "workdir": {"type": "string", "description": "The working directory to run the command in. Defaults to the session's workspace directory. Use this instead of 'cd' commands"},
                },
                "required": ["command"],
            },
        }

    def _load_env_vars(self, workdir: str) -> dict:
        """Load .env file from working directory if it exists."""
        env = os.environ.copy()
        env_path = os.path.join(workdir, ".env")
        if os.path.exists(env_path):
            try:
                with open(env_path, "r") as f:
                    for line in f:
                        line = line.strip()
                        if line and not line.startswith("#") and "=" in line:
                            key, val = line.split("=", 1)
                            env[key.strip()] = val.strip().strip('"').strip("'")
            except Exception:
                pass  # Silently fail if .env can't be loaded
        return env

    async def run(self, input_data, ctx):
        command = input_data.get("command")
        if not command:
            return ToolResult(text="Error: invalid_input\ncommand is required", error=True)

        # Timeout is enforced by the runtime executor (_execute_tool's single
        # wait_for, driven by this tool's `timeout` parameter) — no inner
        # wait_for here, which avoids a double-layered timeout where the
        # executor's default would cut off a longer timeout the caller asked for.
        # Resolve workdir relative to the session workspace; if the caller
        # passed an absolute path, leave it untouched.
        workdir = str(resolve_tool_path(ctx, input_data.get("workdir")))

        try:
            # Load .env if exists
            env = self._load_env_vars(workdir)
            env["LANG"] = env.get("LANG") or "en_US.UTF-8"
            env["LC_ALL"] = env.get("LC_ALL") or "en_US.UTF-8"

            # Use zsh if available, otherwise bash, fallback to /bin/sh
            shell_bin = shutil.which("zsh") or shutil.which("bash")
            if not shell_bin:
                shell_bin = "/bin/sh"

            proc = await asyncio.create_subprocess_exec(
                shell_bin, "-c", command,
                cwd=workdir,
                env=env,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )

            try:
                stdout, stderr = await proc.communicate()
            except asyncio.CancelledError:
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    try:
                        proc.kill()
                    except ProcessLookupError:
                        pass
                try:
                    await proc.wait()
                except Exception:
                    pass
                raise

            stdout_text = stdout.decode('utf-8', errors='replace') if stdout else ""
            stderr_text = stderr.decode('utf-8', errors='replace') if stderr else ""
            stdout_text = stdout_text.strip()
            stderr_text = stderr_text.strip()

            exit_code = proc.returncode if proc.returncode is not None else 0

            if exit_code == 0:
                display_text = stdout_text if stdout_text else "(Command executed successfully with no output)"
                return ToolResult(
                    text=f"Exit code: 0\n{display_text}",
                    metadata={"exit_code": exit_code, "stdout": stdout_text, "stderr": stderr_text}
                )
            else:
                return ToolResult(
                    text=f"Exit code: {exit_code}\n{stderr_text or stdout_text}",
                    error=True,
                    metadata={"exit_code": exit_code, "stdout": stdout_text, "stderr": stderr_text}
                )

        except PermissionError:
            return ToolResult(text="Error: permission_denied\nPermission denied executing command", error=True)
        except FileNotFoundError:
            return ToolResult(text=f"Error: workdir_not_found\nWorking directory not found: {workdir}", error=True)
        except Exception as e:
            return ToolResult(text=f"Error: execution_failed\n{e}", error=True)
