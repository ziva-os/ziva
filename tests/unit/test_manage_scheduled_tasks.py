"""Tests for `manage_scheduled_tasks` and the discriminated-union
schedule schema.

This file replaces the 1.0.4 tests. The 1.0.4 bug ("schedule_time-only
tasks fire every 5 minutes because the loader ignored schedule_time and
fell back to the default 300s interval") was fixed twice over:

  1. The loader was rewritten to route through the unified
     `ziva.scheduled.compute_next_run` helper.
  2. The schema itself was upgraded to a discriminated union
     (`schedule.kind: every | daily | weekly`) so the model can no longer
     express the ambiguous interval-vs-daily combination that the
     1.0.4 fix was patching around.

The tests in this file cover the new schema, the shared scheduling
helpers, the tool's six actions, and the HTTP server's
backward-compatible surface.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import time
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Plugin loader — import by file path so the test doesn't require the
# workspace to be installed as a package.
# ---------------------------------------------------------------------------

PLUGIN_PATH = Path(__file__).resolve().parents[2] / "plugins" / "tools" / "manage_scheduled_tasks" / "impl.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("manage_scheduled_tasks_impl", PLUGIN_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_tool_class():
    return _load_module().ManageScheduledTasksTool


# ---------------------------------------------------------------------------
# Shared scheduling helpers (ziva.scheduled)
# ---------------------------------------------------------------------------


def _load_scheduled():
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
    from ziva import scheduled  # type: ignore

    return scheduled


# --- normalize_schedule ----------------------------------------------------


def test_normalize_every_minimal():
    s = _load_scheduled()
    out = s.normalize_schedule({"kind": "every", "interval_seconds": 300})
    assert out == {"kind": "every", "interval_seconds": 300}


def test_normalize_every_drops_unrelated_fields():
    """Extra fields for other kinds are dropped at normalization time.

    This is OpenClaw's "canonical form" pattern — the runtime re-derives
    the persisted shape from `kind` alone, so a daily→weekly update
    can't leave stale `time` / `tz` hanging around.
    """
    sched = _load_scheduled()
    raw = {
        "kind": "every",
        "interval_seconds": 600,
        "time": "09:00",
        "tz": "Asia/Shanghai",
        "days": ["MO"],
    }
    out = sched.normalize_schedule(raw)
    assert out == {"kind": "every", "interval_seconds": 600}


def test_normalize_daily_with_tz():
    sched = _load_scheduled()
    out = sched.normalize_schedule({"kind": "daily", "time": "21:00", "tz": "Asia/Shanghai"})
    assert out == {"kind": "daily", "time": "21:00", "tz": "Asia/Shanghai"}


def test_normalize_daily_without_tz():
    sched = _load_scheduled()
    out = sched.normalize_schedule({"kind": "daily", "time": "09:00"})
    assert out == {"kind": "daily", "time": "09:00"}


def test_normalize_weekly_weekdays():
    sched = _load_scheduled()
    out = sched.normalize_schedule(
        {"kind": "weekly", "days": ["MO", "TU", "WE", "TH", "FR"], "time": "09:00"}
    )
    assert out["days"] == ["MO", "TU", "WE", "TH", "FR"]
    assert out["time"] == "09:00"


def test_normalize_unknown_kind_rejected():
    sched = _load_scheduled()
    with pytest.raises(sched.ScheduleError, match="schedule.kind"):
        sched.normalize_schedule({"kind": "unknown"})


def test_normalize_every_missing_interval_rejected():
    sched = _load_scheduled()
    with pytest.raises(sched.ScheduleError, match="interval_seconds"):
        sched.normalize_schedule({"kind": "every"})


def test_normalize_every_zero_interval_rejected():
    sched = _load_scheduled()
    with pytest.raises(sched.ScheduleError, match="interval_seconds"):
        sched.normalize_schedule({"kind": "every", "interval_seconds": 0})


def test_normalize_every_interval_too_large_rejected():
    sched = _load_scheduled()
    with pytest.raises(sched.ScheduleError, match="30 days"):
        sched.normalize_schedule({"kind": "every", "interval_seconds": 31 * 86400})


def test_normalize_daily_bad_time_rejected():
    sched = _load_scheduled()
    with pytest.raises(sched.ScheduleError, match="HH:MM"):
        sched.normalize_schedule({"kind": "daily", "time": "25:00"})
    with pytest.raises(sched.ScheduleError, match="HH:MM"):
        sched.normalize_schedule({"kind": "daily", "time": "9am"})


def test_normalize_weekly_invalid_day_code_rejected():
    sched = _load_scheduled()
    with pytest.raises(sched.ScheduleError, match="weekday"):
        sched.normalize_schedule({"kind": "weekly", "days": ["Mon"], "time": "09:00"})


def test_normalize_weekly_duplicate_days_rejected():
    sched = _load_scheduled()
    with pytest.raises(sched.ScheduleError, match="duplicate"):
        sched.normalize_schedule({"kind": "weekly", "days": ["MO", "MO"], "time": "09:00"})


def test_normalize_weekly_empty_days_rejected():
    sched = _load_scheduled()
    with pytest.raises(sched.ScheduleError, match="days"):
        sched.normalize_schedule({"kind": "weekly", "days": [], "time": "09:00"})


def test_normalize_bad_tz_rejected():
    sched = _load_scheduled()
    with pytest.raises(sched.ScheduleError, match="IANA"):
        sched.normalize_schedule(
            {"kind": "daily", "time": "09:00", "tz": "Not/A/Zone"}
        )


# --- (none) -----------------------------------------------------------------


# --- compute_next_run -----------------------------------------------------


def test_next_run_every_drift_free():
    """OpenClaw's anchor pattern: subsequent runs align to a grid
    instead of sliding forward on every miss."""
    sched = _load_scheduled()
    anchor = 1_700_000_000.0
    schedule = {"kind": "every", "interval_seconds": 60, "anchor_at": anchor}
    # 90 s later: elapsed=90, steps=2, next = anchor + 120
    assert sched.compute_next_run(schedule, anchor + 90) == anchor + 120
    # exactly on grid: steps=1, next = anchor + 60 (the next slot past "now")
    assert sched.compute_next_run(schedule, anchor + 60) == anchor + 120
    # just past the grid line: still steps=2
    assert sched.compute_next_run(schedule, anchor + 60.001) == anchor + 120
    # far in the future: aligned to grid (next slot past "now")
    # `+1` in compute_next_run returns the slot strictly after now, so 60s further.
    assert sched.compute_next_run(schedule, anchor + 3600) == anchor + 3600 + 60


def test_next_run_daily_basic():
    sched = _load_scheduled()
    schedule = {"kind": "daily", "time": "21:00"}
    # Pick a reference time well past 21:00 local — next run must be tomorrow 21:00.
    ref = 1_730_000_000  # epoch; just need any well-formed value
    # We can't easily predict the exact local second without inspecting tz;
    # assert the result is > ref and well-aligned (within ±24h + buffer).
    nxt = sched.compute_next_run(schedule, ref)
    assert nxt is not None
    assert nxt > ref
    assert nxt - ref < 86400 + 60  # within ~24h of now


def test_next_run_weekly_picks_nearest_matching_day():
    sched = _load_scheduled()
    schedule = {"kind": "weekly", "days": ["MO"], "time": "09:00"}
    # Pick a Sunday afternoon — Monday 09:00 must be the answer.
    ref = time.mktime(time.struct_time((2030, 1, 6, 15, 0, 0, 6, 6, -1)))  # Sun Jan 6 2030 15:00
    nxt = sched.compute_next_run(schedule, ref)
    assert nxt is not None
    local = time.localtime(nxt)
    assert local.tm_wday == 0  # Monday
    assert local.tm_hour == 9
    assert local.tm_min == 0


def test_next_run_invalid_schedule_returns_none():
    sched = _load_scheduled()
    assert sched.compute_next_run({"kind": "every"}, 1_700_000_000.0) is None


def test_next_run_invalid_kind_returns_none():
    sched = _load_scheduled()
    assert sched.compute_next_run({"kind": "wat"}, 1_700_000_000.0) is None


# --- describe_schedule ----------------------------------------------------


def test_describe_known_kinds():
    sched = _load_scheduled()
    assert sched.describe_schedule({"kind": "every", "interval_seconds": 300}) == "every 5 minutes"
    assert sched.describe_schedule({"kind": "every", "interval_seconds": 7200}) == "every 2 hours"
    assert sched.describe_schedule({"kind": "every", "interval_seconds": 86400}) == "every 1 day"
    assert sched.describe_schedule({"kind": "daily", "time": "21:00"}) == "daily at 21:00"
    assert (
        sched.describe_schedule({"kind": "weekly", "days": ["MO", "TU", "WE", "TH", "FR"], "time": "09:00"})
        == "weekdays at 09:00"
    )
    assert (
        sched.describe_schedule(
            {"kind": "weekly", "days": ["MO", "TU", "WE", "TH", "FR"], "time": "09:00", "tz": "Asia/Shanghai"}
        )
        == "weekdays at 09:00 [Asia/Shanghai]"
    )


def test_describe_unconfigured():
    sched = _load_scheduled()
    assert sched.describe_schedule({}) == "unconfigured"


def test_describe_legacy_record_with_schedule_field():
    """describe_schedule accepts a full record (with `schedule` nested)."""
    sched = _load_scheduled()
    assert (
        sched.describe_schedule({"schedule": {"kind": "daily", "time": "21:00"}})
        == "daily at 21:00"
    )


# ---------------------------------------------------------------------------
# Tool: schema guarantees
# ---------------------------------------------------------------------------


def test_schema_actions_include_get_and_run():
    cls = _load_tool_class()
    spec = cls().spec()
    actions = spec["input_schema"]["properties"]["action"]["enum"]
    assert "list" in actions
    assert "create" in actions
    assert "update" in actions
    assert "delete" in actions
    assert "get" in actions
    assert "run" in actions


def test_schedule_field_is_required_on_create():
    cls = _load_tool_class()
    schema = cls().spec()["input_schema"]
    all_of = schema["allOf"]
    create_branch = next(b for b in all_of if b.get("if", {}).get("properties", {}).get("action", {}).get("const") == "create")
    assert "schedule" in create_branch["then"]["required"]
    assert "prompt" in create_branch["then"]["required"]


def test_task_id_required_for_get_update_delete_run():
    cls = _load_tool_class()
    schema = cls().spec()["input_schema"]
    all_of = schema["allOf"]
    id_branch = next(
        b
        for b in all_of
        if b.get("if", {}).get("properties", {}).get("action", {}).get("enum")
        == ["update", "delete", "get", "run"]
    )
    assert "task_id" in id_branch["then"]["required"]


def test_description_does_not_mention_legacy_default_300():
    """The 1.0.4 description no longer promises any default interval
    that could silently turn daily tasks into 5-minute loops."""
    cls = _load_tool_class()
    desc = cls().spec()["description"].lower()
    assert "default 300" not in desc
    # Must teach the model what the three kinds are.
    assert "every" in desc
    assert "daily" in desc
    assert "weekly" in desc


def test_description_mentions_timezone():
    cls = _load_tool_class()
    desc = cls().spec()["description"].lower()
    assert "tz" in desc or "timezone" in desc
    assert "iana" in desc.lower() or "asia/shanghai" in desc.lower()


def test_description_includes_examples():
    cls = _load_tool_class()
    desc = cls().spec()["description"]
    # At least one JSON example for each kind
    assert "every" in desc and "interval_seconds" in desc
    assert "daily" in desc and "time" in desc
    assert "weekly" in desc and "days" in desc


# ---------------------------------------------------------------------------
# Tool: end-to-end behaviour through the 6 actions
# ---------------------------------------------------------------------------


class _FakeStorage:
    """Minimal stand-in for FileStorage so the tool can be exercised
    without touching ~/.ziva.

    The real FileStorage exposes classmethods (``FileStorage.list_automations(project_id)``),
    so this fake mirrors that signature exactly. Tests should call
    ``_FakeStorage.reset()`` to start each with a clean slate.
    """

    records: dict[str, dict] = {}

    @classmethod
    def reset(cls) -> None:
        cls.records = {}

    @classmethod
    def list_automations(cls, project_id):
        return list(cls.records.values())

    @classmethod
    def upsert_automation(cls, project_id, payload):
        cls.records[payload["id"]] = dict(payload)

    @classmethod
    def delete_automation(cls, project_id, aid):
        return cls.records.pop(aid, None) is not None

    @classmethod
    def replace_automations(cls, project_id, automations):
        cls.records = {a["id"]: a for a in automations}


class _FakeRuntime:
    def __init__(self, project_id="proj-1") -> None:
        self.project_id = project_id
        self.automation_callback = lambda: None
        self.storage = _FakeStorage


@pytest.fixture(autouse=True)
def _reset_fake_storage():
    _FakeStorage.reset()
    yield
    _FakeStorage.reset()


def _make_ctx(runtime):
    from ziva.shared_types import RuntimeContext

    return RuntimeContext(
        session_id="sess-1",
        config={},
        metadata={"_runtime": runtime},
    )


def _patch_file_storage(monkeypatch, fake):
    """Make `from ziva.storage.file_storage import FileStorage` resolve to
    the in-memory fake in the impl module."""
    monkeypatch.setattr(fake, "storage", fake, raising=False)


def test_create_every_minimal(monkeypatch):
    fake = _FakeRuntime()
    module = _load_module()  # single load — both tool class and monkeypatch target the same module globals
    monkeypatch.setattr(module, "FileStorage", _FakeStorage)

    tool = module.ManageScheduledTasksTool()
    result = asyncio.run(
        tool.run(
            {
                "action": "create",
                "name": "PR monitor",
                "prompt": "check open PRs",
                "schedule": {"kind": "every", "interval_seconds": 300},
            },
            _make_ctx(fake),
        )
    )
    assert not result.error, result.text
    record = next(iter(_FakeStorage.records.values()))
    assert record["schedule"]["kind"] == "every"
    assert record["schedule"]["interval_seconds"] == 300
    # Anchor was set on create so the first run aligns to the grid.
    assert "anchor_at" in record["schedule"]
    assert "every 5 minutes" in result.text


def test_create_daily_with_tz(monkeypatch):
    fake = _FakeRuntime()
    module = _load_module()
    monkeypatch.setattr(module, "FileStorage", _FakeStorage)

    tool = module.ManageScheduledTasksTool()
    result = asyncio.run(
        tool.run(
            {
                "action": "create",
                "name": "morning",
                "prompt": "...",
                "schedule": {"kind": "daily", "time": "09:00", "tz": "Asia/Shanghai"},
            },
            _make_ctx(fake),
        )
    )
    assert not result.error, result.text
    record = next(iter(_FakeStorage.records.values()))
    assert record["schedule"] == {"kind": "daily", "time": "09:00", "tz": "Asia/Shanghai"}
    assert "daily at 09:00 [Asia/Shanghai]" in result.text


def test_create_weekly_weekdays(monkeypatch):
    fake = _FakeRuntime()
    module = _load_module()
    monkeypatch.setattr(module, "FileStorage", _FakeStorage)

    tool = module.ManageScheduledTasksTool()
    result = asyncio.run(
        tool.run(
            {
                "action": "create",
                "name": "weekdays",
                "prompt": "...",
                "schedule": {
                    "kind": "weekly",
                    "days": ["MO", "TU", "WE", "TH", "FR"],
                    "time": "09:00",
                },
            },
            _make_ctx(fake),
        )
    )
    assert not result.error, result.text
    record = next(iter(_FakeStorage.records.values()))
    assert record["schedule"]["kind"] == "weekly"
    assert record["schedule"]["days"] == ["MO", "TU", "WE", "TH", "FR"]
    assert "weekdays at 09:00" in result.text


def test_create_without_schedule_fails(monkeypatch):
    fake = _FakeRuntime()
    module = _load_module()
    monkeypatch.setattr(module, "FileStorage", _FakeStorage)

    tool = module.ManageScheduledTasksTool()
    result = asyncio.run(
        tool.run(
            {"action": "create", "prompt": "...", "name": "x"},
            _make_ctx(fake),
        )
    )
    assert result.error
    assert "schedule" in result.text


def test_create_with_invalid_schedule_fails(monkeypatch):
    fake = _FakeRuntime()
    module = _load_module()
    monkeypatch.setattr(module, "FileStorage", _FakeStorage)

    tool = module.ManageScheduledTasksTool()
    result = asyncio.run(
        tool.run(
            {
                "action": "create",
                "prompt": "...",
                "schedule": {"kind": "weekly", "days": ["Mon"], "time": "09:00"},
            },
            _make_ctx(fake),
        )
    )
    assert result.error
    assert "weekday" in result.text.lower() or "MO" in result.text


def test_get_returns_full_task_json(monkeypatch):
    fake = _FakeRuntime()
    module = _load_module()
    monkeypatch.setattr(module, "FileStorage", _FakeStorage)

    # Seed
    _FakeStorage.upsert_automation(
        "proj-1",
        {
            "id": "aid-1",
            "name": "n",
            "prompt": "p",
            "schedule": {"kind": "daily", "time": "21:00"},
            "enabled": True,
        },
    )

    tool = module.ManageScheduledTasksTool()
    result = asyncio.run(
        tool.run({"action": "get", "task_id": "aid-1"}, _make_ctx(fake))
    )
    assert not result.error, result.text
    parsed = json.loads(result.text)
    assert parsed["id"] == "aid-1"
    assert parsed["schedule"]["kind"] == "daily"


def test_get_unknown_task_id_fails(monkeypatch):
    fake = _FakeRuntime()
    module = _load_module()
    monkeypatch.setattr(module, "FileStorage", _FakeStorage)

    tool = module.ManageScheduledTasksTool()
    result = asyncio.run(
        tool.run({"action": "get", "task_id": "nope"}, _make_ctx(fake))
    )
    assert result.error
    assert "not found" in result.text


def test_update_can_change_kind(monkeypatch):
    """A single update call can flip between every / daily / weekly."""
    fake = _FakeRuntime()
    module = _load_module()
    monkeypatch.setattr(module, "FileStorage", _FakeStorage)

    _FakeStorage.upsert_automation(
        "proj-1",
        {
            "id": "aid-1",
            "name": "n",
            "prompt": "p",
            "schedule": {"kind": "every", "interval_seconds": 300},
            "enabled": True,
        },
    )

    tool = module.ManageScheduledTasksTool()
    result = asyncio.run(
        tool.run(
            {
                "action": "update",
                "task_id": "aid-1",
                "schedule": {"kind": "weekly", "days": ["MO", "FR"], "time": "10:30"},
            },
            _make_ctx(fake),
        )
    )
    assert not result.error, result.text
    record = _FakeStorage.records["aid-1"]
    assert record["schedule"]["kind"] == "weekly"
    assert record["schedule"]["days"] == ["MO", "FR"]


def test_update_enabled_false_persists(monkeypatch):
    fake = _FakeRuntime()
    module = _load_module()
    monkeypatch.setattr(module, "FileStorage", _FakeStorage)

    _FakeStorage.upsert_automation(
        "proj-1",
        {
            "id": "aid-1",
            "name": "n",
            "prompt": "p",
            "schedule": {"kind": "every", "interval_seconds": 300},
            "enabled": True,
        },
    )

    tool = module.ManageScheduledTasksTool()
    result = asyncio.run(
        tool.run(
            {"action": "update", "task_id": "aid-1", "enabled": False},
            _make_ctx(fake),
        )
    )
    assert not result.error, result.text
    assert _FakeStorage.records["aid-1"]["enabled"] is False


def test_delete_removes_task(monkeypatch):
    fake = _FakeRuntime()
    module = _load_module()
    monkeypatch.setattr(module, "FileStorage", _FakeStorage)

    _FakeStorage.upsert_automation(
        "proj-1",
        {
            "id": "aid-1",
            "name": "n",
            "prompt": "p",
            "schedule": {"kind": "every", "interval_seconds": 300},
            "enabled": True,
        },
    )

    tool = module.ManageScheduledTasksTool()
    result = asyncio.run(
        tool.run({"action": "delete", "task_id": "aid-1"}, _make_ctx(fake))
    )
    assert not result.error, result.text
    assert "aid-1" not in _FakeStorage.records


def test_list_human_readable_includes_kind(monkeypatch):
    fake = _FakeRuntime()
    module = _load_module()
    monkeypatch.setattr(module, "FileStorage", _FakeStorage)

    _FakeStorage.upsert_automation(
        "proj-1",
        {
            "id": "aid-1",
            "name": "n",
            "prompt": "p",
            "schedule": {"kind": "weekly", "days": ["MO"], "time": "09:00"},
            "enabled": True,
        },
    )

    tool = module.ManageScheduledTasksTool()
    result = asyncio.run(tool.run({"action": "list"}, _make_ctx(fake)))
    assert not result.error, result.text
    assert "aid-1" in result.text
    assert "weekly" in result.text.lower() or "MO" in result.text


# ---------------------------------------------------------------------------
# DesktopAPIServer: Automation dataclass + migration
# ---------------------------------------------------------------------------


def _build_server():
    """Build just enough of DesktopAPIServer to exercise Automation in isolation."""
    from ziva.transports.desktop_api.server import Automation, DesktopAPIServer

    class _StubRuntime:
        workspace_root = Path("/tmp")

    server = DesktopAPIServer.__new__(DesktopAPIServer)
    server.runtime = _StubRuntime()
    return server, Automation


def test_automation_rejects_record_without_schedule():
    """from_dict requires a `schedule` object — no fallback to flat fields."""
    from ziva.scheduled import ScheduleError

    _, Automation = _build_server()
    with pytest.raises(ScheduleError):
        Automation.from_dict(
            {
                "id": "x",
                "name": "t",
                "prompt": "p",
                "interval_seconds": 300,
                "schedule_time": "09:00:00",
            }
        )


def test_automation_drops_unrelated_keys_in_schedule_field():
    """normalize_schedule strips fields that don't belong to the declared kind."""
    _, Automation = _build_server()
    a = Automation.from_dict(
        {
            "id": "y",
            "name": "t",
            "prompt": "p",
            "schedule": {
                "kind": "weekly",
                "days": ["MO", "TU"],
                "time": "09:00",
                "interval_seconds": 300,  # not a weekly field — dropped
            },
        }
    )
    assert a.schedule == {"kind": "weekly", "days": ["MO", "TU"], "time": "09:00"}


