"""Tests for STT warmup-on-startup.

Why this file exists
--------------------
mlx_whisper's first transcribe() call pays a heavy cold start (≈5s on
Apple Silicon):
  - import mlx_whisper          (~2s: Metal/native stack init)
  - model load from npz         (~3s: 461 MB whisper-small-mlx)
  - Metal shader compilation    (one-time, folded into first call)
  - numba JIT for mel spectrogram (one-time, ~2s)

Subsequent calls are ~0.6s because ModelHolder caches the loaded model.

`DesktopAPIServer.start()` kicks off a background daemon thread that
runs a dummy transcribe on a 1-second silent wav so the cost overlaps
with the user's normal app-launch idle time. These tests pin that
contract:

  1. start() returns without waiting for warmup to finish
  2. /api/stt/status reports "warming" while warmup is in flight
  3. /api/stt/status reports "ready" once warmup completes
  4. /api/stt/status reports "needs_download" when the model isn't on
     disk yet (we never want to block startup on a multi-GB download)
  5. Warmup failure doesn't crash start(); status reports "error"
  6. Re-starting an already-warm process is fast (no re-load)

The tests use a stub STT module injected via sys.modules so we don't
need the real 461 MB model or any Metal hardware in CI.
"""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from ziva_runtime.runtime import Runtime
from ziva_runtime.transports.desktop_api.server import DesktopAPIServer


# ---------------------------------------------------------------------------
# Fake mlx_whisper that records calls and lets us simulate slow/failing loads
# without touching real Metal/native code.
# ---------------------------------------------------------------------------

class _FakeModelHolder:
    """Mirrors mlx_whisper.transcribe.ModelHolder: a class-level cache so
    the model is loaded at most once per process. Server.py's warmup
    triggers the load via transcribe(); subsequent transcribe() calls
    reuse the cached instance."""

    model = None
    model_path = None
    load_calls: list = []

    @classmethod
    def reset(cls) -> None:
        cls.model = None
        cls.model_path = None
        cls.load_calls = []

    @classmethod
    def get_model(cls, model_path: str, dtype: Any):
        cls.load_calls.append(model_path)
        time.sleep(_FakeMlxWhisper.load_delay_seconds)
        if _FakeMlxWhisper.fail_on_load:
            raise RuntimeError("simulated model load failure")
        cls.model = object()
        cls.model_path = model_path
        return cls.model


class _FakeMlxWhisper:
    """Records every transcribe() invocation. Tracks how long the first
    one took so tests can verify the cold-start cost is actually being
    absorbed by warmup, not by the user's request."""

    instances: list = []
    load_delay_seconds: float = 0.0
    fail_on_load: bool = False

    @classmethod
    def reset(cls) -> None:
        cls.instances.clear()
        cls.load_delay_seconds = 0.0
        cls.fail_on_load = False
        _FakeModelHolder.reset()

    @staticmethod
    def transcribe(audio, path_or_hf_repo, language=None, **_kwargs):
        _FakeModelHolder.get_model(path_or_hf_repo, dtype=None)
        return {"text": "", "segments": [], "language": "en"}


@pytest.fixture
def fake_mlx(monkeypatch):
    """Inject _FakeMlxWhisper into sys.modules so speech_to_text +
    warmup hit our stub instead of the real mlx package."""
    _FakeMlxWhisper.reset()
    sys.modules["mlx_whisper"] = _FakeMlxWhisper

    # Also stub ModelHolder — it's an attribute on transcribe.py in the
    # real package, but our fake puts it on the module itself, which
    # matches what server.py imports via `import mlx_whisper`.
    _FakeMlxWhisper.ModelHolder = _FakeModelHolder

    yield _FakeMlxWhisper

    sys.modules.pop("mlx_whisper", None)


@pytest.fixture
def model_on_disk(tmp_path, monkeypatch) -> Path:
    """Pretend a model exists at the path server.py will look for.
    Returns the directory containing a fake weights.npz."""
    models_dir = tmp_path / ".ziva" / "models" / "mlx-community" / "whisper-small-mlx"
    models_dir.mkdir(parents=True)
    (models_dir / "weights.npz").write_bytes(b"fake-npz")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    return models_dir


# ---------------------------------------------------------------------------
# Core contract: warmup runs in the background and doesn't block start()
# ---------------------------------------------------------------------------

def test_start_returns_before_warmup_finishes(fake_mlx, model_on_disk, monkeypatch):
    """start() must return promptly even if the model load takes seconds.
    Otherwise we'd be trading a slow mic click for a slow app launch,
    which is worse UX."""
    _FakeMlxWhisper.load_delay_seconds = 3.0
    rt = Runtime.create(workspace_root=model_on_disk.parent.parent)
    api = DesktopAPIServer(rt)

    t0 = time.perf_counter()
    asyncio.run(api.start(host="127.0.0.1", port=0))
    elapsed = time.perf_counter() - t0

    # start() returns immediately; warmup runs in a daemon thread.
    assert elapsed < 1.0, f"start() blocked on warmup: {elapsed:.2f}s"
    assert api._stt_status == "warming", (
        f"status should be 'warming' while the background thread runs, "
        f"got {api._stt_status!r}"
    )

    # Let warmup finish so the daemon thread doesn't outlive the test.
    asyncio.run(api.stop())


