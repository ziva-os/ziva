from __future__ import annotations

import asyncio
import json
from typing import Any, Dict

from ziva.capabilities.interfaces import BaseHook
from ziva.shared_types import RuntimeContext


class ShellHook(BaseHook):
    """Shell-script hook — executes ``impl.sh`` via ``bash``.

    * **stdin**: JSON payload (same fields the Python hook receives, plus
      ``event``, ``session_id``, ``workspace``, ``is_subagent``).
    * **stdout**: if valid JSON → merged into payload (can modify
      ``arguments`` / ``output``); if non-JSON → treated as a side-effect
      log (payload unchanged).
    * **exit code**: when ``block=True`` and exit ≠ 0, the payload gets
      ``_block=True`` + ``_block_reason`` so ``_run_hooks`` can abort.

    ``event_name`` / ``matcher`` / ``block`` / ``timeout`` / ``async_run``
    are assigned by the loader from ``manifest.yaml`` — exactly like Python
    hooks.
    """

    def __init__(self, script_path: str):
        self.script_path = script_path

    async def handle(self, payload: Dict[str, Any], ctx: RuntimeContext) -> Dict[str, Any]:
        stdin_json = self._build_stdin(payload, ctx)

        if self.async_run:
            asyncio.create_task(self._exec(stdin_json))
            return payload

        stdout, exit_code = await self._exec(stdin_json)

        # stdout 是有效 JSON → merge 到 payload（修改 arguments/output）
        try:
            modified = json.loads(stdout)
            if isinstance(modified, dict):
                payload = {**payload, **modified}
        except (json.JSONDecodeError, UnicodeDecodeError):
            pass  # 非 JSON = 纯副作用（通知/日志），payload 不变

        if exit_code != 0 and self.block:
            payload["_block"] = True
            payload["_block_reason"] = stdout.strip()[-500:] if stdout.strip() else f"exit {exit_code}"

        return payload

    def _build_stdin(self, payload: dict, ctx: RuntimeContext) -> str:
        data = dict(payload)
        data["event"] = self.event_name
        runtime = ctx.metadata.get("_runtime")
        if runtime:
            data["session_id"] = ctx.session_id
            data["workspace"] = getattr(runtime, "workspace_root", None)
            data["is_subagent"] = bool(ctx.metadata.get("_subagent"))
            # 当前模型信息 — shell hook 可能需要根据模型能力做决策
            # （如非多模态模型拦截 read_file 读图片）
            try:
                session = runtime._get_session(ctx.session_id)
                model_name = getattr(session, "model_name", None) or runtime.config.get("model", {}).get("name", "")
                data["model"] = model_name
                data["supports_image"] = runtime._model_supports_image(model_name)
            except Exception:
                pass
        return json.dumps(data, default=str)

    async def _exec(self, stdin_json: str) -> tuple[str, int]:
        try:
            proc = await asyncio.create_subprocess_shell(
                f"bash {self.script_path}",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            # timeout = 0 / None → 不限时（用于长时间运行的异步副作用脚本）
            if not self.timeout:
                stdout_b, _ = await proc.communicate(stdin_json.encode())
            else:
                stdout_b, _ = await asyncio.wait_for(
                    proc.communicate(stdin_json.encode()),
                    timeout=self.timeout,
                )
            return stdout_b.decode(errors="replace"), proc.returncode or 0
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except Exception:
                pass
            return "", -1
        except Exception as e:
            return str(e), -1
