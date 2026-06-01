from ziva_runtime.adapters.mcp.client import MCPClient, MCPServerConfig, MCPToolWrapper, parse_mcp_config


def test_parse_mcp_config_empty():
    result = parse_mcp_config({})
    assert result == []


def test_parse_mcp_config_stdio():
    config = {"mcp": {"servers": [{"name": "test", "command": "npx", "args": ["-y", "some-server"]}]}}
    result = parse_mcp_config(config)
    assert len(result) == 1
    assert result[0].name == "test"
    assert result[0].command == "npx"
    assert result[0].transport == "stdio"


def test_parse_mcp_config_http():
    config = {"mcp": {"servers": [{"name": "remote", "url": "https://example.com/mcp", "transport": "http"}]}}
    result = parse_mcp_config(config)
    assert len(result) == 1
    assert result[0].url == "https://example.com/mcp"


def test_mcp_tool_wrapper_spec():
    wrapper = MCPToolWrapper("test_tool", "A test tool", {"type": "object", "properties": {"x": {"type": "string"}}}, None)
    spec = wrapper.spec()
    assert spec["name"] == "test_tool"
    assert spec["description"] == "A test tool"


def test_mcp_config_in_default():
    from ziva_runtime.config.loader import load_effective_config
    config = load_effective_config()
    assert "mcp" in config
    assert config["mcp"]["servers"] == []


def test_mcp_server_config_defaults():
    cfg = MCPServerConfig(name="test")
    assert cfg.name == "test"
    assert cfg.command is None
    assert cfg.args == []
    assert cfg.url is None
    assert cfg.transport == "stdio"


def test_parse_mcp_config_multiple_servers():
    config = {
        "mcp": {
            "servers": [
                {"name": "local", "command": "node", "args": ["server.js"]},
                {"name": "remote", "url": "https://example.com/mcp", "transport": "http"},
            ]
        }
    }
    result = parse_mcp_config(config)
    assert len(result) == 2
    assert result[0].name == "local"
    assert result[1].name == "remote"


def test_mcp_client_init_empty_configs():
    client = MCPClient([])
    assert client.connected_servers == []
    assert client._connected is False


def test_parse_mcp_config_skips_invalid_entries():
    config = {"mcp": {"servers": [{"name": "valid", "command": "node"}, "invalid", None]}}
    result = parse_mcp_config(config)
    assert len(result) == 1
    assert result[0].name == "valid"
