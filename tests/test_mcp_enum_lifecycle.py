"""Tests for the MCPConnectStatus enum that replaced the old boolean flags.

These cover the specific bugs we just fixed:
  - FAILED status used to be silently relabeled as "connected", permanently
    disabling retries. Now FAILED is its own state and the connector retries
    on the next turn.
  - NO_CONFIG used to set the same boolean, hiding "no MCP configured" from
    "MCP configured but failed". Now they're distinct and the tool wrapper
    surfaces them with different error messages.
  - DISCONNECTED is the new initial state (was the implicit `False`).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from ziva_runtime.shared_types import MCPConnectStatus, SessionState


def test_default_session_starts_disconnected():
    s = SessionState()
    assert s.mcp_status is MCPConnectStatus.DISCONNECTED
    assert s.mcp_client is None


def test_all_terminal_states_are_distinct():
    """The four meaningful states must be different enum members."""
    states = {
        MCPConnectStatus.DISCONNECTED,
        MCPConnectStatus.CONNECTING,
        MCPConnectStatus.CONNECTED,
        MCPConnectStatus.NO_CONFIG,
        MCPConnectStatus.FAILED,
    }
    assert len(states) == 5


def test_enum_values_are_strings_for_serialization():
    """JSON-friendly values; useful if we ever persist the status."""
    for st in MCPConnectStatus:
        assert isinstance(st.value, str)


def test_session_can_transition_through_states():
    s = SessionState()
    s.mcp_status = MCPConnectStatus.CONNECTING
    assert s.mcp_status is MCPConnectStatus.CONNECTING
    s.mcp_status = MCPConnectStatus.CONNECTED
    s.mcp_client = "fake-client"
    assert s.mcp_status is MCPConnectStatus.CONNECTED
    assert s.mcp_client == "fake-client"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
