from pathlib import Path

from ziva_runtime.capabilities.registries import CapabilityRegistry
from ziva_runtime.plugins.loader import load_plugins


def test_all_new_plugins_load():
    root = Path(__file__).resolve().parents[1]
    registry = CapabilityRegistry()
    load_plugins([root / "plugins"], registry)

    tool_names = {t.instance.spec()["name"] for t in registry.list_kind("tool")}
    assert "read_file" in tool_names
    assert "write_file" in tool_names
    assert "grep" in tool_names
    assert "apply_patch" in tool_names
    assert "update_plan" in tool_names


def test_memory_stores_loaded():
    root = Path(__file__).resolve().parents[1]
    registry = CapabilityRegistry()
    load_plugins([root / "plugins"], registry)

    mem_ids = {m.id for m in registry.list_kind("memory")}
    assert "memory.inmemory" in mem_ids


def test_no_duplicate_tools():
    root = Path(__file__).resolve().parents[1]
    registry = CapabilityRegistry()
    load_plugins([root / "plugins"], registry)

    tools = registry.list_kind("tool")
    names = [t.instance.spec()["name"] for t in tools]
    assert len(names) == len(set(names))


def test_tool_spec_structures():
    root = Path(__file__).resolve().parents[1]
    registry = CapabilityRegistry()
    load_plugins([root / "plugins"], registry)

    for t in registry.list_kind("tool"):
        spec = t.instance.spec()
        assert "name" in spec
        assert "description" in spec
        assert "input_schema" in spec
        assert spec["input_schema"]["type"] == "object"
