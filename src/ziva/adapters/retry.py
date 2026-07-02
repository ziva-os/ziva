"""HTTP retry helpers for model adapter calls.

Design adapted from anomalyco/opencode:
- equal-jitter exponential backoff (0.8..1.2 × base × 2^attempt)
- MAX_RETRIES = 2 (so up to 3 total attempts)
- respect Retry-After header when present
- retry only on transient status codes and sensitive-content errors
"""
from __future__ import annotations

import asyncio
import random
from typing import Any, Awaitable, Callable, TypeVar

T = TypeVar("T")

MAX_RETRIES = 2
RETRYABLE_STATUS = {429, 500, 502, 503, 504, 529}
BASE_DELAY_MS = 500
MAX_DELAY_MS = 10_000

# Substrings that indicate a retryable upstream rejection — observed on
# OpenAI-compat providers (notably Anthropic-via-proxy and DeepSeek) when
# the model flags the prompt as sensitive. These are content-level errors
# that occasionally succeed on retry.
_RETRYABLE_CONTENT_MARKERS = (
    "1027", "new_sensitive", "1026", "input_sensitive",
)


def _is_retryable(exc: Exception) -> bool:
    status = getattr(exc, "status_code", None)
    if isinstance(status, int) and status in RETRYABLE_STATUS:
        return True
    msg = str(exc).lower()
    if any(marker in msg for marker in _RETRYABLE_CONTENT_MARKERS):
        return True
    # String-form status codes from SDKs that wrap HTTP errors as text
    for code in RETRYABLE_STATUS:
        if f" {code} " in f" {msg} " or f":{code}" in msg:
            return True
    return False


def _retry_after_ms(exc: Exception) -> int | None:
    """Extract Retry-After (ms) from an exception, if present."""
    resp = getattr(exc, "response", None)
    if resp is None:
        return None
    headers = getattr(resp, "headers", None) or {}
    for key, val in headers.items():
        if str(key).lower() == "retry-after":
            try:
                seconds = float(val)
                return int(seconds * 1000)
            except (TypeError, ValueError):
                return None
    return None


async def _backoff_delay(attempt: int, exc: Exception) -> float:
    hint_ms = _retry_after_ms(exc)
    if hint_ms is not None:
        return min(hint_ms / 1000.0, MAX_DELAY_MS / 1000.0)
    base = BASE_DELAY_MS * (2 ** attempt) / 1000.0
    return min(random.uniform(0.8, 1.2) * base, MAX_DELAY_MS / 1000.0)


async def call_with_retry(
    fn: Callable[..., Awaitable[T]],
    *args: Any,
    **kwargs: Any,
) -> T:
    """Call an async fn, retrying on retryable failures up to MAX_RETRIES."""
    last_exc: Exception | None = None
    for attempt in range(MAX_RETRIES + 1):
        try:
            return await fn(*args, **kwargs)
        except Exception as exc:
            last_exc = exc
            if attempt == MAX_RETRIES or not _is_retryable(exc):
                raise
            await asyncio.sleep(await _backoff_delay(attempt, exc))
    # Unreachable — the loop either returns or raises. Keeps type checkers calm.
    assert last_exc is not None
    raise last_exc
