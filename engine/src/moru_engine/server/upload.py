"""Web-platform upload client for the upload job.

Talks to the moru.gg web API (contracts in moru-app/contracts/web-api.yaml):
presigned-slot request, archive PUT, and pack registration. Module-level
coroutines (same pattern as live_models.py) so tests can monkeypatch each
step of the sequence independently. ``api_token`` is forwarded as a
Bearer header; the web platform rejects unauthenticated uploads (401).
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import aiohttp

if TYPE_CHECKING:
    from pathlib import Path

#: Metadata calls (slot request / registration) are small JSON round-trips.
_API_TIMEOUT = aiohttp.ClientTimeout(total=30)
#: The archive PUT streams the whole zip; generous for slow uplinks.
_PUT_TIMEOUT = aiohttp.ClientTimeout(total=600)


class WebUploadError(Exception):
    """The web platform rejected an upload step (HTTP error status)."""


def _auth_headers(api_token: str | None) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_token}"} if api_token else {}


async def _ensure_ok(resp: aiohttp.ClientResponse, step: str) -> None:
    """Raise WebUploadError for 4xx/5xx, surfacing the body's error message."""
    if resp.status < 400:
        return
    try:
        body = await resp.text()
    except Exception:  # noqa: BLE001 — body is best-effort diagnostics
        body = ""
    detail = body
    try:
        parsed = json.loads(body)
    except ValueError:
        pass
    else:
        if isinstance(parsed, dict):
            detail = str(parsed.get("error") or parsed.get("detail") or "")
    detail = detail.strip()[:300]
    message = f"{step} failed: HTTP {resp.status}"
    raise WebUploadError(f"{message} - {detail}" if detail else message)


async def request_upload_slots(
    web_url: str, api_token: str | None, size: int, sha256: str
) -> dict[str, Any]:
    """POST /api/upload-url; return the resource_pack slot {url, object_key}."""
    async with aiohttp.ClientSession(timeout=_API_TIMEOUT) as session:
        async with session.post(
            f"{web_url}/api/upload-url",
            json={
                "files": [
                    {"kind": "resource_pack", "size": size, "sha256": sha256}
                ]
            },
            headers=_auth_headers(api_token),
        ) as resp:
            await _ensure_ok(resp, "upload slot request")
            payload = await resp.json()
    uploads = payload.get("uploads") or []
    slot = next(
        (u for u in uploads if u.get("kind") == "resource_pack"), None
    )
    if not slot or not slot.get("url") or not slot.get("object_key"):
        raise WebUploadError(
            "upload slot request returned no usable resource_pack slot"
        )
    return slot


async def put_archive(url: str, zip_path: Path) -> None:
    """PUT the zip to the presigned URL, streaming the file from disk."""
    size = zip_path.stat().st_size
    async with aiohttp.ClientSession(timeout=_PUT_TIMEOUT) as session:
        with zip_path.open("rb") as fh:
            async with session.put(
                url,
                data=fh,
                headers={
                    "Content-Type": "application/zip",
                    "Content-Length": str(size),
                },
            ) as resp:
                await _ensure_ok(resp, "archive upload")


async def register_pack(
    web_url: str, api_token: str | None, payload: dict[str, Any]
) -> dict[str, Any]:
    """POST /api/translations (TranslationPackCreate); return {pack_id, url}."""
    async with aiohttp.ClientSession(timeout=_API_TIMEOUT) as session:
        async with session.post(
            f"{web_url}/api/translations",
            json=payload,
            headers=_auth_headers(api_token),
        ) as resp:
            await _ensure_ok(resp, "pack registration")
            return await resp.json()