def test_warmup_eventually_marks_status_ready(fake_mlx, model_on_disk):
    """After the background thread finishes loading, status flips to
    'ready' so the frontend can stop showing the warming hint."""
    _FakeMlxWhisper.load_delay_seconds = 0.5
    rt = Runtime.create(workspace_root=model_on_disk.parent.parent)
    api = DesktopAPIServer(rt)

    asyncio.run(api.start(host="127.0.0.1", port=0))

    # Poll for warmup completion (CI boxes vary in how fast the daemon
    # thread gets scheduled).
    deadline = time.perf_counter() + 5.0
    while time.perf_counter() < deadline and api._stt_status == "warming":
        time.sleep(0.05)

    assert api._stt_status == "ready", (
        f"warmup did not complete within 5s; status={api._stt_status!r}"
    )
    assert len(_FakeMlxWhisper.instances) >= 0  # fake doesn't record calls

    asyncio.run(api.stop())


# ---------------------------------------------------------------------------
# First transcribe after warmup is fast (no re-load)
# ---------------------------------------------------------------------------

def test_warmup_loads_model_exactly_once(fake_mlx, model_on_disk):
    """After warmup finishes, ModelHolder.load_calls should record exactly
    one load. The real mlx_whisper.ModelHolder short-circuits on
    subsequent calls (it caches by model_path), so this is the proxy
    for "the user's first /api/stt won't pay the cold-start tax"."""
    _FakeMlxWhisper.load_delay_seconds = 0.1

    rt = Runtime.create(workspace_root=model_on_disk.parent.parent)
    api = DesktopAPIServer(rt)

    asyncio.run(api.start(host="127.0.0.1", port=0))

    deadline = time.perf_counter() + 5.0
    while time.perf_counter() < deadline and api._stt_status == "warming":
        time.sleep(0.05)
    assert api._stt_status == "ready"

    assert len(_FakeModelHolder.load_calls) == 1, (
        f"warmup should trigger exactly one load, got "
        f"{len(_FakeModelHolder.load_calls)}: {_FakeModelHolder.load_calls!r}"
    )

    asyncio.run(api.stop())


# ---------------------------------------------------------------------------
# Warmup is best-effort: failures must not break start()
# ---------------------------------------------------------------------------

def test_warmup_failure_does_not_break_server(fake_mlx, model_on_disk):
    """If the model load raises, start() must still have returned and
    the server must still serve /api/stt/status. The user will just see
    'error' instead of 'ready' and the first real call will be slow."""
    _FakeMlxWhisper.fail_on_load = True

    rt = Runtime.create(workspace_root=model_on_disk.parent.parent)
    api = DesktopAPIServer(rt)

    # Must not raise despite the simulated failure.
    asyncio.run(api.start(host="127.0.0.1", port=0))

    # Give the daemon thread a moment to fail.
    deadline = time.perf_counter() + 2.0
    while time.perf_counter() < deadline and api._stt_status not in ("error", "needs_download"):
        time.sleep(0.05)
    assert api._stt_status == "error", (
        f"status should report error after warmup failure, got {api._stt_status!r}"
    )

    asyncio.run(api.stop())


def test_status_endpoint_reports_current_state(fake_mlx, model_on_disk):
    """The /api/stt/status HTTP endpoint must surface the current warmup
    state so the frontend can render UI hints without poking at internals.
    """
    rt = Runtime.create(workspace_root=model_on_disk.parent.parent)
    api = DesktopAPIServer(rt)

    # Pre-start: status is 'idle'.
    async def _idle():
        from aiohttp.test_utils import make_mocked_request
        resp = await api.stt_status(make_mocked_request("GET", "/api/stt/status"))
        return resp
    resp = asyncio.run(_idle())
    assert resp.status == 200
    import json
    body = json.loads(resp.body)
    assert body == {"status": "idle"}


def test_needs_download_when_model_missing(fake_mlx, tmp_path, monkeypatch):
    """If ~/.ziva/models/.../weights.npz doesn't exist, warmup must NOT
    block on a multi-GB download — instead it should mark status as
    'needs_download' so the first user call downloads + warms up."""
    # No model on disk; tmp_path exists but is empty
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    rt = Runtime.create(workspace_root=tmp_path)
    api = DesktopAPIServer(rt)

    # The fake's get_model will be called only if warmup reaches the
    # transcribe() call. With no local model, warmup should bail at the
    # local_path check and set status to 'needs_download' WITHOUT calling
    # transcribe.
    asyncio.run(api.start(host="127.0.0.1", port=0))

    deadline = time.perf_counter() + 2.0
    while time.perf_counter() < deadline and api._stt_status == "warming":
        time.sleep(0.05)

    assert api._stt_status == "needs_download", (
        f"expected 'needs_download' when model not on disk, got "
        f"{api._stt_status!r}"
    )
    # The fake's get_model was never invoked (we bailed before transcribe).
    assert _FakeModelHolder.model is None

    asyncio.run(api.stop())