def test_automation_rejects_invalid_schedule_strictly():
    """1.0.5+: from_dict raises on invalid schedule — the loader is
    responsible for dropping these records (see _load_persisted_automations)."""
    from ziva.scheduled import ScheduleError

    _, Automation = _build_server()
    with pytest.raises(ScheduleError):
        Automation.from_dict(
            {
                "id": "z",
                "name": "t",
                "prompt": "p",
                "schedule": {"kind": "weekly", "days": ["Mon"], "time": "25:00"},
            }
        )


def test_automation_to_dict_only_exposes_new_schema():
    """to_dict() never echoes flat fields back — schedule is the only contract."""
    _, Automation = _build_server()
    a = Automation(
        id="x",
        name="t",
        prompt="p",
        schedule={"kind": "every", "interval_seconds": 300, "anchor_at": 1.0},
    )
    d = a.to_dict()
    assert "interval_seconds" not in d
    assert "schedule_time" not in d
    assert "schedule" in d


def test_server_next_run_timestamp_uses_canonical_helper():
    from ziva.scheduled import compute_next_run

    server, _ = _build_server()
    # Pass a real schedule; should delegate to compute_next_run.
    schedule = {"kind": "daily", "time": "21:00"}
    direct = compute_next_run(schedule, time.time())
    via_server = server._next_run_timestamp(schedule)
    assert via_server == direct
    assert via_server is not None


