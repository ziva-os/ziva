from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

import yaml


DEFAULT_CONFIG: Dict[str, Any] = {
    "model": {"name": "gpt-4.1"},
    "providers": [{"name": "OpenAI", "api_type": "openai_compatible", "api_key": "", "base_url": "", "models": [{"name": "gpt-4.1"}]}],
    "prompt": {"profile": "default", "variables": {}},
    "tool": {"allow": [], "deny": [], "max_rounds": 3},
    "skill": {
        "enabled": [],
        "extra_paths": ["~/.ziva/skills", "~/.agents/skills"],
    },
    "hooks": {"before_turn": [], "after_turn": [], "before_tool": [], "after_tool": []},
    "memory": {"backend": "inmemory", "context_window_tokens": 200000},
    "plugin": {"paths": ["./plugins"], "trust": {"unsigned": "low"}},
    "approval": {"policy": "suggest", "allow_without_prompt": []},
    "sandbox": {"mode": "off", "writable_dirs": [], "blocked_commands": []},
    "mcp": {"enabled": False, "servers": [], "extra_skill_paths": []},
}


def _deep_merge(base: Dict[str, Any], overlay: Dict[str, Any]) -> Dict[str, Any]:
    result = dict(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def _load_yaml(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ValueError(f"Config file must be a mapping: {path}")
    return data


def _expect_type(container: Dict[str, Any], key: str, typ, where: str) -> None:
    if key in container and not isinstance(container[key], typ):
        raise ValueError(f"{where}.{key} must be {typ.__name__}")


def validate_config(config: Dict[str, Any]) -> None:
    required_top = {"model", "prompt", "tool", "skill", "hooks", "memory", "plugin", "approval", "sandbox"}
    missing = required_top - set(config)
    if missing:
        raise ValueError(f"Missing top-level config sections: {sorted(missing)}")

    model = config.get("model", {})
    if not isinstance(model, dict):
        raise ValueError("model must be an object")
    if not isinstance(model.get("name"), str) or not model.get("name"):
        raise ValueError("model.name must be a non-empty string")

    providers = config.get("providers")
    if providers is not None:
        if not isinstance(providers, list):
            raise ValueError("providers must be a list")
        for i, p in enumerate(providers):
            if not isinstance(p, dict):
                raise ValueError(f"providers[{i}] must be an object")
            _expect_type(p, "api_type", str, f"providers[{i}]")
            _expect_type(p, "api_key", str, f"providers[{i}]")
            _expect_type(p, "base_url", str, f"providers[{i}]")
            if "models" in p and not isinstance(p["models"], list):
                raise ValueError(f"providers[{i}].models must be a list")

    prompt = config.get("prompt", {})
    if not isinstance(prompt, dict):
        raise ValueError("prompt must be an object")
    _expect_type(prompt, "profile", str, "prompt")
    _expect_type(prompt, "variables", dict, "prompt")

    tool = config.get("tool", {})
    if not isinstance(tool, dict):
        raise ValueError("tool must be an object")
    _expect_type(tool, "allow", list, "tool")
    _expect_type(tool, "deny", list, "tool")
    if "max_rounds" in tool and tool["max_rounds"] != 0 and (not isinstance(tool["max_rounds"], int) or tool["max_rounds"] <= 0):
        raise ValueError("tool.max_rounds must be a positive integer or 0 for unlimited")

    hooks = config.get("hooks", {})
    if not isinstance(hooks, dict):
        raise ValueError("hooks must be an object")
    for key in ("before_turn", "after_turn", "before_tool", "after_tool"):
        _expect_type(hooks, key, list, "hooks")

    memory = config.get("memory", {})
    if not isinstance(memory, dict):
        raise ValueError("memory must be an object")
    _expect_type(memory, "backend", str, "memory")
    if "context_window_tokens" in memory and (not isinstance(memory["context_window_tokens"], int) or memory["context_window_tokens"] <= 0):
        raise ValueError("memory.context_window_tokens must be a positive integer")

    plugin = config.get("plugin", {})
    if not isinstance(plugin, dict):
        raise ValueError("plugin must be an object")
    paths = plugin.get("paths")
    if not isinstance(paths, list) or not all(isinstance(p, str) for p in paths):
        raise ValueError("plugin.paths must be a list of strings")
    trust = plugin.get("trust", {})
    if not isinstance(trust, dict):
        raise ValueError("plugin.trust must be an object")

    approval = config.get("approval", {})
    if not isinstance(approval, dict):
        raise ValueError("approval must be an object")
    _expect_type(approval, "policy", str, "approval")
    _expect_type(approval, "allow_without_prompt", list, "approval")

    sandbox = config.get("sandbox", {})
    if not isinstance(sandbox, dict):
        raise ValueError("sandbox must be an object")
    _expect_type(sandbox, "mode", str, "sandbox")
    _expect_type(sandbox, "writable_dirs", list, "sandbox")
    _expect_type(sandbox, "blocked_commands", list, "sandbox")


def load_effective_config(
    global_path: Path | None = None,
    workspace_path: Path | None = None,
    session_override: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    config = dict(DEFAULT_CONFIG)
    if global_path:
        config = _deep_merge(config, _load_yaml(global_path))
    if workspace_path:
        config = _deep_merge(config, _load_yaml(workspace_path))
    if session_override:
        config = _deep_merge(config, session_override)
    validate_config(config)
    return config
