"""Tests for the device memory watchdog (src/ziva/memwatch.py)."""

import asyncio

import pytest

import ziva.memwatch as memwatch
from ziva.memwatch import (
    find_chrome_pids,
    read_mem_available_mb,
    read_rss_mb,
    run_mem_watchdog,
)


@pytest.fixture(autouse=True)
def _reset_chrome_activity():
    memwatch._last_chrome_use = None
    yield
    memwatch._last_chrome_use = None

MEMINFO = b"""MemTotal:       12000000 kB
MemFree:          300000 kB
MemAvailable:     819200 kB
Buffers:           50000 kB
"""

MEMINFO_PRESSURE = b"""MemTotal:       12000000 kB
MemAvailable:     204800 kB
"""


class FakeProc:
    """In-memory stand-in for /proc."""

    def __init__(self, meminfo: bytes, processes: dict[int, bytes]):
        self.meminfo = meminfo
        self.processes = processes

    def read(self, path: str) -> bytes:
        if path == "/proc/meminfo":
            return self.meminfo
        if path.startswith("/proc/") and path.endswith("/cmdline"):
            return self.processes.get(int(path.split("/")[2]), b"")
        if path.startswith("/proc/") and path.endswith("/status"):
            pid = int(path.split("/")[2])
            rss = self.processes.get(pid * -1, b"VmRSS:\t 0 kB")
            return rss
        raise OSError(path)

    def listdir(self, _path: str):
        pids = {str(p) for p in self.processes if p > 0}
        return sorted(pids) + ["self", "cpuinfo"]


def test_read_mem_available_mb_parses_kb_to_mb():
    fake = FakeProc(MEMINFO, {})
    assert read_mem_available_mb(read=fake.read) == pytest.approx(800.0)


def test_read_mem_available_mb_none_without_proc():
    assert read_mem_available_mb(read=lambda p: (_ for _ in ()).throw(OSError())) is None


def test_find_chrome_pids_matches_markers_only():
    fake = FakeProc(
        MEMINFO,
        {
            100: b"/opt/ziva-venv/bin/node\x00/opt/chrome-devtools-mcp/dist/index.js",
            200: b"/opt/chromium/chrome-bin\x00--no-sandbox",
            300: b"python3\x00-m\x00ziva.app.cli\x00desktop\x00serve",
            400: b"/bin/sh\x00/opt/ensure-chromium.sh",
        },
    )
    pids = find_chrome_pids(listdir=fake.listdir, read=fake.read, own_pid=300)
    assert pids == [100, 200, 400]  # backend pid 300 never matches


def test_read_rss_mb_parses_status():
    fake = FakeProc(MEMINFO, {})
    fake.processes[-200] = b"Name:\tchrome\nVmRSS:\t 614400 kB\n"
    assert read_rss_mb(200, read=fake.read) == pytest.approx(600.0)


@pytest.mark.asyncio
async def test_watchdog_kills_chrome_under_pressure_and_calls_on_pressure():
    fake = FakeProc(
        MEMINFO_PRESSURE,
        {
            200: b"/opt/chromium/chrome-bin",
            -200: b"VmRSS:\t 409600 kB",
        },
    )
    killed, calls = [], []

    def fake_kill(pid, sig):
        killed.append((pid, sig))

    async def fake_sleep(_s):
        raise asyncio.CancelledError  # stop after the first tick

    with pytest.raises(asyncio.CancelledError):
        await run_mem_watchdog(
            lambda: calls.append(1),
            read=fake.read,
            listdir=fake.listdir,
            kill=fake_kill,
            sleep=fake_sleep,
            own_pid=300,
            log=lambda m: None,
        )
    assert killed == [(200, 9)]
    assert calls == [1]


@pytest.mark.asyncio
async def test_watchdog_no_kill_when_memory_healthy():
    fake = FakeProc(MEMINFO, {200: b"/opt/chromium/chrome-bin"})
    killed, calls = [], []

    async def fake_sleep(_s):
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await run_mem_watchdog(
            lambda: calls.append(1),
            read=fake.read,
            listdir=fake.listdir,
            kill=lambda pid, sig: killed.append(pid),
            sleep=fake_sleep,
            own_pid=300,
            log=lambda m: None,
        )
    assert killed == []
    assert calls == []


@pytest.mark.asyncio
async def test_watchdog_idle_exits_unused_chrome_stack():
    """Chrome resident but no browser tool call in the window → killed."""
    fake = FakeProc(MEMINFO, {200: b"/opt/chromium/chrome-bin", -200: b"VmRSS:\t 409600 kB"})
    killed, calls, now, ticks = [], [], [0.0], [0]

    def fake_kill(pid, sig):
        killed.append(pid)

    async def fake_sleep(_s):
        ticks[0] += 1
        if ticks[0] >= 3:
            raise asyncio.CancelledError
        now[0] += 700.0  # jump past the idle window

    with pytest.raises(asyncio.CancelledError):
        await run_mem_watchdog(
            lambda: calls.append(1),
            read=fake.read,
            listdir=fake.listdir,
            kill=fake_kill,
            sleep=fake_sleep,
            clock=lambda: now[0],
            own_pid=300,
            log=lambda m: None,
        )
    assert killed == [200]
    assert calls == [1]


@pytest.mark.asyncio
async def test_watchdog_keeps_chrome_when_browser_active():
    """A recent browser tool call resets the idle window → no kill."""
    fake = FakeProc(MEMINFO, {200: b"/opt/chromium/chrome-bin"})
    killed, calls, now, ticks = [], [], [0.0], [0]

    async def fake_sleep(_s):
        ticks[0] += 1
        if ticks[0] >= 3:
            raise asyncio.CancelledError
        now[0] += 700.0
        memwatch.note_chrome_activity(clock=lambda: now[0])  # activity in window

    with pytest.raises(asyncio.CancelledError):
        await run_mem_watchdog(
            lambda: calls.append(1),
            read=fake.read,
            listdir=fake.listdir,
            kill=lambda pid, sig: killed.append(pid),
            sleep=fake_sleep,
            clock=lambda: now[0],
            own_pid=300,
            log=lambda m: None,
        )
    assert killed == []
    assert calls == []


def test_note_chrome_activity_roundtrip():
    memwatch.note_chrome_activity(clock=lambda: 1234.0)
    assert memwatch.last_chrome_activity() == 1234.0


@pytest.mark.asyncio
async def test_watchdog_exits_without_meminfo_macos():
    ran = []

    def no_proc(_path):
        raise OSError()

    async def fake_sleep(_s):
        ran.append(1)

    await run_mem_watchdog(
        lambda: None, read=no_proc, listdir=lambda p: [], kill=lambda *a: None,
        sleep=fake_sleep, log=lambda m: None,
    )
    assert ran == []  # returned before ever sleeping


import asyncio  # noqa: E402  (used by the async tests above)
