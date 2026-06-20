import uuid
from typing import Any, Dict

from ziva_runtime.shared_types import RuntimeContext, ToolResult
from ziva_runtime.storage.file_storage import FileStorage


class ManageScheduledTasksTool:
    """Tool to manage scheduled background tasks (automations) in Ziva."""

    def spec(self) -> Dict[str, Any]:
        return {
            "name": "manage_scheduled_tasks",
            "description": (
                "Manage scheduled background tasks (automations). "
                "You can list, create, update, or delete tasks that automatically run a prompt on a timer. "
                "Actions: 'list', 'create', 'update', 'delete'."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["list", "create", "update", "delete"],
                        "description": "The action to perform.",
                    },
                    "task_id": {
                        "type": "string",
                        "description": "The ID of the task to update or delete.",
                    },
                    "name": {
                        "type": "string",
                        "description": "The name of the task (for create or update).",
                    },
                    "prompt": {
                        "type": "string",
                        "description": "The prompt for the agent to run automatically (for create or update).",
                    },
                    "interval_seconds": {
                        "type": "integer",
                        "description": "Interval in seconds to repeat the task (default 300, for create or update).",
                    },
                    "schedule_time": {
                        "type": "string",
                        "description": "Specific time of day to run the task (HH:MM:SS format, e.g., '09:00:00'). Optional.",
                    },
                    "enabled": {
                        "type": "boolean",
                        "description": "Whether the task is enabled (for update).",
                    },
                },
                "required": ["action"],
            },
        }

    async def run(self, input_data: Dict[str, Any], ctx: RuntimeContext) -> ToolResult:
        action = input_data.get("action")
        runtime = ctx.metadata.get("_runtime")
        if not runtime:
            return ToolResult(text="Error: runtime is not accessible", error=True)

        project_id = runtime.project_id

        if action == "list":
            tasks = FileStorage.list_automations(project_id)
            if not tasks:
                return ToolResult(text="No scheduled tasks found.")
            lines = []
            for t in tasks:
                lines.append(
                    f"- ID: {t.get('id')} | Name: '{t.get('name')}' | "
                    f"Enabled: {t.get('enabled', True)} | Interval: {t.get('interval_seconds')}s | "
                    f"Schedule Time: {t.get('schedule_time') or 'None'} | "
                    f"Prompt: {str(t.get('prompt'))[:50]}..."
                )
            return ToolResult(text="Scheduled tasks:\\n" + "\\n".join(lines))

        elif action == "create":
            prompt = input_data.get("prompt")
            if not prompt:
                return ToolResult(text="Error: 'prompt' is required to create a task.", error=True)
            
            task_id = str(uuid.uuid4())
            # For a new task, we also need a valid session. We can use the current session.
            session_id = ctx.session_id

            payload = {
                "id": task_id,
                "name": input_data.get("name") or "Untitled Task",
                "prompt": prompt,
                "interval_seconds": input_data.get("interval_seconds", 300),
                "session_id": session_id,
                "enabled": True,
            }
            if input_data.get("schedule_time"):
                payload["schedule_time"] = input_data.get("schedule_time")

            FileStorage.upsert_automation(project_id, payload)
            if hasattr(runtime, "automation_callback"):
                runtime.automation_callback()

            return ToolResult(text=f"Successfully created scheduled task. ID: {task_id}")

        elif action == "update":
            task_id = input_data.get("task_id")
            if not task_id:
                return ToolResult(text="Error: 'task_id' is required to update a task.", error=True)
            
            tasks = FileStorage.list_automations(project_id)
            target = next((t for t in tasks if t.get("id") == task_id), None)
            if not target:
                return ToolResult(text=f"Error: task_id '{task_id}' not found.", error=True)

            for k in ["name", "prompt", "interval_seconds", "schedule_time", "enabled"]:
                if k in input_data:
                    target[k] = input_data[k]
            
            FileStorage.upsert_automation(project_id, target)
            if hasattr(runtime, "automation_callback"):
                runtime.automation_callback()

            return ToolResult(text=f"Successfully updated scheduled task '{task_id}'.")

        elif action == "delete":
            task_id = input_data.get("task_id")
            if not task_id:
                return ToolResult(text="Error: 'task_id' is required to delete a task.", error=True)

            deleted = FileStorage.delete_automation(project_id, task_id)
            if deleted:
                if hasattr(runtime, "automation_callback"):
                    runtime.automation_callback()
                return ToolResult(text=f"Successfully deleted scheduled task '{task_id}'.")
            else:
                return ToolResult(text=f"Error: task_id '{task_id}' not found.", error=True)

        else:
            return ToolResult(text=f"Error: Unknown action '{action}'", error=True)
