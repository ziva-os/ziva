"""Background STT model warmup.

The mlx-whisper STT model (whisper-small-mlx, ~461 MB) needs a cold start
of 10–15 s the first time ``mlx_whisper.transcribe`` is called: it loads
the weights, JIT-compiles the Metal kernels, and runs inference on a
placeholder audio. If we wait for the user's first mic click to trigger
that cost, voice input looks broken for the first 20 s of the app's
lifetime.

We solve this by kicking off the warmup in a daemon thread as early as
possible — ``app.cli`` starts it right after the ``Runtime`` is
constructed, before ``DesktopAPIServer`` is even instantiated — so the
cost overlaps with the rest of Electron's app-launch work.

The warmup state is exposed via a tiny shared module so the running
``DesktopAPIServer`` (which owns the ``/api/stt/status`` endpoint) can
report the same state the warmup thread is updating.

Public surface:

- :data:`stt_status` — current status string. One of ``"idle"``,
  ``"warming"``, ``"ready"``, ``"needs_download"``, ``"error"``.
- :func:`start_stt_warmup` — kick off the daemon thread. Idempotent
  (a second call while a warmup is in flight is a no-op).
- :func:`wait_stt_ready` — block until status is ``"ready"``, ``"error"``,
  or ``"needs_download"``. Useful for tests / diagnostics.
"""
from __future__ import annotations

import logging
import os
import tempfile
import threading
import time
import wave
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ziva.runtime import Runtime

logger = logging.getLogger("ziva.stt_warmup")

# Shared, thread-safe state read by DesktopAPIServer.stt_status.
# A plain string is fine — Python's GIL makes single-attribute writes
# atomic, and consumers only need an eventually-consistent snapshot.
stt_status: str = "idle"
_stt_lock = threading.Lock()
_stt_thread: threading.Thread | None = None


def start_stt_warmup(runtime: "Runtime") -> threading.Thread:
    """Start the STT warmup thread (idempotent).

    Returns the spawned (or already-existing) thread. The thread is a
    daemon, so it dies with the process — no cleanup is needed.
    """
    global _stt_thread, stt_status

    with _stt_lock:
        if _stt_thread is not None and _stt_thread.is_alive():
            logger.debug("STT warmup already running, skipping duplicate kickoff")
            return _stt_thread
        if stt_status == "ready":
            logger.debug("STT model already warm, skipping warmup")
            # Spawn nothing — return a finished-looking dummy.
            return _stt_thread  # type: ignore[return-value]

        stt_status = "warming"
        thread = threading.Thread(
            target=_warmup_stt,
            args=(runtime,),
            name="stt-warmup",
            daemon=True,
        )
        _stt_thread = thread
        thread.start()
        return thread


def wait_stt_ready(timeout: float = 60.0) -> str:
    """Block until warmup reaches a terminal state. Returns the status.

    Used by tests and diagnostic tooling — the request handlers don't
    block on this; they simply respond with whatever :data:`stt_status`
    currently is, and let the frontend poll / retry.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        if stt_status in ("ready", "error", "needs_download"):
            return stt_status
        time.sleep(0.05)
    return stt_status


def _warmup_stt(runtime: "Runtime") -> None:
    """Load the STT model in the background so the first user
    transcription is fast. Exceptions are logged but never re-raised.

    Mirrors the model-resolution logic in
    ``DesktopAPIServer.speech_to_text`` so we warm up the same model
    the user will hit. If the model isn't on disk yet (will be
    downloaded on first real use), this is a no-op — we'd rather not
    block startup on a multi-GB download.
    """
    global stt_status

    try:
        # Resolve the same model path speech_to_text would use.
        models_dir = Path.home() / ".ziva" / "models"
        stt_model = runtime.config.get("stt", {}).get(
            "model", "whisper-small-mlx"
        )
        local_path = None
        for candidate in [models_dir / stt_model, models_dir / "mlx-community" / stt_model]:
            if candidate.exists() and (candidate / "weights.npz").exists():
                local_path = candidate
                break
        if local_path is None:
            # Model not downloaded yet — let the first real request
            # trigger the download + warmup. That's the only way to
            # know what the user actually wants.
            stt_status = "needs_download"
            logger.info("STT model not on disk; first /api/stt will download + warm up")
            return

        # Imports here (not at module top) because mlx_whisper pulls
        # in a heavy Metal/native stack that's only meaningful when
        # we're actually going to run STT.
        import imageio_ffmpeg
        ffmpeg_dir = str(Path(imageio_ffmpeg.get_ffmpeg_exe()).parent)
        os.environ["PATH"] = ffmpeg_dir + os.pathsep + os.environ.get("PATH", "")

        import mlx_whisper  # noqa: F401  — import alone warms Metal/native stack
        # Force the model load + decoder warmup by transcribing a
        # 1-second silent wav. This populates ModelHolder.model and
        # JIT-compiles the Metal kernels.
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            with wave.open(tmp, "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(16000)
                w.writeframes(b"\x00\x00" * 16000)  # 1s silence
            tmp_path = tmp.name
        try:
            mlx_whisper.transcribe(
                tmp_path,
                path_or_hf_repo=str(local_path),
                language=None,
            )
            stt_status = "ready"
            logger.info("STT model warmup complete (%.2fs)", time.time())
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
    except Exception as exc:
        # Warmup is best-effort. Log so it shows up in backend.log
        # (Electron's main.ts pipes stderr there) but don't crash.
        logger.warning("STT warmup failed (first /api/stt will be slow): %s", exc)
        stt_status = "error"