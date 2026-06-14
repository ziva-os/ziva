"""Tests for the module-level adapter registry.

Covers Area 1 of the provider refactor:
- same (api_type, base_url, api_key) returns the same instance
- different keys return different instances
- unknown model raises ValueError instead of silent fallback
- capabilities are resolved from provider + model config
"""
from __future__ import annotations

import pytest

from ziva_runtime.runtime import (
    _create_adapter,
    _reset_adapter_registry,
    _resolve_capabilities,
)


@pytest.fixture(autouse=True)
def _clean_registry():
    _reset_adapter_registry()
    yield
    _reset_adapter_registry()


def _config(model_name: str = "gpt-4", **provider_kwargs) -> dict:
    provider = {
        "name": "Test",
        "api_type": "openai_compatible",
        "base_url": "http://localhost",
        "api_key": "k",
        "models": [{"name": model_name}],
    }
    provider.update(provider_kwargs)
    return {
        "model": {"name": model_name, "max_tokens": 8192},
        "providers": [provider],
    }


def test_singleton_returns_same_instance_for_same_key():
    cfg = _config()
    a1 = _create_adapter(cfg)
    a2 = _create_adapter(cfg)
    assert a1 is a2


def test_different_api_key_yields_different_instance():
    a1 = _create_adapter(_config())
    cfg2 = _config()
    cfg2["providers"][0]["api_key"] = "different"
    a2 = _create_adapter(cfg2)
    assert a1 is not a2


def test_different_base_url_yields_different_instance():
    a1 = _create_adapter(_config())
    cfg2 = _config()
    cfg2["providers"][0]["base_url"] = "http://other"
    a2 = _create_adapter(cfg2)
    assert a1 is not a2


def test_unknown_model_raises_value_error():
    cfg = _config()
    # Point model.name at something no provider declares
    cfg["model"]["name"] = "mystery-model"
    with pytest.raises(ValueError, match="not listed in any provider"):
        _create_adapter(cfg)


def test_anthropic_api_type_routes_to_anthropic_adapter():
    cfg = _config()
    cfg["providers"][0]["api_type"] = "anthropic"
    adapter = _create_adapter(cfg)
    from ziva_runtime.adapters.anthropic.provider import AnthropicChatAdapter
    assert isinstance(adapter, AnthropicChatAdapter)


def test_anthropic_adapter_receives_default_max_tokens():
    cfg = _config()
    cfg["providers"][0]["api_type"] = "anthropic"
    cfg["model"]["max_tokens"] = 12345
    adapter = _create_adapter(cfg)
    assert adapter._default_max_tokens == 12345


def test_capabilities_merge_provider_and_model_levels():
    provider_cfg = {
        "capabilities": {"thinking": True, "vision": False},
        "models": [
            {"name": "m1", "capabilities": {"vision": True}},
            {"name": "m2"},
        ],
    }
    # Model-level overrides provider-level
    assert _resolve_capabilities(provider_cfg, "m1") == {"thinking": True, "vision": True}
    # Model without capabilities inherits provider-level
    assert _resolve_capabilities(provider_cfg, "m2") == {"thinking": True, "vision": False}


def test_capabilities_propagate_to_openai_adapter():
    cfg = _config()
    cfg["providers"][0]["capabilities"] = {"thinking": True, "vision": True}
    cfg["providers"][0]["models"] = [{"name": "gpt-4", "capabilities": {"vision": False}}]
    adapter = _create_adapter(cfg)
    assert adapter._capabilities == {"thinking": True, "vision": False}