def test_server_next_run_timestamp_returns_none_for_invalid():
    server, _ = _build_server()
    # Invalid schedule → compute_next_run returns None → wrapper also returns None.
    assert server._next_run_timestamp({"kind": "wat"}) is None


# ---------------------------------------------------------------------------
# DesktopAPIServer: HTTP handlers via synthetic request objects
# ---------------------------------------------------------------------------


class _FakeRequest:
    """Stand-in for aiohttp.web.Request exposing only the methods our handlers use."""

    def __init__(self, body=None, match_info=None):
        self._body = body
        self.match_info = match_info or {}
        self.body_exists = body is not None

    async def json(self):
        return self._body or {}


def _build_full_server(tmp_path):
    """Construct a real DesktopAPIServer pointed at a temp storage root."""
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
    from ziva.storage import file_storage as fs_module
    from ziva.transports.desktop_api.server import DesktopAPIServer

    fs_module.set_base_dir(tmp_path)

    class _StubRuntime:
        workspace_root = Path(tmp_path)
        project_id = "test-project"
        config = {}

    rt = _StubRuntime()
    server = DesktopAPIServer(rt)
    return server


def test_create_automation_via_new_schedule_payload(tmp_path):
    server = _build_full_server(tmp_path)
    req = _FakeRequest(
        body={
            "name": "n",
            "prompt": "p",
            "schedule": {"kind": "daily", "time": "09:00", "tz": "Asia/Shanghai"},
        }
    )
    resp = asyncio.run(server.create_automation(req))
    body = json.loads(resp.text)
    assert "automation" in body
    auto = body["automation"]
    assert auto["schedule"]["kind"] == "daily"
    # 1.0.5+: no legacy field echo — frontends must use `schedule`.
    assert "schedule_time" not in auto
    assert "interval_seconds" not in auto


