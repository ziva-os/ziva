from __future__ import annotations

from typing import Any, Dict

from ziva_runtime.shared_types import RuntimeContext, ToolResult

# After this many tool calls without an `update_plan`, start nudging. The
# reminder re-fires every _THRESHOLD calls (8, 16, 24, …) so a long task
# gets periodic reminders until the model syncs the plan; `update_plan`
# resets the counter to 0, which silences the nudge.
_THRESHOLD = 8


class PlanReminderHook:
    event_name: str = "after_tool"
    matcher: str | None = None

    async def handle(self, payload: Dict[str, Any], ctx: RuntimeContext) -> Dict[str, Any]:
        runtime = ctx.metadata.get("_runtime")
        if not runtime:
            return payload

        session = runtime._get_session(ctx.session_id)
        plan = session.plan
        if not plan:
            return payload  # No active plan — nothing to nudge about.

        # Count this tool call against the "time since last update_plan"
        # tally. update_plan resets this to 0 when it runs, so the counter
        # measures exactly how stale the plan is.
        session.plan_tool_calls_since_update = getattr(
            session, "plan_tool_calls_since_update", 0
        ) + 1
        count = session.plan_tool_calls_since_update

        # Nudge every _THRESHOLD calls. Modulo means a long task without
        # updates gets periodic reminders rather than a single one.
        if count >= _THRESHOLD and count % _THRESHOLD == 0:
            total = len(plan)
            done = sum(
                1 for s in plan if isinstance(s, dict) and s.get("status") == "completed"
            )
            output = payload.get("output")
            reminder = (
                f"\n\n<reminder>The task plan has {total} step(s) ({done} completed) and "
                f"it has been {count} tool call(s) since the last `update_plan`. "
                f"If any step has made progress, call `update_plan` now to sync its status "
                f"— do not wait until everything is done.</reminder>"
            )
            if isinstance(output, ToolResult):
                output.text += reminder

        return payload
