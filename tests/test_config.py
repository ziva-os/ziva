from pathlib import Path

from ziva_runtime.config.loader import load_effective_config


def test_config_merge_and_validate(tmp_path: Path):
    global_cfg = tmp_path / "global.yaml"
    workspace_cfg = tmp_path / "workspace.yaml"
    global_cfg.write_text("model:\n  provider: openai_agents\n  name: gpt-4.1\n", encoding="utf-8")
    workspace_cfg.write_text("prompt:\n  profile: custom\n", encoding="utf-8")

    cfg = load_effective_config(global_cfg, workspace_cfg, {"memory": {"backend": "sqlite"}})
    assert cfg["model"]["name"] == "gpt-4.1"
    assert cfg["prompt"]["profile"] == "custom"
    assert cfg["memory"]["backend"] == "sqlite"
