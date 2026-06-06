from ziva_runtime.shared_types import ToolResult


class UpdatePlanTool:
    def spec(self):
        return {
            "name": "update_plan",
            "description": "Update the task plan with step statuses",
            "input_schema": {
                "type": "object",
                "properties": {
                    "steps": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "id": {"type": "string"},
                                "description": {"type": "string"},
                                "status": {"type": "string", "enum": ["pending", "in_progress", "completed"]},
                            },
                            "required": ["id", "description", "status"],
                        },
                    }
                },
                "required": ["steps"],
            },
        }

    async def run(self, input_data, ctx):
        steps = input_data.get("steps", [])
        if not isinstance(steps, list):
            return ToolResult(text="Error: invalid_input\nsteps must be an array", error=True)

        valid_statuses = {"pending", "in_progress", "completed"}
        validated = []
        for step in steps:
            if not isinstance(step, dict):
                continue
            status = step.get("status", "pending")
            if status not in valid_statuses:
                return ToolResult(text=f"Error: invalid_status\nInvalid status: {status}", error=True)
            validated.append({
                "id": step.get("id", ""),
                "description": step.get("description", ""),
                "status": status,
            })

        ctx.metadata["plan"] = validated
        status_counts = {}
        for s in validated:
            st = s["status"]
            status_counts[st] = status_counts.get(st, 0) + 1
        parts = [f"{c} {st}" for st, c in status_counts.items()]
        return ToolResult(
            text=f"Plan updated: {len(validated)} steps ({', '.join(parts)})",
            metadata={"plan": validated, "total": len(validated)},
        )
