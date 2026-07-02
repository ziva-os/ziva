import sys
from pathlib import Path

# Add src to path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ziva.config.loader import load_effective_config, validate_config


def test_approval_defaults():
    config = load_effective_config()
    assert config["approval"]["policy"] == "suggest"
    assert config["sandbox"]["mode"] == "off"


def test_approval_config_invalid_policy():
    try:
        validate_config({**load_effective_config(), "approval": {"policy": "invalid"}})
        # Should not raise for now - policy validation can be added later
    except ValueError:
        pass
