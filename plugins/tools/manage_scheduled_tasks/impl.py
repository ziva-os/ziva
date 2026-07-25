"""Manage scheduled background tasks (automations) in Ziva.

Discriminated-union schedule design (modeled after OpenClaw's
`src/agents/tools/cron-tool.ts`):

    schedule: {
      "kind": "every" | "daily" | "weekly",
      # kind=every:  "interval_seconds": int
      # kind=daily:  "time": "HH:MM", ["tz": "IANA"]
      # kind=weekly: "days": ["MO".."SU"], "time": "HH:MM", ["tz"]
    }

The model-facing JSON Schema keeps all schedule fields siblings on the
same object (no nested union), tolerates any extras, and lets
`ziva.scheduled.normalize_schedule` strip the irrelevant ones at write
time. See `src/ziva/scheduled.py` for the shared parsing/normalization
helpers used by both this tool and the HTTP server.
"""

import uuid
from typing import Any, Dict

from ziva.scheduled import (
    ScheduleError,
    compute_next_run,
    describe_schedule,
    normalize_schedule,
)
from ziva.shared_types import RuntimeContext, ToolResult
from ziva.storage.file_storage import FileStorage


class ManageScheduledTasksTool:
    """Manage recurring background tasks.

    Each task sends a `prompt` to the agent on a schedule. Scheduling
    mode is chosen by ``schedule.kind``. See `SPEC_DESCRIPTION` below
    for the full model-facing documentation.
    """

    # -- Tool spec ---------------------------------------------------------

    SPEC_DESCRIPTION = (
        "Manage recurring background tasks in Ziva. Each task sends a "
        "`prompt` to the agent on a schedule you pick via `schedule.kind`:\n"
        "  - `every`  — fixed interval (seconds). Requires "
        "`schedule.interval_seconds`.\n"
        "  - `daily`  — once per day at `schedule.time` (HH:MM, local or "
        "`schedule.tz`).\n"
        "  - `weekly` — on `schedule.days` (ISO weekday codes MO..SU) at "
        "`schedule.time`.\n"
        "\n"
        "All other `schedule.*` fields are ignored by validation; the "
        "canonical form is rebuilt on save. Times default to the host's "
        "local timezone unless `schedule.tz` is set to an IANA name like "
        "'Asia/Shanghai' or 'America/New_York'.\n"
        "\n"
        "Actions:\n"
        "  list   — show all tasks with their schedules\n"
        "  create — add a new task (requires `prompt` + `schedule`)\n"
        "  update — modify a task by `task_id` (any subset of fields)\n"
        "  delete — remove a task by `task_id`\n"
        "  get    — fetch full details of one task\n"
        "  run    — trigger a task immediately without changing its schedule\n"
        "\n"
        "Examples:\n"
        '  // every 5 minutes\n'
        '  {"action":"create","name":"PR monitor","prompt":"check open PRs",'
        '"schedule":{"kind":"every","interval_seconds":300}}\n'
        "\n"
        '  // every day at 21:00 local\n'
        '  {"action":"create","prompt":"summarize today",'
        '"schedule":{"kind":"daily","time":"21:00"}}\n'
        "\n"
        '  // weekdays at 09:00 Shanghai time\n'
        '  {"action":"create","prompt":"...",'
        '"schedule":{"kind":"weekly","days":["MO","TU","WE","TH","FR"],'
        '"time":"09:00","tz":"Asia/Shanghai"}}\n'
        "\n"
        '  // pause without deleting\n'
        '  {"action":"update","task_id":"...","enabled":false}\n'
        "\n"
        '  // fire once now\n'
        '  {"action":"run","task_id":"..."}\n'
    )

    SPEC_INPUT_SCHEMA: Dict[str, Any] = {
        "type": "object",
        "description": (
            "Schedule mode is chosen by `schedule.kind`. Other fields are "
            "interpreted based on the kind; fields for other kinds are "
            "ignored at validation time."
        ),
        "properties": {
            "action": {
                "type": "string",
                "enum": ["list", "create", "update", "delete", "get", "run"],
                "description": "What to do.",
            },
            "task_id": {
                "type": "string",
                "description": "Required for update/delete/get/run.",
            },
            "name": {
                "type": "string",
                "maxLength": 200,
                "description": "Human label (optional).",
            },
            "prompt": {
                "type": "string",
                "description": (
                    "The prompt sent to the agent when the task fires. "
                    "Required for create; omit on update to keep current."
                ),
            },
            "enabled": {
                "type": "boolean",
                "description": "Pause the task without deleting it (update only).",
            },
            "schedule": {
                "type": "object",
                "description": (
                    "Schedule spec. Set `kind` to pick the mode. All "
                    "other fields are interpreted based on `kind`; "
                    "fields for other kinds are dropped at validation time."
                ),
                "properties": {
                    "kind": {
                        "type": "string",
                        "enum": ["every", "daily", "weekly"],
                        "description": "Scheduling mode.",
                    },
                    "interval_seconds": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 30 * 86400,
                        "description": (
                            "Repeat interval in seconds. Required when "
                            "kind=every. Capped at 30 days to prevent "
                            "drift and runaway schedules."
                        ),
                    },
                    "time": {
                        "type": "string",
                        "pattern": r"^([01]\d|2[0-3]):[0-5]\d$",
                        "description": (
                            "Time of day in HH:MM (24-hour). Required when "
                            "kind=daily or weekly. Interpreted in `tz` "
                            "if set, otherwise host local time."
                        ),
                    },
                    "tz": {
                        "type": "string",
                        "description": (
                            "IANA timezone, e.g. 'Asia/Shanghai' or "
                            "'America/New_York'. Defaults to host local."
                        ),
                    },
                    "days": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": 7,
                        "uniqueItems": True,
                        "items": {
                            "type": "string",
                            "enum": ["MO", "TU", "WE", "TH", "FR", "SA", "SU"],
                        },
                        "description": (
                            "ISO weekday codes. Required when kind=weekly. "
                            "Use ['MO','TU','WE','TH','FR'] for 'weekdays'."
                        ),
                    },
                },
                "required": ["kind"],
            },
        },
        "required": ["action"],
        "allOf": [
            {
                "if": {
                    "properties": {"action": {"const": "create"}},
                    "required": ["action"],
                },
                "then": {"required": ["prompt", "schedule"]},
            },
            {
                "if": {
                    "properties": {
                        "action": {"enum": ["update", "delete", "get", "run"]}
                    },
                    "required": ["action"],
                },
                "then": {"required": ["task_id"]},
            },
        ],
    }

    def spec(self) -> Dict[str, Any]:
        return {
            "name": "manage_scheduled_tasks",
            "description": self.SPEC_DESCRIPTION,
            "input_schema": self.SPEC_INPUT_SCHEMA,
        }

    # -- Execution ---------------------------------------------------------

    async def run(self, input_data: Dict[str, Any], ctx: RuntimeContext) -> ToolResult:
        action = input_data.get("action")
        runtime = ctx.metadata.get("_runtime")
        if not runtime:
            return ToolResult(text="Error: runtime is not accessible", error=True)

        project_id = runtime.project_id

        if action == "list":
            return self._list(project_id)
        if action == "get":
            return self._get(project_id, input_data.get("task_id", ""))
        if action == "create":
            return await self._create(input_data, ctx, project_id, runtime)
        if action == "update":
            return await self._update(input_data, ctx, project_id, runtime)
        if action == "delete":
            return self._delete(project_id, input_data.get("task_id", ""), runtime)
        if action == "run":
            return await self._run_now(project_id, input_data.get("task_id", ""), runtime)
        return ToolResult(text=f"Error: Unknown action '{action}'", error=True)

    # -- Action handlers ---------------------------------------------------

    def _list(self, project_id: str) -> ToolResult:
        tasks = FileStorage.list_automations(project_id)
        if not tasks:
            return ToolResult(text="No scheduled tasks found.")
        lines = []
        for t in tasks:
            lines.append(
                f"- ID: {t.get('id')} | Name: '{t.get('name')}' | "
                f"Enabled: {t.get('enabled', True)} | "
                f"Mode: {describe_schedule(t)} | "
                f"Prompt: {str(t.get('prompt'))[:50]}..."
            )
        return ToolResult(text="Scheduled tasks:\n" + "\n".join(lines))

    def _get(self, project_id: str, task_id: str) -> ToolResult:
        if not task_id:
            return ToolResult(text="Error: 'task_id' is required to get a task.", error=True)
        tasks = FileStorage.list_automations(project_id)
        target = next((t for t in tasks if t.get("id") == task_id), None)
        if not target:
            return ToolResult(text=f"Error: task_id '{task_id}' not found.", error=True)
        import json

        return ToolResult(text=json.dumps(target, indent=2, ensure_ascii=False))

    async def _create(
        self,
        input_data: Dict[str, Any],
        ctx: RuntimeContext,
        project_id: str,
        runtime: Any,
    ) -> ToolResult:
        prompt = input_data.get("prompt")
        if not prompt:
            return ToolResult(text="Error: 'prompt' is required to create a task.", error=True)
        schedule_raw = input_data.get("schedule")
        if schedule_raw is None:
            return ToolResult(text="Error: 'schedule' is required to create a task.", error=True)
        try:
            schedule = normalize_schedule(schedule_raw)
        except ScheduleError as exc:
            return ToolResult(text=f"Error: invalid schedule — {exc}", error=True)

        # Drift-free grid: anchor now so the first run aligns to the
        # grid regardless of when the task happens to fire.
        if schedule["kind"] == "every" and "anchor_at" not in schedule:
            import time as _time
            schedule["anchor_at"] = int(_time.time())

        task_id = str(uuid.uuid4())
        session_id = ctx.session_id
        import time as _time

        payload = {
            "id": task_id,
            "name": input_data.get("name") or "Untitled Task",
            "prompt": prompt,
            "enabled": True,
            "session_id": session_id,
            "schedule": schedule,
            "created_at": int(_time.time()),
            "updated_at": int(_time.time()),
        }
        FileStorage.upsert_automation(project_id, payload)
        if hasattr(runtime, "automation_callback"):
            runtime.automation_callback()
        return ToolResult(
            text=(
                f"Successfully created scheduled task. ID: {task_id}\n"
                f"Schedule: {describe_schedule(schedule)}\n"
                f"Name: {payload['name']}"
            )
        )

    async def _update(
        self,
        input_data: Dict[str, Any],
        ctx: RuntimeContext,
        project_id: str,
        runtime: Any,
    ) -> ToolResult:
        task_id = input_data.get("task_id")
        if not task_id:
            return ToolResult(text="Error: 'task_id' is required to update a task.", error=True)

        tasks = FileStorage.list_automations(project_id)
        target = next((t for t in tasks if t.get("id") == task_id), None)
        if not target:
            return ToolResult(text=f"Error: task_id '{task_id}' not found.", error=True)

        # Apply simple scalar fields first.
        for k in ("name", "prompt", "enabled"):
            if k in input_data:
                target[k] = input_data[k]

        # Schedule change: re-normalize from the merged view so the
        # user can change `kind` + `time` + `days` in one call.
        if "schedule" in input_data:
            try:
                target["schedule"] = normalize_schedule(input_data["schedule"])
            except ScheduleError as exc:
                return ToolResult(text=f"Error: invalid schedule — {exc}", error=True)

        import time as _time
        target["updated_at"] = int(_time.time())
        FileStorage.upsert_automation(project_id, target)
        if hasattr(runtime, "automation_callback"):
            runtime.automation_callback()
        return ToolResult(
            text=(
                f"Successfully updated scheduled task '{task_id}'.\n"
                f"Schedule: {describe_schedule(target)}"
            )
        )

    def _delete(self, project_id: str, task_id: str, runtime: Any) -> ToolResult:
        if not task_id:
            return ToolResult(text="Error: 'task_id' is required to delete a task.", error=True)
        deleted = FileStorage.delete_automation(project_id, task_id)
        if deleted:
            if hasattr(runtime, "automation_callback"):
                runtime.automation_callback()
            return ToolResult(text=f"Successfully deleted scheduled task '{task_id}'.")
        return ToolResult(text=f"Error: task_id '{task_id}' not found.", error=True)

    async def _run_now(self, project_id: str, task_id: str, runtime: Any) -> ToolResult:
        if not task_id:
            return ToolResult(text="Error: 'task_id' is required to run a task.", error=True)
        tasks = FileStorage.list_automations(project_id)
        target = next((t for t in tasks if t.get("id") == task_id), None)
        if not target:
            return ToolResult(text=f"Error: task_id '{task_id}' not found.", error=True)
        # The HTTP server exposes a trigger_automation_now path; if it's
        # wired up on the runtime, use it. Otherwise tell the user to
        # run the task via the UI's "Run now" button (which calls
        # POST /automations/{aid}/run).
        trigger = getattr(runtime, "trigger_automation_now", None)
        if trigger is None:
            return ToolResult(
                text=(
                    f"Task '{task_id}' is configured to run ({describe_schedule(target)}). "
                    "Use the UI's 'Run now' button (POST /automations/<id>/run) to trigger "
                    "an immediate execution."
                )
            )
        try:
            await trigger(task_id)
        except Exception as exc:
            return ToolResult(text=f"Error: trigger failed — {exc}", error=True)
        return ToolResult(text=f"Triggered task '{task_id}' ({describe_schedule(target)}).")


__all__ = ["ManageScheduledTasksTool"]