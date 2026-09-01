"""Device memory watchdog (Android/proot).

Two complementary mechanisms:

- **Idle exit** (primary): the headless Chromium stack is only resident
  because a session used browser tools at some point. Ten minutes without
  a browser tool call means it is dead weight (0.5-1GB), so the watchdog
  exits it; the next browser tool call respawns it in seconds. This keeps
  the device out of memory pressure in the first place instead of
  reacting to it.
- **Pressure kill** (last resort): when MemAvailable is already critical,
  kill the Chromium tree *before* the kernel's low-memory killer kills
  the BACKEND — a dead browser respawns lazily, a dead backend takes the
  whole app down for ~10s (this was the recurring code=137 in logs).

Browser activity is reported by the MCP layer via :func:`note_chrome_activity`.

Non-Linux hosts (macOS dev machines) have no /proc/meminfo — the watchdog
exits immediately, so this is safe to start unconditionally.
"""

import asyncio
import inspect
import logging
import os
import time

logger = logging.getLogger(__name__)

# Substrings of /proc/<pid>/cmdline that identify the on-device browser
# stack: the wrapper + real binary (/opt/chromium/chrome{,-bin}), the MCP
# node bridge, and the download/fixup shell wrapper. Matches are substrings,
# so "/opt/chromium/chrome" also covers "chrome-bin".
_CHROME_MARKERS = (
    b"/opt/chromium/chrome",
    b"chrome-devtools-mcp",
    b"ensure-chromium",
)

# Second-stage shed candidates: heavy, RESPAWNABLE tool children that are
# not the browser stack — npm/npx skill installs, pip/uv installs, and any
# other node-based MCP server. Killing one fails a single tool call (the
# turn continues); letting the kernel OOM-kill the backend kills the whole
# app for ~10s and interrupts every session.
_TOOL_CHILD_MARKERS = (
    b"npx",
    b"npm exec",
    b"npm install",
    b"pip install",
    b"uv pip",
    b"uv tool",
    # /proc cmdline is NUL-separated (no spaces) — bare substring. Matches
    # node itself and anything under node_modules.
    b"node",
)

# Last time a chrome MCP tool call ran (monotonic clock), or None when the
# browser stack has never been used this process lifetime. Written by the
# MCP layer, read by the watchdog loop.
_last_chrome_use: float | None = None


def note_chrome_activity(clock=time.monotonic) -> None:
    """Record that a browser MCP tool call is happening (liveness signal)."""
    global _last_chrome_use
    _last_chrome_use = clock()


def last_chrome_activity() -> float | None:
    return _last_chrome_use


def _read_bytes(path: str) -> bytes:
    with open(path, "rb") as f:
        return f.read()


def read_mem_available_mb(read=_read_bytes) -> float | None:
    """Device-global MemAvailable in MB, or None when unavailable (macOS)."""
    try:
        data = read("/proc/meminfo").decode(errors="replace")
    except OSError:
        return None
    for line in data.splitlines():
        if line.startswith("MemAvailable:"):
            try:
                return int(line.split()[1]) / 1024.0
            except (ValueError, IndexError):
                return None
    return None


def read_rss_mb(pid: int, read=_read_bytes) -> float:
    """RSS of one pid in MB (0 when unreadable)."""
    try:
        data = read(f"/proc/{pid}/status").decode(errors="replace")
    except OSError:
        return 0.0
    for line in data.splitlines():
        if line.startswith("VmRSS:"):
            try:
                return int(line.split()[1]) / 1024.0
            except (ValueError, IndexError):
                return 0.0
    return 0.0


def find_chrome_pids(listdir=os.listdir, read=_read_bytes, own_pid=None) -> list[int]:
    """Pids of the Chromium browser stack (browser, node bridge, wrappers)."""
    return _find_pids(_CHROME_MARKERS, listdir=listdir, read=read, own_pid=own_pid)


def find_tool_child_pids(listdir=os.listdir, read=_read_bytes, own_pid=None) -> list[int]:
    """Pids of heavy respawnable tool children (npm/npx/pip/uv/node MCP)."""
    return _find_pids(_TOOL_CHILD_MARKERS, listdir=listdir, read=read, own_pid=own_pid)


def _find_pids(markers, listdir=os.listdir, read=_read_bytes, own_pid=None) -> list[int]:
    pids: list[int] = []
    for name in listdir("/proc"):
        if not name.isdigit():
            continue
        pid = int(name)
        if own_pid is not None and pid == own_pid:
            continue
        try:
            cmdline = read(f"/proc/{pid}/cmdline")
        except OSError:
            continue  # permission or vanished mid-scan
        if any(marker in cmdline for marker in markers):
            pids.append(pid)
    return sorted(pids)


