from pathlib import Path

from ziva.config.loader import load_effective_config


def test_config_merge_and_validate(tmp_path: Path):
    global_cfg = tmp_path / "global.yaml"
    global_cfg.write_text("model:\n  provider: openai\n  name: gpt-4.1\n", encoding="utf-8")

    cfg = load_effective_config(global_cfg, {"memory": {"backend": "sqlite"}})
    assert cfg["model"]["name"] == "gpt-4.1"
    assert cfg["memory"]["backend"] == "sqlite"
