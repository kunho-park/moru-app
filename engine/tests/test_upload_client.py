"""Error surfacing for the web-platform upload client.

The upload endpoints are rate limited, and an edge layer answers 429 with an
HTML page rather than the API's JSON. Both have to reach the user as something
actionable instead of raw markup.
"""

from __future__ import annotations

import json

import pytest

from moru_engine.server.upload import WebUploadError, _ensure_ok


class _Resp:
    """Minimal stand-in for the parts of aiohttp's response we read."""

    def __init__(
        self, status: int, body: str = "", headers: dict[str, str] | None = None
    ) -> None:
        self.status = status
        self.headers = headers or {}
        self._body = body

    async def text(self) -> str:
        return self._body


@pytest.mark.asyncio
async def test_success_status_raises_nothing() -> None:
    await _ensure_ok(_Resp(200, json.dumps({"ok": True})), "pack registration")


@pytest.mark.asyncio
async def test_json_error_body_is_surfaced() -> None:
    resp = _Resp(400, json.dumps({"error": "sha256 mismatch"}))

    with pytest.raises(WebUploadError) as excinfo:
        await _ensure_ok(resp, "upload slot request")

    assert "sha256 mismatch" in str(excinfo.value)
    assert "HTTP 400" in str(excinfo.value)


@pytest.mark.asyncio
async def test_html_body_is_dropped_instead_of_pasted() -> None:
    """An HTML error page must not become the user-facing message."""
    resp = _Resp(502, "<html><body><h1>502 Bad Gateway</h1></body></html>")

    with pytest.raises(WebUploadError) as excinfo:
        await _ensure_ok(resp, "pack registration")

    message = str(excinfo.value)
    assert "HTTP 502" in message
    assert "<html>" not in message
    assert "Bad Gateway" not in message


@pytest.mark.asyncio
async def test_rate_limit_reports_the_wait_from_retry_after() -> None:
    resp = _Resp(
        429,
        json.dumps({"error": "Too many requests, slow down"}),
        {"Retry-After": "1800"},
    )

    with pytest.raises(WebUploadError) as excinfo:
        await _ensure_ok(resp, "upload slot request")

    message = str(excinfo.value)
    assert "한도" in message
    assert "30분" in message


@pytest.mark.asyncio
async def test_rate_limit_without_retry_after_still_reads_as_rate_limited() -> None:
    """The 429 an edge layer emits carries an HTML body and no usable header."""
    resp = _Resp(429, "<html><body>Too Many Requests</body></html>")

    with pytest.raises(WebUploadError) as excinfo:
        await _ensure_ok(resp, "upload slot request")

    message = str(excinfo.value)
    assert "한도" in message
    assert "잠시 후" in message
    assert "<html>" not in message


@pytest.mark.asyncio
async def test_expired_retry_after_falls_back_to_the_vague_wait() -> None:
    resp = _Resp(429, "", {"Retry-After": "0"})

    with pytest.raises(WebUploadError) as excinfo:
        await _ensure_ok(resp, "upload slot request")

    assert "잠시 후" in str(excinfo.value)


@pytest.mark.asyncio
async def test_retry_after_http_date_is_understood() -> None:
    """Retry-After allows an HTTP date, and edge layers use it."""
    from datetime import UTC, datetime, timedelta
    from email.utils import format_datetime

    when = format_datetime(datetime.now(UTC) + timedelta(minutes=10))
    resp = _Resp(429, "", {"Retry-After": when})

    with pytest.raises(WebUploadError) as excinfo:
        await _ensure_ok(resp, "upload slot request")

    assert "10분" in str(excinfo.value)
