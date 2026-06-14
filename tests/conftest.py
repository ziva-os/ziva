import pytest


@pytest.fixture(autouse=True)
def _reset_adapter_registry():
    """Clear the module-level adapter cache before each test.

    Without this, the first test that calls _create_adapter populates the
    registry, and subsequent tests get a stale cached instance even when
    they pass a different config — breaking test isolation.
    """
    from ziva_runtime.runtime import _reset_adapter_registry
    _reset_adapter_registry()
    yield
    _reset_adapter_registry()
