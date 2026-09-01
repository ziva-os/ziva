"""Regression tests for MCP server config parsing (adapters/mcp/client.py)."""
from ziva.adapters.mcp.client import parse_mcp_config


def _parse(server_entry):
    cfg = {"mcp": {"enabled": True, "servers": {"srv": server_entry}}}
    servers = parse_mcp_config(cfg)
    assert len(servers) == 1
    return servers[0]


def test_string_command_splits_tokens():
    s = _parse({"command": "uvx minimax-coding-plan-mcp"})
    assert s.command == "uvx"
    assert s.args == ["minimax-coding-plan-mcp"]


def test_list_command_splits_head_tail():
    s = _parse({"command": ["/bin/sh", "/opt/ensure-chromium.sh"]})
    assert s.command == "/bin/sh"
    assert s.args == ["/opt/ensure-chromium.sh"]


def test_string_command_plus_args_key_merges():
    """The Android patcher historically wrote ``command: /bin/sh`` +
    ``args: [/opt/ensure-chromium.sh]``. The string branch used to ignore
    the args key, launching a bare shell with no script."""
    s = _parse({"command": "/bin/sh", "args": ["/opt/ensure-chromium.sh"]})
    assert s.command == "/bin/sh"
    assert s.args == ["/opt/ensure-chromium.sh"]


def test_command_absent_uses_args_key():
    s = _parse({"args": ["serve", "--port", "9"], "url": "http://x"})
    assert s.args == ["serve", "--port", "9"]


def test_disabled_server_skipped():
    cfg = {"mcp": {"enabled": True, "servers": {"a": {"command": "x", "enabled": False}}}}
    assert parse_mcp_config(cfg) == []