def test_create_automation_rejects_flat_interval_seconds(tmp_path):
    """Flat `interval_seconds` is not accepted on create."""
    server = _build_full_server(tmp_path)
    req = _FakeRequest(
        body={"name": "flat", "prompt": "p", "interval_seconds": 600}
    )
    resp = asyncio.run(server.create_automation(req))
    assert resp.status == 400
    body = json.loads(resp.text)
    assert body["error"] == "invalid_schedule"
    assert "schedule" in body["detail"]


def test_create_automation_rejects_flat_schedule_time(tmp_path):
    """Flat `schedule_time` is not accepted on create."""
    server = _build_full_server(tmp_path)
    req = _FakeRequest(
        body={"name": "flat", "prompt": "p", "schedule_time": "21:00:00"}
    )
    resp = asyncio.run(server.create_automation(req))
    assert resp.status == 400


def test_create_automation_rejects_flat_both_fields(tmp_path):
    server = _build_full_server(tmp_path)
    req = _FakeRequest(
        body={
            "name": "flat",
            "prompt": "p",
            "interval_seconds": 300,
            "schedule_time": "21:00:00",
        }
    )
    resp = asyncio.run(server.create_automation(req))
    assert resp.status == 400


def test_create_automation_missing_schedule_returns_400(tmp_path):
    server = _build_full_server(tmp_path)
    req = _FakeRequest(body={"name": "x", "prompt": "p"})
    resp = asyncio.run(server.create_automation(req))
    assert resp.status == 400


