from plugins.tools.web_search.impl import WebSearchTool
from ziva_runtime.shared_types import RuntimeContext


def test_web_search_spec():
    tool = WebSearchTool()
    spec = tool.spec()
    assert spec["name"] == "web_search"
    assert "query" in spec["input_schema"]["required"]


def test_web_search_missing_query():
    tool = WebSearchTool()
    ctx = RuntimeContext(session_id="test", config={}, metadata={})
    import asyncio

    result = asyncio.run(tool.run({}, ctx))
    assert result["error"] == "invalid_input"


def test_web_search_parse_ddg():
    tool = WebSearchTool()
    html = """
    <a rel="nofollow" class="result__a" href="https://example.com">Example Title</a>
    <a class="result__snippet">Example snippet text</a>
    <a rel="nofollow" class="result__a" href="https://test.com">Test Title</a>
    <a class="result__snippet">Test snippet</a>
    """
    results = tool._parse_ddg_html(html, 10)
    assert len(results) == 2
    assert results[0]["title"] == "Example Title"
    assert results[0]["url"] == "https://example.com"


def test_web_search_parse_limit():
    tool = WebSearchTool()
    html = """
    <a rel="nofollow" class="result__a" href="https://a.com">A</a>
    <a class="result__snippet">SA</a>
    <a rel="nofollow" class="result__a" href="https://b.com">B</a>
    <a class="result__snippet">SB</a>
    <a rel="nofollow" class="result__a" href="https://c.com">C</a>
    <a class="result__snippet">SC</a>
    """
    results = tool._parse_ddg_html(html, 2)
    assert len(results) == 2
