import pytest

from ziva.runtime import Runtime
from ziva.shared_types import ToolResult


class _EchoTool:
    """Test-only echo tool used by test_acp_stream and test_tool_loop."""

    def spec(self):
        return {
            "name": "echo",
            "description": "Echo the provided text back unchanged.",
            "input_schema": {
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
            },
        }

    async def run(self, arguments, context=None):
        return ToolResult(text=str(arguments.get("text", "")))


@pytest.fixture(autouse=True)
def _reset_adapter_registry():
    """Clear the module-level adapter cache before each test.

    Without this, the first test that calls _create_adapter populates the
    registry, and subsequent tests get a stale cached instance even when
    they pass a different config — breaking test isolation.
    """
    from ziva.runtime import _reset_adapter_registry

    _reset_adapter_registry()
    yield
    _reset_adapter_registry()


@pytest.fixture(autouse=True)
def _register_test_echo_tool(monkeypatch):
    """Register a test-only 'echo' tool for every Runtime created in tests.

    Several legacy tests (test_acp_stream, test_tool_loop) inject fake
    adapters that call an 'echo' tool. The production plugin set has no
    such tool, so registering it here lets those tests run without hitting
    the real network.
    """
    orig_create = Runtime.create

    @classmethod
    def _wrapped_create(cls, **kwargs):
        runtime = orig_create(**kwargs)
        runtime.registry.register(
            "tool.echo",
            "tool",
            _EchoTool(),
            {"id": "tool.echo", "type": "tool", "permissions": {}},
        )
        return runtime

    monkeypatch.setattr(Runtime, "create", _wrapped_create)
    yield