def test_create_automation_invalid_schedule_returns_400(tmp_path):
    server = _build_full_server(tmp_path)
    req = _FakeRequest(
        body={
            "name": "bad",
            "prompt": "p",
            "schedule": {"kind": "weekly", "days": ["Mon"], "time": "09:00"},
        }
    )
    resp = asyncio.run(server.create_automation(req))
    assert resp.status == 400
    body = json.loads(resp.text)
    assert body["error"] == "invalid_schedule"


def test_update_automation_via_new_schedule_payload(tmp_path):
    server = _build_full_server(tmp_path)
    # Seed
    server.automations["aid-1"] = server.automations.get("aid-1")  # noop
    from ziva.transports.desktop_api.server import Automation

    a = Automation(
        id="aid-1",
        name="n",
        prompt="p",
        schedule={"kind": "every", "interval_seconds": 300},
    )
    server.automations["aid-1"] = a
    server._persist_automation(a)

    req = _FakeRequest(
        match_info={"aid": "aid-1"},
        body={
            "schedule": {"kind": "weekly", "days": ["MO", "WE"], "time": "10:00"}
        },
    )
    resp = asyncio.run(server.update_automation(req))
    assert resp.status == 200
    # Reload from disk to confirm persistence.
    from ziva.storage.file_storage import FileStorage

    stored = next(t for t in FileStorage.list_automations("test-project") if t["id"] == "aid-1")
    assert stored["schedule"]["kind"] == "weekly"


