from __future__ import annotations

from typing import Any, Dict

from ziva_runtime.shared_types import ToolResult


class AskUserTool:
    """Ask the user a question when the agent needs clarification or more information."""

    def spec(self) -> Dict[str, Any]:
        return {
            "name": "ask_user",
            "description": (
                "Ask the user a question when you need clarification, more information, "
                "or a decision before proceeding. The question will be shown in the UI. "
                "Prefer this over making assumptions."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "question": {
                        "type": "string",
                        "description": "The question to ask the user",
                    },
                    "options": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional predefined options for the user to choose from",
                    },
                    "multi_select": {
                        "type": "boolean",
                        "default": False,
                        "description": "When true, the user can select multiple options (checkboxes). When false (default), single-select (radio buttons).",
                    },
                },
                "required": ["question"],
            },
        }

    async def run(self, input_data: Dict[str, Any], ctx: Any) -> ToolResult:
        question = input_data.get("question", "").strip()
        if not question:
            return ToolResult(text="Error: missing_question\nquestion is required", error=True)

        options = input_data.get("options", [])
        multi_select = bool(input_data.get("multi_select", False))
        runtime = ctx.metadata.get("_runtime") if ctx else None
        if runtime is None:
            return ToolResult(
                text="Error: no_runtime\nask_user requires a runtime context to wait for user input.",
                error=True,
            )

        call_id = (ctx.metadata or {}).get("_tool_call_id", "") if ctx else ""

        # Emit the question payload alongside the existing tool_start /
        # tool_end events so the UI can render the card immediately.
        # The tool coroutine itself blocks here until the HTTP reply
        # handler resolves the future — the model round stays open.
        await runtime._emit(
            ctx.session_id,
            {
                "type": "ask_user_question",
                "call_id": call_id,
                "question": question,
                "options": options,
                "multi_select": multi_select,
            },
        )

        raw = await runtime.await_user_answer(session_id=ctx.session_id, call_id=call_id)
        if isinstance(raw, ToolResult):
            return raw
        if isinstance(raw, dict) and raw.get("status") == "cancelled":
            return ToolResult(text="User cancelled the question.", metadata=raw)
        if isinstance(raw, dict) and raw.get("status") == "answered":
            return ToolResult(text=f"User answered: {raw.get('answer', '')}", metadata=raw)
        return ToolResult(text=str(raw), metadata={"raw": raw})
