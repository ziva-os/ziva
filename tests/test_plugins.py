from pathlib import Path

from ziva.capabilities.registries import CapabilityRegistry
from ziva.plugins.loader import load_plugins


def test_plugin_loading():
    root = Path(__file__).resolve().parents[1]
    registry = CapabilityRegistry()
    manifests = load_plugins([root / "plugins"], registry)
    assert manifests
    tool_specs = [r.instance.spec() for r in registry.list_kind("tool")]
    tool_names = {spec["name"] for spec in tool_specs}
    assert "read_file" in tool_names
    assert "apply_patch" in tool_names
