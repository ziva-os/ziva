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
            return {"error": "invalid_input", "message": "steps must be an array"}

        valid_statuses = {"pending", "in_progress", "completed"}
        validated = []
        for step in steps:
            if not isinstance(step, dict):
                continue
            status = step.get("status", "pending")
            if status not in valid_statuses:
                return {"error": "invalid_status", "message": f"Invalid status: {status}"}
            validated.append({
                "id": step.get("id", ""),
                "description": step.get("description", ""),
                "status": status,
            })

        ctx.metadata["plan"] = validated
        return {"plan": validated, "total": len(validated)}
