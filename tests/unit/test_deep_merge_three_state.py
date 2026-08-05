"""Regression tests for the three-state permission fields in agent config.

The "inherit / allow / deny" toggle must round-trip cleanly: when a user
switches an agent from "allow specific" to "inherit all", the stale
``tools`` / ``skills`` / ``hooks`` list must not silently reappear on the
next read. Same for switching between "allow" and "deny".

The merge logic lives in :func:`ziva.config.loader._deep_merge`. A
``None`` value in the overlay is the UI's signal to delete that key.
"""
from __future__ import annotations

from ziva.config.loader import _deep_merge, _strip_none_values


def _disk_agent() -> dict:
    return {
        "agents": {
            "coder": {
                "instructions": "You are a coder",
                "background": False,
                "tools": ["shell", "write_file"],
                "skills": ["search_code"],
                "hooks": ["hook.file_guard"],
            }
        }
    }


def test_inherit_mode_strips_allow_keys():
    """Switching to 'inherit all' deletes allow-lists from disk."""
    overlay = {
        "agents": {
            "coder": {
                "instructions": "You are a coder",
                "background": False,
                "tools": None,
                "skills": None,
                "hooks": None,
            }
        }
    }
    merged = _deep_merge(_disk_agent(), overlay)["agents"]["coder"]
    assert "tools" not in merged
    assert "skills" not in merged
    assert "hooks" not in merged


def test_inherit_mode_strips_deny_keys():
    """If disk has deny_tools, switching to inherit must clear it too."""
    disk = {"agents": {"coder": {"instructions": "x", "deny_tools": ["shell"]}}}
    overlay = {
        "agents": {
            "coder": {
                "instructions": "x",
                "tools": None,
                "deny_tools": None,
            }
        }
    }
    merged = _deep_merge(disk, overlay)["agents"]["coder"]
    assert "tools" not in merged
    assert "deny_tools" not in merged


def test_allow_mode_strips_deny_keys():
    """Switching to 'allow specific' must wipe any prior deny list."""
    disk = {"agents": {"coder": {"instructions": "x", "deny_tools": ["shell"]}}}
    overlay = {
        "agents": {
            "coder": {
                "instructions": "x",
                "tools": ["shell"],
                "deny_tools": None,
            }
        }
    }
    merged = _deep_merge(disk, overlay)["agents"]["coder"]
    assert merged["tools"] == ["shell"]
    assert "deny_tools" not in merged


def test_deny_mode_strips_allow_keys():
    """Switching to 'deny specific' must wipe any prior allow list."""
    disk = {"agents": {"coder": {"instructions": "x", "tools": ["shell"]}}}
    overlay = {
        "agents": {
            "coder": {
                "instructions": "x",
                "tools": None,
                "deny_tools": ["shell"],
            }
        }
    }
    merged = _deep_merge(disk, overlay)["agents"]["coder"]
    assert merged["deny_tools"] == ["shell"]
    assert "tools" not in merged


def test_null_overlay_does_not_introduce_key():
    """A ``None`` overlay value must never create a key on its own."""
    disk = {"agents": {"coder": {"instructions": "x"}}}
    overlay = {
        "agents": {
            "coder": {
                "instructions": "x",
                "tools": None,
                "deny_tools": None,
            }
        }
    }
    merged = _deep_merge(disk, overlay)["agents"]["coder"]
    assert "tools" not in merged
    assert "deny_tools" not in merged


def test_non_null_overlay_still_merges_recursively():
    """Other config sections are unaffected by the None-as-delete rule."""
    disk = {"agents": {"coder": {"instructions": "old"}}}
    overlay = {"agents": {"coder": {"instructions": "new"}}}
    merged = _deep_merge(disk, overlay)
    assert merged["agents"]["coder"]["instructions"] == "new"


def test_strip_none_removes_orphan_nulls_in_fresh_agent_block():
    """_deep_merge only recurses when base already has the key — a
    brand-new agent block in the overlay therefore leaves literal
    ``tools: null`` entries behind. The save handler must scrub those
    before writing the YAML so the file doesn't accumulate noise.
    """
    overlay = {
        "agents": {
            "new_agent": {
                "instructions": "x",
                "tools": None,
                "skills": None,
                "hooks": None,
                "deny_tools": None,
            }
        }
    }
    cleaned = _strip_none_values(_deep_merge({}, overlay))
    assert "tools" not in cleaned["agents"]["new_agent"]
    assert "skills" not in cleaned["agents"]["new_agent"]
    assert "hooks" not in cleaned["agents"]["new_agent"]
    assert "deny_tools" not in cleaned["agents"]["new_agent"]


def test_strip_none_preserves_empty_lists():
    """An empty allow-list ``tools: []`` is semantically distinct from
    "absent" (it means "sub-agent gets zero tools"). The scrubber must
    NOT collapse those — only literal ``None`` values are noise.
    """
    overlay = {
        "agents": {
            "locked_down": {
                "instructions": "x",
                "tools": [],
            }
        }
    }
    cleaned = _strip_none_values(_deep_merge({}, overlay))
    # Note: the UI save handler also converts ``allow with no picks``
    # to inherit before this ever runs, but the loader-level invariant
    # is: empty list survives, None does not.
    assert cleaned["agents"]["locked_down"]["tools"] == []


def test_strip_none_is_recursive():
    """None values buried inside nested lists/dicts are also dropped."""
    payload = {
        "a": 1,
        "b": None,
        "c": {"d": None, "e": "kept", "f": [None, 1, None, "x"]},
    }
    cleaned = _strip_none_values(payload)
    assert cleaned == {"a": 1, "c": {"e": "kept", "f": [1, "x"]}}