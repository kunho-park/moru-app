"""Web-platform upload client: error surfacing and the published compat range.

The upload endpoints are rate limited, and an edge layer answers 429 with an
HTML page rather than the API's JSON. Both have to reach the user as something
actionable instead of raw markup. Registration additionally finalizes the
pack's compatible modpack version range, which must never publish a claim
that does not hold up.
"""

from __future__ import annotations

import json

import pytest

from moru_engine.scanner.pack_identity import VersionRange, declare_version_range
from moru_engine.server.upload import (
    WebUploadError,
    _compatible_versions,
    _ensure_ok,
    register_pack,
)


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


# -- published compatible version range ----------------------------------------


def test_registration_publishes_the_exact_version_by_default() -> None:
    """The default is a point range: today's behaviour, no new claim."""
    assert _compatible_versions({"modpack_version": "4.1.2"}) == {
        "min": "4.1.2",
        "max": "4.1.2",
    }
    # Nothing to anchor a range to -> no field, so the platform keeps
    # resolving the pack by exact version as it always has.
    assert _compatible_versions({}) is None
    assert _compatible_versions({"compatible_up_to": "4.2.0"}) is None


def test_registration_widens_only_with_a_bound_that_holds_up() -> None:
    assert _compatible_versions(
        {"modpack_version": "4.1.2", "compatible_up_to": "4.2.0"}
    ) == {"min": "4.1.2", "max": "4.2.0"}
    # Free text from the export form: unorderable, or below the built
    # version, is dropped rather than published.
    for bad in ("4.2.0-rc1", "4.0.0", "곧", ""):
        assert _compatible_versions(
            {"modpack_version": "4.1.2", "compatible_up_to": bad}
        ) == {"min": "4.1.2", "max": "4.1.2"}
    assert declare_version_range("4.1.2") == VersionRange(min="4.1.2", max="4.1.2")


async def test_register_pack_sends_the_range_and_never_the_raw_input(
    aiohttp_server: object,
) -> None:
    from aiohttp import web

    seen: dict[str, object] = {}

    async def handler(request: web.Request) -> web.Response:
        seen.update(await request.json())
        seen["client"] = request.headers.get("X-Moru-Client", "")
        return web.json_response({"pack_id": "p1", "url": "https://moru.gg/packs/p1"})

    app = web.Application()
    app.router.add_post("/api/translations", handler)
    server = await aiohttp_server(app)  # type: ignore[operator]

    registered = await register_pack(
        f"http://{server.host}:{server.port}",
        None,
        {
            "modpack_name": "Society Sunlit Valley",
            "modpack_version": "4.1.2",
            "compatible_up_to": "4.2.0",
            "target_lang": "ko_kr",
        },
    )

    assert registered["pack_id"] == "p1"
    assert seen["compatible_versions"] == {"min": "4.1.2", "max": "4.2.0"}
    # An engine-side job parameter, not part of the contract body.
    assert "compatible_up_to" not in seen
    assert seen["modpack_version"] == "4.1.2"
    assert str(seen["client"]).startswith("moru-engine/")
