from pathlib import Path

import pytest

from ziva_runtime.config.loader import load_effective_config


def test_invalid_max_rounds_rejected(tmp_path: Path):
    cfg = tmp_path / "cfg.yaml"
    cfg.write_text("tool:\n  max_rounds: 0\n", encoding="utf-8")
    with pytest.raises(ValueError, match="tool.max_rounds"):
        load_effective_config(workspace_path=cfg)


def test_invalid_plugin_paths_rejected(tmp_path: Path):
    cfg = tmp_path / "cfg.yaml"
    cfg.write_text("plugin:\n  paths: not-a-list\n", encoding="utf-8")
    with pytest.raises(ValueError, match="plugin.paths"):
        load_effective_config(workspace_path=cfg)
