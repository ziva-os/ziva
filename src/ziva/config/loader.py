from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

import yaml


DEFAULT_CONFIG: Dict[str, Any] = {
    "model": {
        "name": "claude-sonnet-4-5",
        "max_tokens": 16384,
        "thinking_mode": "disabled",
        "thinking_budget_tokens": 4000,
    },
    "providers": [{"name": "Anthropic", "api_type": "anthropic", "api_key": "", "base_url": "https://api.anthropic.com", "models": [{"name": "claude-sonnet-4-5", "capabilities": {"vision": True}}]}],
    "prompt": {"profile": "default", "variables": {}},
    "tool": {"allow": [], "deny": [], "max_rounds": 0},
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
    "stt": {"model": "mlx-community/whisper-small-mlx"},
    "spawn": {"max_concurrency": 20, "max_history": 50},
    "im_bridge": {
        "default_workspace": None,
        "allowed_senders": [],
        "channels": {},
        "routes": {},
    },
    "agents": {
        "explore": {
            "description": "Read-only search agent for broad fan-out searches; returns conclusions, not file dumps.",
            "instructions": (
                "You are a read-only exploration agent for broad fan-out searches — sweeping many "
                "files, directories, or naming conventions and reporting only the conclusion, not "
                "file dumps. You read excerpts to locate code (files, functions, patterns), not to "
                "review or audit it. Use the read-only tools (read_file, grep, list, glob). Do not "
                "write code or make changes. Be concise and structured; calibrate search breadth to "
                "the request (a quick targeted look vs. a very thorough sweep)."
            ),
            "tools": ["read_file", "grep", "list", "glob"],
            "background": False,
        },
        "plan": {
            "description": "Software architect agent for designing implementation plans.",
            "instructions": (
                "You are a software architect agent for designing implementation plans. Analyze the "
                "request and the codebase, then return a step-by-step plan that identifies the "
                "critical files to change, the build sequence, and the architectural trade-offs. "
                "Use the read-only tools (read_file, grep, list, glob). Do not implement the changes."
            ),
            "tools": ["read_file", "grep", "list", "glob"],
            "background": False,
        },
        "general-purpose": {
            "description": "General-purpose agent for researching complex questions, searching for code, and executing multi-step tasks.",
            "instructions": (
                "You are a general-purpose agent for researching complex questions, searching for "
                "code, and executing multi-step tasks. Use this kind of agent when a search may "
                "take several tries to find the right match. You have access to all tools — read, "
                "write, and edit code, run shell commands — except spawning further sub-agents. "
                "Work autonomously and report what you did."
            ),
            "tools": [
                "read_file", "write_file", "edit_file",
                "grep", "glob", "list",
                "shell", "web_fetch", "read_skill",
            ],
            "background": False,
        },
    },
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
    if "max_tokens" in model and (not isinstance(model["max_tokens"], int) or model["max_tokens"] <= 0):
        raise ValueError("model.max_tokens must be a positive integer")
    if model.get("thinking_mode", "disabled") not in ("disabled", "low", "medium", "high"):
        raise ValueError("model.thinking_mode must be one of: disabled, low, medium, high")
    if "thinking_budget_tokens" in model and (not isinstance(model["thinking_budget_tokens"], int) or model["thinking_budget_tokens"] <= 0):
        raise ValueError("model.thinking_budget_tokens must be a positive integer")
    if model.get("thinking_mode", "disabled") != "disabled":
        budget = int(model.get("thinking_budget_tokens", 4000))
        max_t = int(model.get("max_tokens", 16384))
        if budget >= max_t:
            raise ValueError(
                f"model.thinking_budget_tokens ({budget}) must be < model.max_tokens ({max_t}) "
                f"(Anthropic requires budget_tokens < max_tokens when thinking is enabled)"
            )

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
            if "capabilities" in p:
                if not isinstance(p["capabilities"], dict):
                    raise ValueError(f"providers[{i}].capabilities must be an object")
                for k in p["capabilities"]:
                    if k not in ("thinking", "vision", "tools"):
                        raise ValueError(f"providers[{i}].capabilities has unknown key '{k}'")
            for m in p.get("models", []) or []:
                if not isinstance(m, dict):
                    raise ValueError(f"providers[{i}].models[] must be an object")
                if "capabilities" in m and not isinstance(m["capabilities"], dict):
                    raise ValueError(f"providers[{i}].models[].capabilities must be an object")

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

    spawn = config.get("spawn", {})
    if spawn:
        if not isinstance(spawn, dict):
            raise ValueError("spawn must be an object")
        if "max_concurrency" in spawn and (not isinstance(spawn["max_concurrency"], int) or spawn["max_concurrency"] <= 0):
            raise ValueError("spawn.max_concurrency must be a positive integer")
        if "max_history" in spawn and (not isinstance(spawn["max_history"], int) or spawn["max_history"] <= 0):
            raise ValueError("spawn.max_history must be a positive integer")

    agents = config.get("agents", {})
    if not isinstance(agents, dict):
        raise ValueError("agents must be an object")
    for name, definition in agents.items():
        if not isinstance(definition, dict):
            raise ValueError(f"agents.{name} must be an object")
        _expect_type(definition, "description", str, f"agents.{name}")
        _expect_type(definition, "instructions", str, f"agents.{name}")
        _expect_type(definition, "tools", list, f"agents.{name}")
        if "tools" in definition and not all(isinstance(t, str) for t in definition["tools"]):
            raise ValueError(f"agents.{name}.tools must be a list of strings")
        if "background" in definition and not isinstance(definition["background"], bool):
            raise ValueError(f"agents.{name}.background must be a boolean")
        # Skills whitelist (sub-agent only sees these). Optional.
        _expect_type(definition, "skills", list, f"agents.{name}")
        if "skills" in definition and not all(isinstance(s, str) for s in definition["skills"]):
            raise ValueError(f"agents.{name}.skills must be a list of strings")
        # Hook types this sub-agent triggers. Each value must be one
        # of the supported hook types the runtime recognises
        # (matches cfg.hooks keys). Empty list = inherit all hook
        # types from the parent.
        _expect_type(definition, "hooks", list, f"agents.{name}")
        if "hooks" in definition:
            valid_hook_types = {"before_turn", "after_turn", "before_tool", "after_tool"}
            for hk in definition["hooks"]:
                if not isinstance(hk, str):
                    raise ValueError(f"agents.{name}.hooks must be a list of strings")
                if hk not in valid_hook_types:
                    raise ValueError(
                        f"agents.{name}.hooks contains unknown type '{hk}'. "
                        f"Valid types: {sorted(valid_hook_types)}"
                    )


def load_effective_config(
    global_path: Path | None = None,
    session_override: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    """Load the global config (~/.ziva/config.yaml) and merge any session
    override on top. The runtime has a single source of truth — the global
    config file under the user's home directory. Workspace-local configs
    are intentionally not consulted.
    """
    config = dict(DEFAULT_CONFIG)
    if global_path:
        config = _deep_merge(config, _load_yaml(global_path))
    if session_override:
        config = _deep_merge(config, session_override)
    validate_config(config)
    return config
