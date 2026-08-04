from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict

import yaml


ALLOWED_TYPES = {"tool", "skill", "hook", "memory", "prompt"}


@dataclass
class PluginManifest:
    id: str
    type: str
    version: str
    entry: str
    config: Dict[str, Any]
    permissions: Dict[str, Any]
    enabled_by_default: bool
    path: Path
    # —— hook 专用（平铺在 manifest.yaml 顶层；None = 未声明，由 loader 回退到 BaseHook 默认值）——
    event_name: str | None = None
    matcher: str | None = None
    block: bool | None = None
    timeout: int | None = None
    async_run: bool | None = None


def load_manifest(path: Path) -> PluginManifest:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"Invalid manifest: {path}")

    for key in ("id", "type", "version", "entry"):
        if key not in raw:
            raise ValueError(f"Manifest missing required field {key}: {path}")

    manifest_type = str(raw["type"])
    if manifest_type not in ALLOWED_TYPES:
        raise ValueError(f"Manifest has unsupported type '{manifest_type}': {path}")

    plugin_id = str(raw["id"])
    if "." not in plugin_id:
        raise ValueError(f"Manifest id should contain namespace separator '.': {path}")

    entry = str(raw["entry"])
    if ":" not in entry and manifest_type != "hook":
        raise ValueError(f"Manifest entry must use 'module.py:Symbol' format: {path}")
    # hook 类型允许 entry 无 ":"（shell 脚本，如 impl.sh）

    cfg = raw.get("config", {}) or {}
    perms = raw.get("permissions", {}) or {}
    if not isinstance(cfg, dict):
        raise ValueError(f"Manifest config must be an object: {path}")
    if not isinstance(perms, dict):
        raise ValueError(f"Manifest permissions must be an object: {path}")

    # hook 专用：键不存在 / 值为 null → 保留 None；loader 见 None 会回退到 BaseHook 默认值
    block_raw = raw.get("block")
    timeout_raw = raw.get("timeout")
    async_raw = raw.get("async_run")
    return PluginManifest(
        id=plugin_id,
        type=manifest_type,
        version=str(raw["version"]),
        entry=entry,
        config=cfg,
        permissions=perms,
        enabled_by_default=bool(raw.get("enabled_by_default", True)),
        path=path.parent,
        event_name=raw.get("event_name"),
        matcher=raw.get("matcher"),
        block=bool(block_raw) if block_raw is not None else None,
        timeout=int(timeout_raw) if timeout_raw is not None else None,
        async_run=bool(async_raw) if async_raw is not None else None,
    )
