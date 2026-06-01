import asyncio
import os
import re
import shutil


# ANSI escape code pattern - improved to handle OSC 133 sequences
ANSI_ESCAPE = re.compile(r'\x1b\[[0-9;]*[mGKHfABCDnsu]')
OSC_ESCAPE = re.compile(r'\x1b\].*?[\x07\x1b\\]')
OSC_133 = re.compile(r'\x1b\]133;[A-Z][^\x07\x1b]*?[\x07\x1b\\]')


def strip_ansi(text: str) -> str:
    """Strip ANSI escape codes from text, including OSC 133 sequences."""
    # Remove OSC 133 sequences
    text = OSC_133.sub('', text)
    # Remove other OSC sequences
    text = OSC_ESCAPE.sub('', text)
    # Remove standard ANSI escape codes
    text = ANSI_ESCAPE.sub('', text)
    return text


class ShellTool:
    """Enhanced shell tool with workdir, ANSI stripping, and unified truncation."""

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
                    "workdir": {"type": "string", "description": "The working directory to run the command in. Use this instead of 'cd' commands"},
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
            return {"error": "invalid_input", "message": "command is required"}

        timeout = min(int(input_data.get("timeout", 30)), self.MAX_TIMEOUT)
        workdir = input_data.get("workdir", os.getcwd())

        try:
            # Load .env if exists
            env = self._load_env_vars(workdir)

            # Use zsh if available, otherwise bash, fallback to /bin/sh
            shell = shutil.which("zsh") or shutil.which("bash")
            if not shell:
                shell = "/bin/sh"

            proc = await asyncio.create_subprocess_exec(
                shell, "-c", command,
                cwd=workdir,
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE  # Separate stderr
            )

            try:
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
                timed_out = False
                exit_code = proc.returncode if proc.returncode is not None else 0
            except asyncio.TimeoutError:
                proc.kill()
                try:
                    await proc.wait()
                except Exception:
                    pass
                return {
                    "exit_code": -1,
                    "stdout": "",
                    "stderr": f"Command timed out after {timeout} seconds",
                    "timed_out": True
                }

            # Decode and clean stdout
            stdout_text = stdout.decode('utf-8', errors='replace') if stdout else ""
            stdout_text = strip_ansi(stdout_text)

            # Decode and clean stderr
            stderr_text = stderr.decode('utf-8', errors='replace') if stderr else ""
            stderr_text = strip_ansi(stderr_text)

            # Normalize line endings
            stdout_text = stdout_text.replace('\r\n', '\n').replace('\r', '\n')
            stderr_text = stderr_text.replace('\r\n', '\n').replace('\r', '\n')

            stdout_text = stdout_text.strip()
            stderr_text = stderr_text.strip()

            return {
                "exit_code": exit_code,
                "stdout": stdout_text if stdout_text else "(Command executed successfully with no output)" if exit_code == 0 else f"(Command failed with exit code {exit_code})",
                "stderr": stderr_text,
                "timed_out": False,
            }

        except PermissionError:
            return {"error": "permission_denied", "message": f"Permission denied executing command"}
        except FileNotFoundError:
            return {"error": "workdir_not_found", "message": f"Working directory not found: {workdir}"}
        except Exception as e:
            return {"error": "execution_failed", "message": str(e)}
