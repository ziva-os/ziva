from pathlib import Path

import pytest

from ziva_runtime.config.loader import load_effective_config


def test_max_rounds_zero_means_unlimited(tmp_path: Path):
    cfg = tmp_path / "cfg.yaml"
    cfg.write_text("tool:\n  max_rounds: 0\n", encoding="utf-8")
    loaded = load_effective_config(cfg)
    assert loaded["tool"]["max_rounds"] == 0


def test_invalid_max_rounds_rejected(tmp_path: Path):
    cfg = tmp_path / "cfg.yaml"
    cfg.write_text("tool:\n  max_rounds: -1\n", encoding="utf-8")
    with pytest.raises(ValueError, match="tool.max_rounds"):
        load_effective_config(cfg)


def test_invalid_plugin_paths_rejected(tmp_path: Path):
    cfg = tmp_path / "cfg.yaml"
    cfg.write_text("plugin:\n  paths: not-a-list\n", encoding="utf-8")
    with pytest.raises(ValueError, match="plugin.paths"):
        load_effective_config(cfg)


def test_agents_hooks_field_accepted(tmp_path: Path):
    """agents.<name>.hooks (list[str]) is accepted when all entries are
    valid hook types matching the runtime's 4 supported types."""
    cfg = tmp_path / "cfg.yaml"
    cfg.write_text(
        "agents:\n"
        "  explore:\n"
        "    instructions: hi\n"
        "    tools: [read_file]\n"
        "    hooks: [before_turn, after_tool]\n",
        encoding="utf-8",
    )
    loaded = load_effective_config(cfg)
    assert loaded["agents"]["explore"]["hooks"] == ["before_turn", "after_tool"]


def test_agents_hooks_invalid_type_rejected(tmp_path: Path):
    """Unknown hook types in agents.<name>.hooks fail validation with a
    clear error message mentioning the valid set."""
    cfg = tmp_path / "cfg.yaml"
    cfg.write_text(
        "agents:\n"
        "  bad:\n"
        "    instructions: hi\n"
        "    tools: [read_file]\n"
        "    hooks: [before_turn, around_noon]\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="hooks contains unknown type 'around_noon'"):
        load_effective_config(cfg)


def test_agents_hooks_must_be_list_of_strings(tmp_path: Path):
    """Non-string entries in agents.<name>.hooks fail validation."""
    cfg = tmp_path / "cfg.yaml"
    cfg.write_text(
        "agents:\n"
        "  bad:\n"
        "    instructions: hi\n"
        "    tools: [read_file]\n"
        "    hooks: [42]\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="hooks must be a list of strings"):
        load_effective_config(cfg)
