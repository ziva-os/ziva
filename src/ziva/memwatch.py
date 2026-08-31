"""Device memory watchdog (Android/proot).

The backend is the usual victim of the Android low-memory killer: when the
device runs tight (WebView rendering a huge conversation + headless Chromium
+ proot + this Python process), LMK SIGKILLs the largest process and that is
often us — the whole app then loses the backend for ~10s (restart + boot
handshake). Chromium is usually the biggest *disposable* consumer.

This watchdog watches MemAvailable (inside proot /proc/meminfo shows the
device-global values) and, under pressure, kills the Chromium process tree
(browser + chrome-devtools-mcp node bridge) *before* the kernel kills us.
The MCP layer treats that like any transport death: the next tool call
reconnects and ensure-chromium respawns the browser on demand.

Non-Linux hosts (macOS dev machines) have no /proc/meminfo — the watchdog
exits immediately, so this is safe to start unconditionally.
"""

import asyncio
import inspect
import logging
import os

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
        if any(marker in cmdline for marker in _CHROME_MARKERS):
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
    interval_s: float = 15.0,
    warn_mb: float = 1000.0,
    pressure_mb: float = 350.0,
    kill_cooldown_s: float = 90.0,
    sleep=asyncio.sleep,
    read=_read_bytes,
    listdir=os.listdir,
    kill=os.kill,
    own_pid=None,
    log=None,
) -> None:
    """Watch device memory; kill the Chromium tree when it gets critical.

    ``on_pressure`` is invoked (sync or async) after a pressure kill so the
    caller can reset the MCP connection state — the dead browser then
    respawns lazily on the next tool call.
    """
    log = log or (lambda msg: logger.info("[mem] %s", msg))
    if own_pid is None:
        own_pid = os.getpid()
    if read_mem_available_mb(read=read) is None:
        return  # no /proc/meminfo (macOS/dev host) — nothing to watch
    last_kill = -kill_cooldown_s  # allow an immediate first kill
    tick = 0
    while True:
        avail = read_mem_available_mb(read=read)
        if avail is None:
            return
        chrome_pids = find_chrome_pids(listdir=listdir, read=read, own_pid=own_pid)
        chrome_mb = sum(read_rss_mb(pid, read=read) for pid in chrome_pids)
        # Heartbeat only when memory is tight or a browser stack exists, so
        # the shared log stays small on healthy days.
        if avail < warn_mb or chrome_pids:
            log(
                f"avail={avail:.0f}MB chrome={chrome_mb:.0f}MB "
                f"({len(chrome_pids)} procs)"
            )
        now = tick * interval_s
        if (
            avail < pressure_mb
            and chrome_pids
            and now - last_kill >= kill_cooldown_s
        ):
            killed = _kill_tree(chrome_pids, kill=kill)
            log(
                f"PRESSURE: avail={avail:.0f}MB → SIGKILL chrome tree "
                f"({killed} procs, ~{chrome_mb:.0f}MB); MCP reconnects on "
                f"next use"
            )
            last_kill = now
            result = on_pressure()
            if inspect.isawaitable(result):
                await result
        tick += 1
        await sleep(interval_s)
