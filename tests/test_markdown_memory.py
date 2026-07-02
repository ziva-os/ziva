import asyncio
from pathlib import Path
from plugins.memory.markdown.impl import MarkdownMemoryStore
from ziva.shared_types import RuntimeContext

def test_put_and_search(tmp_path):
    store = MarkdownMemoryStore(str(tmp_path))
    ctx = RuntimeContext(session_id="test", config={}, metadata={})

    asyncio.run(store.put("test_key", {"data": "hello"}, ctx))
    results = asyncio.run(store.search("test", 10, ctx))

    assert len(results) == 1
    assert results[0]["key"] == "test_key"
    assert "hello" in results[0]["content"]

def test_put_and_summarize(tmp_path):
    store = MarkdownMemoryStore(str(tmp_path))
    ctx = RuntimeContext(session_id="test", config={}, metadata={})

    asyncio.run(store.put("key1", {"v": "1"}, ctx))
    asyncio.run(store.put("key2", {"v": "2"}, ctx))
    result = asyncio.run(store.summarize(ctx))

    assert result["total_keys"] == 2
    assert set(result["keys"]) == {"key1", "key2"}

def test_search_no_results(tmp_path):
    store = MarkdownMemoryStore(str(tmp_path))
    ctx = RuntimeContext(session_id="test", config={}, metadata={})

    asyncio.run(store.put("existing", {"v": "data"}, ctx))
    results = asyncio.run(store.search("nonexistent", 10, ctx))
    assert len(results) == 0

def test_persistence(tmp_path):
    db = tmp_path / "mem"
    ctx = RuntimeContext(session_id="test", config={}, metadata={})

    store1 = MarkdownMemoryStore(str(db))
    asyncio.run(store1.put("persist", {"data": "survives"}, ctx))

    store2 = MarkdownMemoryStore(str(db))
    results = asyncio.run(store2.search("persist", 10, ctx))
    assert len(results) == 1
    assert "survives" in results[0]["content"]

def test_empty_summarize(tmp_path):
    store = MarkdownMemoryStore(str(tmp_path))
    ctx = RuntimeContext(session_id="test", config={}, metadata={})
    result = asyncio.run(store.summarize(ctx))
    assert result["total_keys"] == 0

def test_overwrite(tmp_path):
    store = MarkdownMemoryStore(str(tmp_path))
    ctx = RuntimeContext(session_id="test", config={}, metadata={})

    asyncio.run(store.put("key", {"v": "first"}, ctx))
    asyncio.run(store.put("key", {"v": "second"}, ctx))

    results = asyncio.run(store.search("key", 10, ctx))
    assert len(results) == 1
    assert "second" in results[0]["content"]
