from __future__ import annotations

from typing import Any, Dict


class AskUserTool:
    """Ask the user a question when the agent needs clarification or more information."""

    def spec(self) -> Dict[str, Any]:
        return {
            "name": "ask_user",
            "description": (
                "Ask the user a question when you need clarification, more information, "
                "or a decision before proceeding. The question is shown in the UI and "
                "this tool BLOCKS until the user answers — you will not get a result "
                "back until they respond, so do not produce any post-question summary "
                "or repeat the question in your own words. Just call the tool and wait. "
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
                },
                "required": ["question"],
            },
        }

    async def run(self, input_data: Dict[str, Any], ctx: Any) -> Dict[str, Any]:
        question = input_data.get("question", "").strip()
        if not question:
            return {"error": "missing_question", "message": "question is required"}

        options = input_data.get("options", [])
        runtime = ctx.metadata.get("_runtime") if ctx else None
        if runtime is None:
            return {
                "error": "no_runtime",
                "message": "ask_user requires a runtime context to wait for user input.",
            }

        # Emit the question payload alongside the existing tool_start /
        # tool_end events so the UI can render the card immediately.
        # The tool coroutine itself blocks here until the HTTP reply
        # handler resolves the future — the model round stays open.
        await runtime._emit(
            ctx.session_id,
            {
                "type": "ask_user_question",
                "question": question,
                "options": options,
            },
        )

        return await runtime.await_user_answer(session_id=ctx.session_id)
