from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any, Iterable

from ziva.capabilities.registries import CapabilityRegistry
from ziva.plugins.manifest import PluginManifest, load_manifest


TYPE_DIRS = {
    "tool": "tools",
    "skill": "skills",
    "hook": "hooks",
    "memory": "memory",
    "prompt": "prompts",
}


def _load_symbol(file_path: Path, symbol: str) -> Any:
    spec = importlib.util.spec_from_file_location(f"ziva_plugin_{file_path.stem}_{symbol}", str(file_path))
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Failed to load plugin module from {file_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if not hasattr(module, symbol):
        raise RuntimeError(f"Plugin symbol {symbol} not found in {file_path}")
    return getattr(module, symbol)


def discover_manifests(plugin_roots: Iterable[Path]) -> list[PluginManifest]:
    manifests: list[PluginManifest] = []
    for root in plugin_roots:
        for type_name, dir_name in TYPE_DIRS.items():
            base = root / dir_name
            if not base.exists():
                continue
            for manifest_path in base.glob("*/manifest.yaml"):
                manifest = load_manifest(manifest_path)
                if manifest.type != type_name:
                    raise ValueError(
                        f"Manifest type mismatch for {manifest.id}: expected {type_name}, got {manifest.type}"
                    )
                manifests.append(manifest)
    return manifests


def load_plugins(plugin_roots: Iterable[Path], registry: CapabilityRegistry, config: dict[str, Any] | None = None) -> list[PluginManifest]:
    manifests = discover_manifests(plugin_roots)
    loaded = []
    for manifest in manifests:
        if not manifest.enabled_by_default:
            if config and manifest.type == "memory":
                backend = config.get("memory", {}).get("backend", "inmemory")
                if manifest.id != f"memory.{backend}":
                    continue
            elif config:
                tool_id = manifest.id.replace("tool.", "")
                if not config.get("tools", {}).get(tool_id, {}).get("enabled", False):
                    continue
            else:
                continue
        module_file, symbol = manifest.entry.split(":", 1)
        cls_or_fn = _load_symbol(manifest.path / module_file, symbol)
        instance = cls_or_fn() if isinstance(cls_or_fn, type) else cls_or_fn
        registry.register(manifest.id, manifest.type, instance, {
            "version": manifest.version,
            "config": manifest.config,
            "permissions": manifest.permissions,
            "enabled_by_default": manifest.enabled_by_default,
            "path": str(manifest.path),
        })
        loaded.append(manifest)
    return loaded
