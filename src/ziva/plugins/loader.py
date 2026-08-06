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
            if manifest.type in ("hook", "prompt"):
                # hook/prompt 类型：enabled_by_default=false 时直接跳过
                continue
            elif config:
                tool_id = manifest.id.replace("tool.", "")
                if not config.get("tools", {}).get(tool_id, {}).get("enabled", False):
                    continue
            else:
                continue

        # ① 创建实例 — hook 类型支持 shell 脚本（entry 无 ":"）
        if manifest.type == "hook" and ":" not in manifest.entry:
            from ziva.capabilities.shell_hook import ShellHook
            instance = ShellHook(script_path=str(manifest.path / manifest.entry))
        else:
            module_file, symbol = manifest.entry.split(":", 1)
            cls_or_fn = _load_symbol(manifest.path / module_file, symbol)
            instance = cls_or_fn() if isinstance(cls_or_fn, type) else cls_or_fn

        # ② hook 字段统一赋值：manifest 值 != None 才覆盖实例属性
        # （event_name/matcher 由 manifest 默认 None，block/timeout/async_run 同理）
        # —— 这样 Python hook 的 manifest 可以只写 event_name，省略的字段回退到 BaseHook 默认
        if manifest.type == "hook":
            if manifest.event_name is not None:
                instance.event_name = manifest.event_name
            if manifest.matcher is not None:
                instance.matcher = manifest.matcher
            if manifest.block is not None:
                instance.block = manifest.block
            if manifest.timeout is not None:
                instance.timeout = manifest.timeout
            if manifest.async_run is not None:
                instance.async_run = manifest.async_run

        registry.register(manifest.id, manifest.type, instance, {
            "version": manifest.version,
            "config": manifest.config,
            "permissions": manifest.permissions,
            "enabled_by_default": manifest.enabled_by_default,
            "path": str(manifest.path),
        })
        loaded.append(manifest)
    return loaded
