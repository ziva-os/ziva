"""Shared scheduling primitives for `manage_scheduled_tasks`.

A single source of truth for schedule parsing, normalization, and
next-run computation, so the plugin tool (`plugins/tools/manage_scheduled_tasks/impl.py`)
and the HTTP server (`src/ziva/transports/desktop_api/server.py`) can never
drift out of sync on what "every day at 21:00" actually means.

Design (modeled after OpenClaw's `src/cron/{schedule,normalize,parse}.ts`):

- `schedule.kind` is a discriminated union — `every | daily | weekly`.
  Exactly one mode per task; ambiguous combinations are rejected at
  normalization time, not silently coerced.
- `time` is `HH:MM` (24-hour). `tz` is an optional IANA name; when
  omitted we use the host's local timezone via `datetime.astimezone()`
  with no argument, which preserves the runtime's existing
  local-time behaviour.
- `interval_seconds` is the drift-free interval for `kind=every`.
  The `anchor_at` field is set when the task is created and is used to
  compute the next run as `anchor + ⌈(now − anchor) / N⌉ × N`, which
  keeps the trigger perfectly aligned to a grid instead of drifting
  forward on every miss (this is OpenClaw's `anchorMs` pattern).
- All extra fields under `schedule.*` are tolerated by the model-facing
  schema but stripped at normalization — the canonical form only carries
  fields relevant to its `kind`. Same philosophy as OpenClaw's
  `coerceSchedule` in `src/cron/normalize.ts`.
"""

from __future__ import annotations

import logging
import math
import re
from datetime import datetime, timedelta
from typing import Any
try:
    from zoneinfo import ZoneInfo  # py3.9+
except ImportError:  # pragma: no cover
    ZoneInfo = None  # type: ignore[assignment]


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

WEEKDAY_SET = {"MO", "TU", "WE", "TH", "FR", "SA", "SU"}
WEEKDAY_TO_INT = {"MO": 0, "TU": 1, "WE": 2, "TH": 3, "FR": 4, "SA": 5, "SU": 6}

# every kind caps at 30 days to keep the loader loop healthy and to
# avoid silent mistakes (a 5-minute cron is a sane schedule; a
# once-a-month check belongs in human memory, not in a hot path).
MAX_INTERVAL_SECONDS = 30 * 86400
MIN_INTERVAL_SECONDS = 1

_TIME_PATTERN = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


# ---------------------------------------------------------------------------
# Validation / normalization
# ---------------------------------------------------------------------------


class ScheduleError(ValueError):
    """Raised when a schedule dict fails validation. The error message is
    safe to surface to the model — no internal state is leaked."""


def normalize_schedule(raw: Any) -> dict:
    """Validate ``raw`` and return the canonical schedule dict.

    The canonical form keeps only the fields relevant to the chosen
    ``kind``:

      - ``every``  → ``{kind, interval_seconds, [anchor_at]}``
      - ``daily``  → ``{kind, time, [tz]}``
      - ``weekly`` → ``{kind, days, time, [tz]}``

    Raises :class:`ScheduleError` on any malformed input. The error
    message is LLM-friendly.
    """
    if not isinstance(raw, dict):
        raise ScheduleError("schedule must be an object with `kind`")
    kind = raw.get("kind")
    if kind not in ("every", "daily", "weekly"):
        raise ScheduleError(
            f"schedule.kind must be one of every/daily/weekly, got {kind!r}"
        )

    out: dict = {"kind": kind}
    if kind == "every":
        n = raw.get("interval_seconds")
        if not isinstance(n, int) or isinstance(n, bool):
            raise ScheduleError("kind=every requires integer interval_seconds")
        if n < MIN_INTERVAL_SECONDS:
            raise ScheduleError(
                f"kind=every interval_seconds must be ≥ {MIN_INTERVAL_SECONDS}"
            )
        if n > MAX_INTERVAL_SECONDS:
            raise ScheduleError(
                f"kind=every interval_seconds capped at {MAX_INTERVAL_SECONDS} (30 days)"
            )
        out["interval_seconds"] = int(n)
        anchor = raw.get("anchor_at")
        if isinstance(anchor, (int, float)):
            out["anchor_at"] = int(anchor)
    elif kind == "daily":
        t = raw.get("time")
        if not _TIME_PATTERN.match(t or ""):
            raise ScheduleError("kind=daily requires `time` in HH:MM (24-hour)")
        out["time"] = t
        _maybe_tz(out, raw)
    else:  # weekly
        days = raw.get("days")
        if not isinstance(days, list) or not days:
            raise ScheduleError("kind=weekly requires non-empty `days` array")
        if not all(isinstance(d, str) and d in WEEKDAY_SET for d in days):
            raise ScheduleError(
                "kind=weekly `days` must be ISO weekday codes (MO,TU,WE,TH,FR,SA,SU)"
            )
        if len(set(days)) != len(days):
            raise ScheduleError("kind=weekly `days` contains duplicates")
        out["days"] = list(days)
        t = raw.get("time")
        if not _TIME_PATTERN.match(t or ""):
            raise ScheduleError("kind=weekly requires `time` in HH:MM (24-hour)")
        out["time"] = t
        _maybe_tz(out, raw)
    return out


def _maybe_tz(out: dict, raw: dict) -> None:
    tz = raw.get("tz")
    if tz is None or tz == "":
        return
    if not isinstance(tz, str):
        raise ScheduleError("`tz` must be a string IANA timezone name (e.g. 'Asia/Shanghai')")
    if ZoneInfo is not None:
        try:
            ZoneInfo(tz)
        except Exception as exc:
            raise ScheduleError(
                f"`tz` {tz!r} is not a valid IANA timezone ({exc})"
            ) from exc
    out["tz"] = tz





