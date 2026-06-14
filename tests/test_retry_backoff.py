"""Tests for the retry helper.

Covers Area 2 of the provider refactor:
- MAX_RETRIES = 2 (3 total attempts)
- retryable status codes {429, 500, 502, 503, 504, 529}
- sensitive-content errors (1027 / new_sensitive) are retryable
- Retry-After header is respected when present
- equal-jitter backoff grows exponentially
"""
from __future__ import annotations

import asyncio
from typing import Optional

import pytest

from ziva_runtime.adapters.retry import (
    MAX_RETRIES,
    RETRYABLE_STATUS,
    _backoff_delay,
    _is_retryable,
    _retry_after_ms,
    call_with_retry,
)


class FakeApiError(Exception):
    def __init__(self, msg: str, status_code: Optional[int] = None, response=None):
        super().__init__(msg)
        self.status_code = status_code
        self.response = response


def test_max_retries_is_two():
    assert MAX_RETRIES == 2


def test_retryable_status_codes():
    for code in (429, 500, 502, 503, 504, 529):
        assert _is_retryable(FakeApiError("err", status_code=code)), f"{code} should be retryable"
    for code in (400, 401, 403, 404, 422):
        assert not _is_retryable(FakeApiError("err", status_code=code)), f"{code} should NOT be retryable"


def test_sensitive_content_markers_are_retryable():
    for marker in ("1027", "new_sensitive", "1026", "input_sensitive"):
        assert _is_retryable(FakeApiError(f"err {marker}"))


def test_non_retryable_auth_error():
    assert not _is_retryable(FakeApiError("invalid_api_key", status_code=401))


def test_backoff_grows_exponentially():
    """Equal-jitter backoff: base * 2^attempt with 0.8..1.2 jitter.

    The jitter window is wide, so we check the floor (0.8 × base) grows.
    """
    err = FakeApiError("rate", status_code=429)
    floor_0 = 0.8 * 0.5  # attempt 0: 0.8 × (500ms / 1000)
    floor_1 = 0.8 * 1.0  # attempt 1: 0.8 × (1000ms / 1000)
    floor_2 = 0.8 * 2.0  # attempt 2: 0.8 × (2000ms / 1000)

    d0 = asyncio.run(_backoff_delay(0, err))
    d1 = asyncio.run(_backoff_delay(1, err))
    d2 = asyncio.run(_backoff_delay(2, err))

    assert d0 >= floor_0 * 0.99
    assert d1 >= floor_1 * 0.99
    assert d2 >= floor_2 * 0.99
    assert d0 < d1 < d2


def test_retry_after_header_overrides_backoff():
    """If the response has Retry-After, use that instead of exponential."""
    # Match the shape of httpx.Response: a `.headers` mapping
    class FakeResponse:
        def __init__(self, headers):
            self.headers = headers
    resp = FakeResponse({"retry-after": "2.5"})  # 2.5 seconds
    err = FakeApiError("rate", status_code=429, response=resp)
    delay = asyncio.run(_backoff_delay(0, err))
    assert delay == 2.5


def test_call_with_retry_succeeds_after_transient_failure():
    calls = {"n": 0}

    async def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise FakeApiError("503 busy", status_code=503)
        return "ok"

    result = asyncio.run(call_with_retry(flaky))
    assert result == "ok"
    assert calls["n"] == 3


def test_call_with_retry_raises_non_retryable_immediately():
    calls = {"n": 0}

    async def bad():
        calls["n"] += 1
        raise FakeApiError("bad request", status_code=400)

    with pytest.raises(FakeApiError):
        asyncio.run(call_with_retry(bad))
    assert calls["n"] == 1


def test_call_with_retry_exhausts_then_raises():
    calls = {"n": 0}

    async def always_503():
        calls["n"] += 1
        raise FakeApiError("always 503", status_code=503)

    with pytest.raises(FakeApiError):
        asyncio.run(call_with_retry(always_503))
    assert calls["n"] == MAX_RETRIES + 1


def test_call_with_retry_passes_args_and_kwargs():
    async def echo(a, b, c=None):
        return (a, b, c)

    result = asyncio.run(call_with_retry(echo, 1, 2, c=3))
    assert result == (1, 2, 3)
