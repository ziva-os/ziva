"""Tests for the new model.* and spawn.* config fields (Area 4)."""
from __future__ import annotations

import pytest

from ziva_runtime.config.loader import DEFAULT_CONFIG, validate_config


def _base() -> dict:
    import copy
    return copy.deepcopy(DEFAULT_CONFIG)


def test_default_config_has_model_max_tokens():
    assert DEFAULT_CONFIG["model"]["max_tokens"] == 16384


def test_default_config_has_thinking_fields():
    assert DEFAULT_CONFIG["model"]["thinking_mode"] == "disabled"
    assert DEFAULT_CONFIG["model"]["thinking_budget_tokens"] == 4000


def test_default_config_has_spawn_section():
    assert DEFAULT_CONFIG["spawn"]["max_concurrency"] == 20
    assert DEFAULT_CONFIG["spawn"]["max_history"] == 50


def test_validator_rejects_zero_max_tokens():
    cfg = _base()
    cfg["model"]["max_tokens"] = 0
    with pytest.raises(ValueError, match="max_tokens must be a positive integer"):
        validate_config(cfg)


def test_validator_rejects_invalid_thinking_mode():
    cfg = _base()
    cfg["model"]["thinking_mode"] = "extreme"
    with pytest.raises(ValueError, match="thinking_mode must be one of"):
        validate_config(cfg)


def test_validator_rejects_budget_equal_to_max_tokens():
    cfg = _base()
    cfg["model"]["max_tokens"] = 4000
    cfg["model"]["thinking_mode"] = "high"
    cfg["model"]["thinking_budget_tokens"] = 4000
    with pytest.raises(ValueError, match="must be < model.max_tokens"):
        validate_config(cfg)


def test_validator_allows_budget_above_max_when_thinking_disabled():
    cfg = _base()
    cfg["model"]["max_tokens"] = 1000
    cfg["model"]["thinking_mode"] = "disabled"
    cfg["model"]["thinking_budget_tokens"] = 99999  # ignored
    validate_config(cfg)  # should not raise


def test_validator_rejects_zero_spawn_concurrency():
    cfg = _base()
    cfg["spawn"] = {"max_concurrency": 0, "max_history": 50}
    with pytest.raises(ValueError, match="spawn.max_concurrency must be a positive integer"):
        validate_config(cfg)


def test_validator_rejects_unknown_provider_capability():
    cfg = _base()
    cfg["providers"] = [{
        "name": "X",
        "api_type": "openai_compatible",
        "api_key": "",
        "base_url": "",
        "models": [{"name": "m"}],
        "capabilities": {"bogus_capability": True},
    }]
    with pytest.raises(ValueError, match="unknown key 'bogus_capability'"):
        validate_config(cfg)


def test_validator_accepts_known_provider_capabilities():
    cfg = _base()
    cfg["providers"] = [{
        "name": "X",
        "api_type": "openai_compatible",
        "api_key": "",
        "base_url": "",
        "models": [{"name": "m"}],
        "capabilities": {"thinking": True, "vision": False, "tools": True},
    }]
    validate_config(cfg)  # should not raise


def test_validator_accepts_model_level_capabilities():
    cfg = _base()
    cfg["providers"] = [{
        "name": "X",
        "api_type": "openai_compatible",
        "api_key": "",
        "base_url": "",
        "models": [{"name": "m", "capabilities": {"thinking": True}}],
    }]
    validate_config(cfg)
