import sys
from types import SimpleNamespace

import pytest

from ziva_runtime.adapters.mcp.client import (
    MCPClient,
    MCPServerConfig,
    MCPToolWrapper,
    mcp_call_result_to_tool_result,
    parse_mcp_config,
)


def test_parse_mcp_config_dict_format():
    config = {
        "mcp": {
            "servers": {
                "filesystem": {
                    "command": "npx",
                    "args": ["-y", "@modelcontextprotocol/server-filesystem"],
                    "environment": {"FOO": "bar"},
                }
            }
        }
    }
    configs = parse_mcp_config(config)
    assert len(configs) == 1
    cfg = configs[0]
    assert cfg.name == "filesystem"
    assert cfg.command == "npx"
    assert cfg.args == ["-y", "@modelcontextprotocol/server-filesystem"]
    assert cfg.environment == {"FOO": "bar"}


def test_parse_mcp_config_list_format():
    config = {
        "mcp": {
            "servers": [
                {
                    "name": "memory",
                    "command": "node",
                    "args": ["memory-server.js"],
                    "enabled": True,
                },
                {
                    "name": "disabled-server",
                    "command": "node",
                    "args": ["disabled.js"],
                    "enabled": False,
                },
            ]
        }
    }
    configs = parse_mcp_config(config)
    assert len(configs) == 1
    assert configs[0].name == "memory"


def test_parse_mcp_config_legacy_mcp_servers():
    config = {
        "mcpServers": {
            "fetch": {"command": "uvx", "args": ["mcp-server-fetch"], "transport": "stdio"}
        }
    }
    configs = parse_mcp_config(config)
    assert len(configs) == 1
    assert configs[0].name == "fetch"
    assert configs[0].transport == "stdio"


def test_parse_mcp_config_http_transport():
    config = {
        "mcp": {
            "servers": [
                {
                    "name": "remote",
                    "url": "https://example.com/mcp",
                    "transport": "streamable_http",
                    "headers": {"Authorization": "Bearer token"},
                }
            ]
        }
    }
    configs = parse_mcp_config(config)
    assert len(configs) == 1
    assert configs[0].transport == "streamable_http"
    assert configs[0].url == "https://example.com/mcp"
    assert configs[0].headers == {"Authorization": "Bearer token"}


def test_mcp_server_config_defaults():
    cfg = MCPServerConfig(name="test")
    assert cfg.command is None
    assert cfg.args == []
    assert cfg.transport == "stdio"
    assert cfg.timeout == 120


def test_mcp_tool_wrapper_spec():
    wrapper = MCPToolWrapper(
        tool_name="read_file",
        tool_description="Reads a file",
        tool_schema={"type": "object", "properties": {}},
        server_name="filesystem",
    )
    spec = wrapper.spec()
    assert spec["name"] == "read_file"
    assert spec["description"] == "Reads a file"
    assert spec["input_schema"] == {"type": "object", "properties": {}}


@pytest.mark.asyncio
async def test_mcp_tool_wrapper_run_requires_runtime(monkeypatch):
    wrapper = MCPToolWrapper(
        tool_name="t",
        tool_description="d",
        tool_schema={},
        server_name="s",
    )

    class FakeCtx:
        session_id = "sid"
        metadata = {}

    result = await wrapper.run({}, FakeCtx())
    assert result.error is True
    assert "mcp_unavailable" in result.text


def test_mcp_call_result_parses_text_only():
    result = {"content": [{"type": "text", "text": "hello world"}]}
    output = mcp_call_result_to_tool_result(result)
    assert output.text == "hello world"
    assert output.error is False


def test_mcp_call_result_parses_standard_content_types():
    result = {
        "content": [
            {"type": "text", "text": "hello"},
            {"type": "image", "mimeType": "image/jpeg", "data": "abc123"},
            {"type": "audio", "mimeType": "audio/mpeg", "data": "def456"},
            {"type": "resource", "resource": {"uri": "file:///tmp/a.txt", "mimeType": "text/plain", "text": "resource text"}},
            {"type": "resource_link", "uri": "file:///tmp/b.txt", "name": "b.txt"},
        ],
        "structuredContent": {"ok": True},
    }

    output = mcp_call_result_to_tool_result(result)

    assert output.text.startswith("hello")
    # Structured content is preserved in metadata for the UI, not duplicated in text.
    assert "resource text" in output.text
    assert "Resource link: b.txt" in output.text
    assert output.images == ["data:image/jpeg;base64,abc123"]
    assert output.metadata["audio"] == ["data:audio/mpeg;base64,def456"]
    assert output.metadata["structuredContent"] == {"ok": True}
    assert len(output.metadata["resources"]) == 2


def test_mcp_call_result_parses_object_aliases_and_errors():
    result = SimpleNamespace(
        isError=True,
        content=[
            SimpleNamespace(type="text", text="bad"),
            SimpleNamespace(type="image", mimeType="image/png", data=b"png"),
        ],
    )

    output = mcp_call_result_to_tool_result(result)

    assert output.error is True
    assert output.text == "bad"
    assert output.images == ["data:image/png;base64,cG5n"]


def test_mcp_call_result_uses_structured_content_when_no_text():
    result = {"content": [], "structured_content": {"answer": 42}}

    output = mcp_call_result_to_tool_result(result)

    # Non-text structured content is not serialized into LLM text; metadata is enough.
    assert output.text == ""
    assert output.metadata["structuredContent"] == {"answer": 42}


def test_mcp_call_result_extracts_llm_text_from_structured_fallback():
    result = {
        "content": [],
        "structuredContent": {"type": "text", "text": "structured natural language"},
    }

    output = mcp_call_result_to_tool_result(result)

    assert output.text == "structured natural language"
    assert output.metadata["structuredContent"] == {
        "type": "text",
        "text": "structured natural language",
    }