def test_update_automation_rejects_flat_interval_seconds(tmp_path):
    """Flat `interval_seconds` is not accepted on update."""
    server = _build_full_server(tmp_path)
    from ziva.transports.desktop_api.server import Automation

    a = Automation(
        id="aid-1",
        name="n",
        prompt="p",
        schedule={"kind": "every", "interval_seconds": 300},
    )
    server.automations["aid-1"] = a
    server._persist_automation(a)

    req = _FakeRequest(
        match_info={"aid": "aid-1"},
        body={"interval_seconds": 1200},
    )
    resp = asyncio.run(server.update_automation(req))
    assert resp.status == 400
    body = json.loads(resp.text)
    assert body["error"] == "invalid_schedule"
    # Original schedule is untouched.
    assert server.automations["aid-1"].schedule["interval_seconds"] == 300


def test_load_persisted_automations_silently_skips_unparseable_records(tmp_path):
    """Records that can't be parsed are skipped silently — no log, no
    file rewrite. The valid records load normally."""
    server = _build_full_server(tmp_path)
    from ziva.storage.file_storage import FileStorage

    FileStorage.replace_automations(
        "test-project",
        [
            {
                "id": "old-aid",
                "name": "old",
                "prompt": "p",
                "interval_seconds": 300,
                "schedule_time": "21:00:00",
                "enabled": True,
                "next_run": None,
            },
            {
                "id": "good-aid",
                "name": "good",
                "prompt": "p",
                "schedule": {"kind": "daily", "time": "09:00"},
                "enabled": True,
                "next_run": None,
            },
        ],
    )

    server._load_persisted_automations()

    # Unparseable record silently skipped.
    assert "old-aid" not in server.automations
    # Valid record loaded.
    assert "good-aid" in server.automations
    # The on-disk file is NOT rewritten — orphan records stay put.
    remaining = FileStorage.list_automations("test-project")
    assert {r["id"] for r in remaining} == {"old-aid", "good-aid"}