# ---------------------------------------------------------------------------
# Next-run computation
# ---------------------------------------------------------------------------


def compute_next_run(schedule: dict, now: float | datetime) -> float | None:
    """Return the unix timestamp of the next scheduled run.

    ``now`` may be a ``float`` (epoch seconds) or a ``datetime``. Three
    branches:

      - ``every``  — drift-free grid alignment via ``anchor_at``.
      - ``daily``  — next occurrence of ``time`` (in ``tz`` or local).
      - ``weekly`` — next occurrence of the earliest matching ``days``
                     at ``time``.

    Returns ``None`` when the schedule is unparseable (defensive —
    callers fall back to "do nothing" rather than spinning).
    """
    try:
        schedule = normalize_schedule(schedule)  # re-validate defensively
    except ScheduleError:
        # Defensive: the scheduler loop should never crash because of a
        # corrupt or half-migrated record. The caller treats None as
        # "skip this task until it's fixed".
        logger.debug("compute_next_run: invalid schedule %r; returning None", schedule)
        return None
    kind = schedule["kind"]

    if isinstance(now, (int, float)):
        now_dt = datetime.fromtimestamp(now)
    else:
        now_dt = now

    if kind == "every":
        return _next_every(schedule, now_dt)
    if kind == "daily":
        return _next_daily_or_weekly(schedule, now_dt, days=None)
    if kind == "weekly":
        # Validate days defensively (normalize_schedule already did this)
        return _next_daily_or_weekly(schedule, now_dt, days=schedule["days"])
    return None


def _next_every(schedule: dict, now: datetime) -> float:
    n = schedule["interval_seconds"]
    anchor = float(schedule.get("anchor_at", now.timestamp()))
    if anchor > now.timestamp():
        # Anchor is in the future (e.g. the task was just created with a
        # backdated anchor). Fire at the anchor.
        return anchor
    elapsed = now.timestamp() - anchor
    steps = int(math.floor(elapsed / n)) + 1
    return anchor + steps * n


def _next_daily_or_weekly(schedule: dict, now: datetime, days: list | None) -> float | None:
    tz_name = schedule.get("tz")
    if tz_name and ZoneInfo is not None:
        try:
            tz = ZoneInfo(tz_name)
        except Exception:
            tz = None
    else:
        tz = None
    local = now.astimezone(tz) if tz is not None else now.astimezone()

    h, m = map(int, schedule["time"].split(":"))
    target_weekdays = (
        {WEEKDAY_TO_INT[d] for d in days} if days else None
    )

    # Walk up to 8 days forward (8 covers any weekday gap + today).
    for offset in range(0, 8):
        cand = (local + timedelta(days=offset)).replace(
            hour=h, minute=m, second=0, microsecond=0,
        )
        if target_weekdays is not None and cand.weekday() not in target_weekdays:
            continue
        if cand > local:
            return cand.timestamp()
    return None


# ---------------------------------------------------------------------------
# Human-readable description (for tool responses + UI list)
# ---------------------------------------------------------------------------


def describe_schedule(schedule: Any) -> str:
    """Return a short human description, e.g. ``"weekdays at 09:00 [Asia/Shanghai]"``.

    Tolerates either a canonical schedule dict or a legacy ``Automation``
    dict (so the tool can use it on freshly loaded records too).
    """
    if not isinstance(schedule, dict):
        return "unconfigured"
    if "schedule" in schedule and isinstance(schedule["schedule"], dict):
        s = schedule["schedule"]
    else:
        s = schedule
    kind = s.get("kind")
    tz = s.get("tz")
    tz_part = f" [{tz}]" if tz else ""
    if kind == "every":
        n = s.get("interval_seconds")
        if not n:
            return "unconfigured"
        return _humanize_interval(n)
    if kind == "daily":
        t = s.get("time")
        return f"daily at {t}{tz_part}" if t else "daily (no time set)"
    if kind == "weekly":
        t = s.get("time", "?")
        days = s.get("days") or []
        return f"{_humanize_days(days)} at {t}{tz_part}"
    # Legacy fall-through (records not yet migrated)
    if s.get("schedule_time"):
        return f"daily at {s['schedule_time']} (legacy)"
    if s.get("interval_seconds"):
        return _humanize_interval(s["interval_seconds"]) + " (legacy)"
    return "unconfigured"


def _humanize_interval(seconds: int) -> str:
    if seconds % 86400 == 0:
        days = seconds // 86400
        return f"every {days} day{'s' if days != 1 else ''}"
    if seconds % 3600 == 0:
        hours = seconds // 3600
        return f"every {hours} hour{'s' if hours != 1 else ''}"
    if seconds % 60 == 0:
        minutes = seconds // 60
        return f"every {minutes} minute{'s' if minutes != 1 else ''}"
    return f"every {seconds} seconds"


def _humanize_days(days: list) -> str:
    if not days:
        return "no days"
    weekday_set = set(days)
    if weekday_set == {"MO", "TU", "WE", "TH", "FR"}:
        return "weekdays"
    if weekday_set == {"SA", "SU"}:
        return "weekends"
    if weekday_set == {"MO", "TU", "WE", "TH", "FR", "SA", "SU"}:
        return "every day"
    return "every " + ", ".join(days)


__all__ = [
    "ScheduleError",
    "WEEKDAY_SET",
    "WEEKDAY_TO_INT",
    "MAX_INTERVAL_SECONDS",
    "MIN_INTERVAL_SECONDS",
    "normalize_schedule",
    "compute_next_run",
    "describe_schedule",
]