from __future__ import annotations

from typing import Any, Dict


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
                },
                "required": ["question"],
            },
        }

    async def run(self, input_data: Dict[str, Any], ctx: Any) -> Dict[str, Any]:
        question = input_data.get("question", "").strip()
        if not question:
            return {"error": "missing_question", "message": "question is required"}

        options = input_data.get("options", [])
        return {
            "status": "asked",
            "question": question,
            "options": options,
            "message": f"Question asked: '{question}'. Waiting for user response.",
        }