def test_load_persisted_automations_silently_skips_invalid_schedule_records(tmp_path):
    server = _build_full_server(tmp_path)
    from ziva.storage.file_storage import FileStorage

    FileStorage.replace_automations(
        "test-project",
        [
            {
                "id": "garbage-aid",
                "name": "garbage",
                "prompt": "p",
                "schedule": {"kind": "weekly", "days": ["Mon"], "time": "25:00"},
                "enabled": True,
                "next_run": None,
            }
        ],
    )

    server._load_persisted_automations()

    assert "garbage-aid" not in server.automations
    # Disk file untouched.
    assert {r["id"] for r in FileStorage.list_automations("test-project")} == {"garbage-aid"}


def test_run_automation_now_uses_session_when_provided(tmp_path):
    """`POST /automations/{aid}/run` accepts a target session_id and
    surfaces it in the response payload."""
    server = _build_full_server(tmp_path)
    from ziva.transports.desktop_api.server import Automation

    a = Automation(
        id="aid-1",
        name="n",
        prompt="p",
        schedule={"kind": "every", "interval_seconds": 300},
    )
    server.automations["aid-1"] = a

    # Stub _run_automation_once to capture the call args.
    seen: dict = {}

    async def _stub(automation, scheduled, session_id=None):
        seen["scheduled"] = scheduled
        seen["session_id"] = session_id
        from ziva.shared_types import ChatResult

        return ChatResult(role="assistant", content="ok")

    server._run_automation_once = _stub  # type: ignore[assignment]

    req = _FakeRequest(match_info={"aid": "aid-1"}, body={"session_id": "user-sess"})
    resp = asyncio.run(server.run_automation_now(req))
    assert resp.status == 200
    # asyncio.create_task schedules the coroutine but doesn't run it
    # synchronously; give it a tick.
    asyncio.run(asyncio.sleep(0))
    assert seen["session_id"] == "user-sess"
    assert seen["scheduled"] is False


def test_trigger_automation_now_helper_exposed_on_runtime(tmp_path):
    """`runtime.trigger_automation_now` must be wired by DesktopAPIServer.__init__
    so the manage_scheduled_tasks tool's `action: "run"` can find it."""
    server = _build_full_server(tmp_path)
    assert hasattr(server.runtime, "trigger_automation_now")
    assert server.runtime.trigger_automation_now == server.trigger_automation_now


def test_trigger_automation_now_raises_for_unknown_id(tmp_path):
    server = _build_full_server(tmp_path)
    with pytest.raises(KeyError):
        asyncio.run(server.trigger_automation_now("missing"))