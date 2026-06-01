import asyncio
from plugins.tools.update_plan.impl import UpdatePlanTool
from ziva_runtime.shared_types import RuntimeContext


def test_update_plan_basic():
    tool = UpdatePlanTool()
    ctx = RuntimeContext(session_id="test", config={}, metadata={})
    steps = [
        {"id": "1", "description": "Read files", "status": "completed"},
        {"id": "2", "description": "Write code", "status": "in_progress"},
        {"id": "3", "description": "Test", "status": "pending"},
    ]
    result = asyncio.run(tool.run({"steps": steps}, ctx))
    assert result["total"] == 3
    assert ctx.metadata["plan"][0]["status"] == "completed"


def test_update_plan_invalid_status():
    tool = UpdatePlanTool()
    ctx = RuntimeContext(session_id="test", config={}, metadata={})
    result = asyncio.run(tool.run({"steps": [{"id": "1", "description": "test", "status": "invalid"}]}, ctx))
    assert "error" in result


def test_spec():
    tool = UpdatePlanTool()
    spec = tool.spec()
    assert spec["name"] == "update_plan"