def _kill_tree(pids: list[int], kill=os.kill) -> int:
    killed = 0
    for pid in pids:
        try:
            kill(pid, 9)
            killed += 1
        except (ProcessLookupError, PermissionError, OSError):
            pass
    return killed


async def run_mem_watchdog(
    on_pressure,
    *,
    interval_s: float = 5.0,
    idle_exit_s: float = 600.0,
    warn_mb: float = 1500.0,
    pressure_mb: float = 450.0,
    kill_cooldown_s: float = 60.0,
    sleep=asyncio.sleep,
    read=_read_bytes,
    listdir=os.listdir,
    kill=os.kill,
    clock=time.monotonic,
    own_pid=None,
    log=None,
) -> None:
    """Watch device memory; shed respawnable children before the kernel does.

    Escalation under pressure: (1) Chromium tree (0.5-1GB, lazy respawn),
    (2) heavy tool children — npm/npx/pip installs and node MCP servers
    (one failed tool call, turn survives). Either is strictly better than
    the kernel SIGKILLing the BACKEND, which takes the whole app down.

    ``on_pressure`` is invoked (sync or async) after any watchdog kill so
    the caller can reset the MCP connection state — the dead pieces then
    respawn lazily on the next tool call.
    """
    global _last_chrome_use
    log = log or (lambda msg: logger.info("[mem] %s", msg))
    if own_pid is None:
        own_pid = os.getpid()
    avail0 = read_mem_available_mb(read=read)
    if avail0 is None:
        return  # no /proc/meminfo (macOS/dev host) — nothing to watch
    log(f"watchdog started, avail={avail0:.0f}MB, poll={interval_s:.0f}s")
    last_kill = -kill_cooldown_s  # allow an immediate first kill
    chrome_seen = False
    chrome_up_since = clock()
    tick = 0
    while True:
        now = clock()
        avail = read_mem_available_mb(read=read)
        if avail is None:
            return
        chrome_pids = find_chrome_pids(listdir=listdir, read=read, own_pid=own_pid)
        chrome_mb = sum(read_rss_mb(pid, read=read) for pid in chrome_pids)
        if chrome_pids and not chrome_seen:
            # Browser stack just appeared (boot prewarm or on-demand
            # respawn): start a fresh idle window so it isn't killed
            # before it ever had a chance.
            chrome_seen = True
            chrome_up_since = now
        elif not chrome_pids:
            chrome_seen = False
        # Heartbeat only when memory is tight or a browser stack exists, so
        # the shared log stays small on healthy days.
        if avail < warn_mb or chrome_pids:
            log(
                f"avail={avail:.0f}MB chrome={chrome_mb:.0f}MB "
                f"({len(chrome_pids)} procs)"
            )
        last_use = _last_chrome_use
        cooled = now - last_kill >= kill_cooldown_s
        if chrome_pids and (
            now - max(chrome_up_since, last_use if last_use is not None else chrome_up_since)
            >= idle_exit_s
        ):
            # Idle exit — the primary mechanism. The stack has not served
            # a single browser tool call in the window; it is pure
            # baseline weight right now.
            if cooled:
                killed = _kill_tree(chrome_pids, kill=kill)
                log(
                    f"IDLE-EXIT: no browser call for {idle_exit_s:.0f}s → "
                    f"SIGKILL chrome tree ({killed} procs, "
                    f"~{chrome_mb:.0f}MB); respawns on next use"
                )
                last_kill = now
                chrome_seen = False
                result = on_pressure()
                if inspect.isawaitable(result):
                    await result
        elif avail < pressure_mb and cooled:
            # Last resort — device is about to OOM-kill the backend. Shed
            # the browser stack first; if it is not resident, shed heavy
            # tool children (npm/npx installs, node MCP servers). One dead
            # tool call beats one dead backend.
            if chrome_pids:
                target, kind, mb = chrome_pids, "chrome tree", chrome_mb
            else:
                tool_pids = find_tool_child_pids(
                    listdir=listdir, read=read, own_pid=own_pid
                )
                target = tool_pids
                kind = "tool children"
                mb = sum(read_rss_mb(pid, read=read) for pid in tool_pids)
            if target:
                killed = _kill_tree(target, kill=kill)
                log(
                    f"PRESSURE: avail={avail:.0f}MB → SIGKILL {kind} "
                    f"({killed} procs, ~{mb:.0f}MB); respawns on next use"
                )
                last_kill = now
                chrome_seen = False
                result = on_pressure()
                if inspect.isawaitable(result):
                    await result
        tick += 1
        await sleep(interval_s)
