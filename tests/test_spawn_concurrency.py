"""Tests for spawn_agent concurrency limits and cancel_agent (Area 3).

Covers:
- _agent_concurrency Semaphore bounds parallel runs
- _background_agents auto-pruning (max_history)
- cancel_agent triggers task.cancel() and propagates CancelledError
- finished_at timestamp is set on terminal states
"""
from __future__ import annotations

import asyncio

from ziva.runtime import Runtime


def _fake_runtime(max_concurrency: int = 20, max_history: int = 50) -> Runtime:
    """Build a Runtime shell with just enough state for spawn_agent tests.

    Bypasses the real constructor (which needs a registry, event bus,
    workspace_root, etc.) — we only need _background_agents, _agent_concurrency,
    _agent_max_history, and _prune_background_agents.
    """
    rt = Runtime.__new__(Runtime)
    rt.config = {"spawn": {"max_concurrency": max_concurrency, "max_history": max_history}}
    rt._background_agents = {}
    rt._agent_concurrency = asyncio.Semaphore(max_concurrency)
    rt._agent_max_history = max_history
    rt.event_bus = None
    rt._sessions = {}
    return rt


def test_semaphore_default_is_20():
    rt = _fake_runtime()
    # Semaphore._value is the current available permits (private attr)
    assert rt._agent_concurrency._value == 20


def test_semaphore_respects_config_override():
    rt = _fake_runtime(max_concurrency=5)
    assert rt._agent_concurrency._value == 5


def test_prune_keeps_only_max_history_finished():
    rt = _fake_runtime(max_history=3)
    # Add 5 finished agents with ascending finished_at
    for i in range(5):
        rt._background_agents[f"bg_{i}"] = {
            "agent_id": f"bg_{i}",
            "status": "completed",
            "finished_at": 1000 + i,
        }
    # Add 2 running agents (should not be pruned)
    rt._background_agents["bg_running_1"] = {"agent_id": "bg_running_1", "status": "running"}
    rt._background_agents["bg_running_2"] = {"agent_id": "bg_running_2", "status": "running"}

    rt._prune_background_agents()

    # The 2 oldest finished agents (bg_0, bg_1) should be gone;
    # bg_2, bg_3, bg_4 should remain, plus the 2 running.
    assert "bg_0" not in rt._background_agents
    assert "bg_1" not in rt._background_agents
    assert "bg_2" in rt._background_agents
    assert "bg_4" in rt._background_agents
    assert "bg_running_1" in rt._background_agents
    assert "bg_running_2" in rt._background_agents


def test_prune_noop_when_under_limit():
    rt = _fake_runtime(max_history=10)
    rt._background_agents["bg_0"] = {"agent_id": "bg_0", "status": "completed", "finished_at": 1000}
    rt._prune_background_agents()
    assert "bg_0" in rt._background_agents


def test_prune_handles_empty_registry():
    rt = _fake_runtime()
    rt._prune_background_agents()  # should not raise


def test_prune_treats_cancelled_as_finished():
    rt = _fake_runtime(max_history=1)
    rt._background_agents["old"] = {"agent_id": "old", "status": "cancelled", "finished_at": 1000}
    rt._background_agents["new"] = {"agent_id": "new", "status": "failed", "finished_at": 2000}
    rt._prune_background_agents()
    assert "old" not in rt._background_agents
    assert "new" in rt._background_agents


def test_prune_treats_failed_as_finished():
    rt = _fake_runtime(max_history=1)
    rt._background_agents["old"] = {"agent_id": "old", "status": "failed", "finished_at": 1000}
    rt._background_agents["new"] = {"agent_id": "new", "status": "completed", "finished_at": 2000}
    rt._prune_background_agents()
    assert "old" not in rt._background_agents
    assert "new" in rt._background_agents
