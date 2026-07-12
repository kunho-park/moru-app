"""Tests for the FastAPI sidecar (moru_engine.server).

Translate jobs are never run here (they need an LLM); scan jobs run for
real against the repo fixture at test/modpack, which is fast and LLM-free.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
import uuid
import zipfile
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from moru_engine import __version__
from moru_engine.pipeline import PipelineConfig, PipelineResult, PipelineStats
from moru_engine.server import create_app
from moru_engine.server.jobs import JobRecord, JobStatus, JobType
from moru_engine.server.live_models import fetch_live_models
from moru_engine.server.upload import WebUploadError

if TYPE_CHECKING:
    from collections.abc import Iterator

TOKEN = "test-session-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
MODPACK = Path(__file__).resolve().parents[1] / "test" / "modpack"


@pytest.fixture(scope="module")
def shutdown_flag() -> threading.Event:
    return threading.Event()


@pytest.fixture(scope="module")
def client(
    tmp_path_factory: pytest.TempPathFactory, shutdown_flag: threading.Event
) -> Iterator[TestClient]:
    root = tmp_path_factory.mktemp("server")
    app = create_app(
        token=TOKEN,
        config_dir=root / "config",
        tm_db_path=root / "tm.sqlite3",
        shutdown_handler=shutdown_flag.set,
        shutdown_delay=0.0,
    )
    # Context manager keeps one event loop (anyio portal) alive across
    # requests, so background job tasks actually run between polls.
    with TestClient(app) as test_client:
        yield test_client


def _wait_for_job(
    client: TestClient, job_id: str, timeout: float = 60.0
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = client.get(f"/jobs/{job_id}", headers=AUTH)
        assert response.status_code == 200
        body = response.json()
        if body["status"] in {"done", "failed", "cancelled"}:
            return body
        time.sleep(0.05)
    pytest.fail(f"job {job_id} did not finish within {timeout}s")


@pytest.fixture(scope="module")
def scan_job(client: TestClient) -> dict[str, Any]:
    """A real, completed scan job over the repo fixture modpack."""
    assert MODPACK.is_dir(), f"missing fixture: {MODPACK}"
    response = client.post(
        "/jobs",
        json={"type": "scan", "params": {"modpack_path": str(MODPACK)}},
        headers=AUTH,
    )
    assert response.status_code == 201, response.text
    job = response.json()
    assert job["type"] == "scan"
    assert job["status"] in {"pending", "running"}
    assert job["created_at"]
    final = _wait_for_job(client, job["id"])
    assert final["status"] == "done", final
    return final


# -- health and auth ---------------------------------------------------------


def test_health_requires_no_auth(client: TestClient) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "version": __version__}


def test_missing_token_is_401(client: TestClient) -> None:
    assert client.get("/providers").status_code == 401
    assert client.get("/config").status_code == 401
    assert client.post("/jobs", json={"type": "scan", "params": {}}).status_code == 401


def test_wrong_token_is_401(client: TestClient) -> None:
    response = client.get(
        "/providers", headers={"Authorization": "Bearer wrong-token"}
    )
    assert response.status_code == 401


def test_valid_token_is_200(client: TestClient) -> None:
    response = client.get("/providers", headers=AUTH)
    assert response.status_code == 200
    providers = {p["id"]: p for p in response.json()}
    assert {"openai", "anthropic", "ollama"} <= providers.keys()
    assert providers["ollama"]["has_key"] is True
    for provider in providers.values():
        assert provider["models"], provider["id"]
        assert isinstance(provider["has_key"], bool)


# -- jobs: scan flow -----------------------------------------------------------


def test_scan_job_completes(scan_job: dict[str, Any]) -> None:
    assert scan_job["status"] == "done"
    assert scan_job["error"] is None


def test_scan_result_category_tree(
    client: TestClient, scan_job: dict[str, Any]
) -> None:
    response = client.get(f"/scan/{scan_job['id']}/result", headers=AUTH)
    assert response.status_code == 200
    body = response.json()
    assert body["modpack_path"] == str(MODPACK)
    categories = body["categories"]
    assert isinstance(categories, list) and categories
    names = {c["name"] for c in categories}
    assert "KubeJS" in names
    for category in categories:
        assert set(category) == {
            "name",
            "handler",
            "file_count",
            "entry_count",
            "char_count",
            "files",
        }
        assert category["file_count"] == len(category["files"]) > 0
        assert category["entry_count"] == sum(
            f["entry_count"] for f in category["files"]
        )
        for file_info in category["files"]:
            assert file_info["path"]
            assert set(file_info) == {"path", "entry_count", "char_count", "sample"}
    # The parse pass counts real entries: the fixture modpack has content.
    assert sum(c["entry_count"] for c in categories) > 0
    assert sum(c["char_count"] for c in categories) > 0


def test_scan_counts_only_entries_missing_target_locale(
    client: TestClient, scan_job: dict[str, Any]
) -> None:
    body = client.get(f"/scan/{scan_job['id']}/result", headers=AUTH).json()
    kubejs = next(category for category in body["categories"] if category["name"] == "KubeJS")
    file_info = next(
        file
        for file in kubejs["files"]
        if Path(file["path"]).name.lower() == "en_us.json"
    )
    source_path = Path(file_info["path"])
    target_path = source_path.with_name("ko_kr.json")
    source = json.loads(source_path.read_text(encoding="utf-8"))
    existing = json.loads(target_path.read_text(encoding="utf-8"))
    pending = {
        key: value
        for key, value in source.items()
        if not str(existing.get(key, "")).strip()
    }

    assert file_info["entry_count"] == len(pending)
    assert file_info["entry_count"] < len(source)
    assert file_info["char_count"] == sum(len(value) for value in pending.values())
    assert file_info["sample"] == {
        key: value[:160] for key, value in list(pending.items())[:3]
    }


def test_scan_result_samples_are_bounded(
    client: TestClient, scan_job: dict[str, Any]
) -> None:
    body = client.get(f"/scan/{scan_job['id']}/result", headers=AUTH).json()
    saw_sample = False
    for category in body["categories"]:
        for file_info in category["files"]:
            sample = file_info["sample"]
            assert len(sample) <= 3
            for value in sample.values():
                assert len(value) <= 160
            if sample:
                saw_sample = True
    assert saw_sample, "no file produced a sample"


def test_scan_ws_emits_parse_stage(
    client: TestClient, scan_job: dict[str, Any]
) -> None:
    frames: list[dict[str, Any]] = []
    with client.websocket_connect(
        f"/jobs/{scan_job['id']}/events?token={TOKEN}"
    ) as ws:
        while True:
            frame = ws.receive_json()
            frames.append(frame)
            if frame["type"] in {"done", "failed", "cancelled"}:
                break
    stages = {f.get("stage") for f in frames if f["type"] == "progress"}
    assert "parse" in stages


def test_scan_result_of_unknown_job_is_404(client: TestClient) -> None:
    assert client.get("/scan/nope/result", headers=AUTH).status_code == 404


def test_job_status_of_unknown_job_is_404(client: TestClient) -> None:
    assert client.get("/jobs/nope", headers=AUTH).status_code == 404


def test_cancel_unknown_job_is_404(client: TestClient) -> None:
    assert client.post("/jobs/nope/cancel", headers=AUTH).status_code == 404


def test_scan_job_missing_modpack_path_is_422(client: TestClient) -> None:
    response = client.post(
        "/jobs", json={"type": "scan", "params": {}}, headers=AUTH
    )
    assert response.status_code == 422


def test_export_requires_completed_translate_job(
    client: TestClient, scan_job: dict[str, Any]
) -> None:
    # Unknown translate job -> 404.
    response = client.post(
        "/jobs",
        json={"type": "export", "params": {"translate_job_id": "nope"}},
        headers=AUTH,
    )
    assert response.status_code == 404
    # A done *scan* job is the wrong kind of source -> 409.
    response = client.post(
        "/jobs",
        json={"type": "export", "params": {"translate_job_id": scan_job["id"]}},
        headers=AUTH,
    )
    assert response.status_code == 409


# -- jobs: upload flow ---------------------------------------------------------


@pytest.fixture
def done_translate_job(client: TestClient, tmp_path: Path) -> JobRecord:
    """A completed translate job injected straight into the manager.

    Running a real translate needs an LLM, so build the JobRecord by hand
    (the shape _run leaves behind) and register it in manager._jobs.
    """
    output_dir = tmp_path / "out"
    lang_file = (
        output_dir / "resourcepack" / "assets" / "somemod" / "lang" / "ko_kr.json"
    )
    lang_file.parent.mkdir(parents=True)
    lang_file.write_text('{"key.hello": "안녕"}', encoding="utf-8")
    mcmeta = output_dir / "resourcepack" / "pack.mcmeta"
    mcmeta.write_text('{"pack": {"pack_format": 15}}', encoding="utf-8")
    override_file = (
        output_dir / "overrides" / "kubejs" / "assets" / "test" / "lang" / "ko_kr.json"
    )
    override_file.parent.mkdir(parents=True)
    override_file.write_text('{"gui.done": "완료"}', encoding="utf-8")
    result = PipelineResult(
        config=PipelineConfig(
            modpack_path=tmp_path / "modpack",
            output_dir=output_dir,
            source_locale="en_us",
            target_locale="ko_kr",
            model="openai/gpt-4o-mini",
        ),
        output_files=[lang_file, override_file, mcmeta],
        stats=PipelineStats(
            total_entries=10,
            translated_entries=8,
            failed_entries=1,
            tm_hits=1,
            duration_seconds=12.5,
        ),
    )
    result.stats.finalize()  # coverage 90.0, quality 0.9
    record = JobRecord(
        id=f"translate-{uuid.uuid4()}",
        type=JobType.TRANSLATE,
        params={"modpack_path": str(tmp_path / "modpack")},
        status=JobStatus.DONE,
        result=result,
        finished=True,
    )
    client.app.state.job_manager._jobs[record.id] = record
    return record


@pytest.fixture
def upload_stubs(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Stub the three web calls in moru_engine.server.upload, recording args."""
    calls: dict[str, Any] = {}

    async def fake_slots(
        web_url: str, api_token: str | None, size: int, sha256: str
    ) -> dict[str, Any]:
        calls["slots"] = {
            "web_url": web_url,
            "api_token": api_token,
            "size": size,
            "sha256": sha256,
        }
        return {
            "kind": "resource_pack",
            "url": "https://r2.test/put/abc",
            "object_key": "packs/abc.zip",
        }

    async def fake_put(url: str, zip_path: Path) -> None:
        with zipfile.ZipFile(zip_path) as zf:
            names = zf.namelist()
        calls["put"] = {"url": url, "names": names}

    async def fake_register(
        web_url: str, api_token: str | None, payload: dict[str, Any]
    ) -> dict[str, Any]:
        calls["register"] = {
            "web_url": web_url,
            "api_token": api_token,
            "payload": payload,
        }
        return {"pack_id": "pk_123", "url": f"{web_url}/packs/pk_123"}

    monkeypatch.setattr(
        "moru_engine.server.upload.request_upload_slots", fake_slots
    )
    monkeypatch.setattr("moru_engine.server.upload.put_archive", fake_put)
    monkeypatch.setattr("moru_engine.server.upload.register_pack", fake_register)
    return calls


def _start_upload(
    client: TestClient, params: dict[str, Any]
) -> "Any":  # httpx.Response
    return client.post(
        "/jobs", json={"type": "upload", "params": params}, headers=AUTH
    )


def _terminal_frame(client: TestClient, job_id: str) -> dict[str, Any]:
    with client.websocket_connect(f"/jobs/{job_id}/events?token={TOKEN}") as ws:
        while True:
            frame = ws.receive_json()
            if frame["type"] in {"done", "failed", "cancelled"}:
                return frame


def test_upload_job_success_with_token(
    client: TestClient,
    done_translate_job: JobRecord,
    upload_stubs: dict[str, Any],
) -> None:
    response = _start_upload(
        client,
        {
            "translate_job_id": done_translate_job.id,
            "modpack_name": "ATM 10",
            "modpack_version": "1.2.3",
            "web_url": "https://web.test",
            "api_token": "desktop-api-token",
        },
    )
    assert response.status_code == 201
    job_id = response.json()["id"]
    final = _wait_for_job(client, job_id)
    assert final["status"] == "done", final["error"]

    frame = _terminal_frame(client, job_id)
    assert frame["pack_id"] == "pk_123"
    assert frame["url"] == "https://web.test/packs/pk_123"

    # The bearer token reaches both authenticated web calls.
    assert upload_stubs["slots"]["api_token"] == "desktop-api-token"
    assert upload_stubs["register"]["api_token"] == "desktop-api-token"
    assert upload_stubs["slots"]["web_url"] == "https://web.test"
    assert upload_stubs["slots"]["size"] > 0
    assert len(upload_stubs["slots"]["sha256"]) == 64

    # The PUT streams one combined zip of both output trees.
    assert upload_stubs["put"]["url"] == "https://r2.test/put/abc"
    assert set(upload_stubs["put"]["names"]) == {
        "resourcepack/assets/somemod/lang/ko_kr.json",
        "resourcepack/pack.mcmeta",
        "overrides/kubejs/assets/test/lang/ko_kr.json",
    }

    # TranslationPackCreate payload mapped from the pipeline result.
    payload = upload_stubs["register"]["payload"]
    assert payload["modpack_name"] == "ATM 10"
    assert payload["modpack_version"] == "1.2.3"
    assert payload["target_lang"] == "ko_kr"
    assert payload["source_lang"] == "en_us"
    assert payload["engine_version"] == __version__
    assert payload["files"] == [
        {"kind": "resource_pack", "object_key": "packs/abc.zip"}
    ]
    assert payload["stats"] == {
        "total_entries": 10,
        "translated_entries": 8,
        "failed_entries": 1,
        "coverage_percent": 90.0,
        "quality_score": 0.9,
        "tm_hits": 1,
        "model": "openai/gpt-4o-mini",
        "duration_seconds": 12.5,
    }


def test_export_job_builds_pack_and_overrides_zips(
    client: TestClient, done_translate_job: JobRecord, tmp_path: Path
) -> None:
    target = tmp_path / "exports" / "pack.zip"
    response = client.post(
        "/jobs",
        json={
            "type": "export",
            "params": {
                "translate_job_id": done_translate_job.id,
                "output_zip": str(target),
            },
        },
        headers=AUTH,
    )
    assert response.status_code == 201
    final = _wait_for_job(client, response.json()["id"])
    assert final["status"] == "done", final["error"]

    frame = _terminal_frame(client, final["id"])
    assert frame["zip_path"] == str(target)
    overrides_zip = target.with_name("pack_overrides.zip")
    assert frame["overrides_zip_path"] == str(overrides_zip)

    # Resource pack zip is installable as-is: mcmeta at the archive root.
    with zipfile.ZipFile(target) as zf:
        assert set(zf.namelist()) == {
            "pack.mcmeta",
            "assets/somemod/lang/ko_kr.json",
        }
    # Overrides zip mirrors the modpack root.
    with zipfile.ZipFile(overrides_zip) as zf:
        assert zf.namelist() == ["kubejs/assets/test/lang/ko_kr.json"]


def test_cancelled_translate_result_can_be_reviewed_and_exported(
    client: TestClient, done_translate_job: JobRecord, tmp_path: Path
) -> None:
    done_translate_job.status = JobStatus.CANCELLED

    review = client.get(
        f"/translate/{done_translate_job.id}/entries",
        headers=AUTH,
    )
    assert review.status_code == 200
    assert review.json()["total"] == len(done_translate_job.result.entries)

    target = tmp_path / "exports" / "partial.zip"
    response = client.post(
        "/jobs",
        json={
            "type": "export",
            "params": {
                "translate_job_id": done_translate_job.id,
                "output_zip": str(target),
            },
        },
        headers=AUTH,
    )
    assert response.status_code == 201
    final = _wait_for_job(client, response.json()["id"])
    assert final["status"] == "done", final["error"]
    assert target.exists()
    assert target.with_name("partial_overrides.zip").exists()


def test_upload_job_without_token_uses_defaults(
    client: TestClient,
    done_translate_job: JobRecord,
    upload_stubs: dict[str, Any],
) -> None:
    response = _start_upload(
        client,
        {"translate_job_id": done_translate_job.id, "modpack_name": "ATM 10"},
    )
    assert response.status_code == 201
    final = _wait_for_job(client, response.json()["id"])
    assert final["status"] == "done", final["error"]

    frame = _terminal_frame(client, final["id"])
    assert frame["url"] == "https://moru.gg/packs/pk_123"
    # No token -> no Authorization header is attached and the default
    # web_url is used (the real web platform rejects such calls with 401).
    assert upload_stubs["slots"]["api_token"] is None
    assert upload_stubs["register"]["api_token"] is None
    assert upload_stubs["slots"]["web_url"] == "https://moru.gg"


def test_upload_job_missing_params_is_422(client: TestClient) -> None:
    response = _start_upload(client, {"modpack_name": "X"})
    assert response.status_code == 422
    assert "translate_job_id" in response.json()["detail"]

    response = _start_upload(client, {"translate_job_id": "whatever"})
    assert response.status_code == 422
    assert "modpack_name" in response.json()["detail"]


def test_upload_job_unknown_translate_job_is_404(client: TestClient) -> None:
    response = _start_upload(
        client, {"translate_job_id": "nope", "modpack_name": "X"}
    )
    assert response.status_code == 404


def test_upload_requires_completed_translate_job(
    client: TestClient, scan_job: dict[str, Any]
) -> None:
    # A scan job is not a valid translate source -> 409.
    response = _start_upload(
        client, {"translate_job_id": scan_job["id"], "modpack_name": "X"}
    )
    assert response.status_code == 409

    # A translate job that has not finished yet -> 409.
    record = JobRecord(
        id=f"translate-{uuid.uuid4()}",
        type=JobType.TRANSLATE,
        params={},
        status=JobStatus.RUNNING,
    )
    client.app.state.job_manager._jobs[record.id] = record
    response = _start_upload(
        client, {"translate_job_id": record.id, "modpack_name": "X"}
    )
    assert response.status_code == 409
    assert "upload requires a completed translate job" in response.json()["detail"]


def test_upload_job_web_failure_marks_job_failed(
    client: TestClient,
    done_translate_job: JobRecord,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def failing_slots(
        web_url: str, api_token: str | None, size: int, sha256: str
    ) -> dict[str, Any]:
        raise WebUploadError(
            "upload slot request failed: HTTP 503 - storage down"
        )

    monkeypatch.setattr(
        "moru_engine.server.upload.request_upload_slots", failing_slots
    )
    response = _start_upload(
        client,
        {"translate_job_id": done_translate_job.id, "modpack_name": "ATM 10"},
    )
    assert response.status_code == 201
    final = _wait_for_job(client, response.json()["id"])
    assert final["status"] == "failed"
    assert "HTTP 503" in final["error"]
    assert "storage down" in final["error"]


# -- websocket events ------------------------------------------------------------


def test_ws_replays_history_for_finished_job(
    client: TestClient, scan_job: dict[str, Any]
) -> None:
    frames: list[dict[str, Any]] = []
    with client.websocket_connect(
        f"/jobs/{scan_job['id']}/events?token={TOKEN}"
    ) as ws:
        with pytest.raises(WebSocketDisconnect) as exc_info:
            while True:
                frames.append(ws.receive_json())
        assert exc_info.value.code == 1000
    assert frames, "expected replayed events for a finished job"
    assert all("type" in frame for frame in frames)
    terminal = frames[-1]
    assert terminal["type"] == "done"
    assert terminal["status"] == "done"
    # The terminal frame is the only terminal-typed frame in the stream.
    assert [f for f in frames if f["type"] in {"done", "failed", "cancelled"}] == [
        terminal
    ]


def test_ws_rejects_bad_token(
    client: TestClient, scan_job: dict[str, Any]
) -> None:
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(
            f"/jobs/{scan_job['id']}/events?token=wrong"
        ):
            pass


def test_ws_unknown_job_closes(client: TestClient) -> None:
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(f"/jobs/nope/events?token={TOKEN}"):
            pass


# -- translate review endpoints ----------------------------------------------------


def test_patch_entry_unknown_job_is_404(client: TestClient) -> None:
    response = client.patch(
        "/translate/nope/entries/some.key",
        json={"translated_text": "x"},
        headers=AUTH,
    )
    assert response.status_code == 404


def test_entries_unknown_job_is_404(client: TestClient) -> None:
    response = client.get("/translate/nope/entries", headers=AUTH)
    assert response.status_code == 404


def test_entries_on_scan_job_is_404(
    client: TestClient, scan_job: dict[str, Any]
) -> None:
    response = client.get(f"/translate/{scan_job['id']}/entries", headers=AUTH)
    assert response.status_code == 404


def test_retranslate_unknown_job_is_404(client: TestClient) -> None:
    response = client.post(
        "/translate/nope/entries/some.key/retranslate", headers=AUTH
    )
    assert response.status_code == 404


def test_retranslate_on_scan_job_is_404(
    client: TestClient, scan_job: dict[str, Any]
) -> None:
    response = client.post(
        f"/translate/{scan_job['id']}/entries/some.key/retranslate",
        headers=AUTH,
    )
    assert response.status_code == 404


# -- glossary / tm / config ----------------------------------------------------------


def test_glossary_empty_then_roundtrip(client: TestClient) -> None:
    params = {"source_lang": "en_us", "target_lang": "ko_kr"}
    response = client.get("/glossary", params=params, headers=AUTH)
    assert response.status_code == 200
    assert response.json() == {
        "source_lang": "en_us",
        "target_lang": "ko_kr",
        "terms": [],
    }

    doc = {
        "source_lang": "en_us",
        "target_lang": "ko_kr",
        "terms": [{"source": "Creeper", "target": "크리퍼"}],
    }
    response = client.put("/glossary", json=doc, headers=AUTH)
    assert response.status_code == 200

    response = client.get("/glossary", params=params, headers=AUTH)
    assert response.status_code == 200
    body = response.json()
    assert body["terms"] == [
        {"source": "Creeper", "target": "크리퍼", "origin": "manual"}
    ]


def test_glossary_rejects_bad_locale(client: TestClient) -> None:
    response = client.get(
        "/glossary",
        params={"source_lang": "../evil", "target_lang": "ko_kr"},
        headers=AUTH,
    )
    assert response.status_code == 422


def test_tm_stats_empty_db(client: TestClient) -> None:
    response = client.get("/tm/stats", headers=AUTH)
    assert response.status_code == 200
    body = response.json()
    assert body["entries"] == 0
    assert body["hits"] == 0
    assert body["last_sync_version"] is None


def test_config_roundtrip(client: TestClient) -> None:
    response = client.get("/config", headers=AUTH)
    assert response.status_code == 200
    assert response.json() == {}

    payload = {"theme": "dark", "nested": {"batch_size": 30, "flag": True}}
    response = client.put("/config", json=payload, headers=AUTH)
    assert response.status_code == 200

    response = client.get("/config", headers=AUTH)
    assert response.status_code == 200
    assert response.json() == payload


# -- providers ----------------------------------------------------------------------


def test_providers_include_openrouter(client: TestClient) -> None:
    body = client.get("/providers", headers=AUTH).json()
    openrouter = next(p for p in body if p["id"] == "openrouter")
    assert openrouter["name"] == "OpenRouter"
    assert openrouter["models"]
    assert all(m.startswith("openrouter/") for m in openrouter["models"])


def test_provider_models_requires_token(client: TestClient) -> None:
    response = client.post("/providers/models", json={"provider": "openai"})
    assert response.status_code == 401


def test_provider_models_unknown_provider_is_404(client: TestClient) -> None:
    response = client.post(
        "/providers/models", headers=AUTH, json={"provider": "nope"}
    )
    assert response.status_code == 404


def test_provider_models_live_success(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_fetch(
        provider: str, *, api_key: str | None = None, api_base: str | None = None
    ) -> list[str]:
        assert (provider, api_key) == ("openai", "sk-test")
        return ["openai/gpt-4o-mini", "openai/gpt-4.1"]

    monkeypatch.setattr("moru_engine.server.app.fetch_live_models", fake_fetch)
    response = client.post(
        "/providers/models",
        headers=AUTH,
        json={"provider": "openai", "api_key": "sk-test"},
    )
    assert response.status_code == 200
    assert response.json() == {
        "provider": "openai",
        "models": ["openai/gpt-4o-mini", "openai/gpt-4.1"],
        "source": "live",
        "error": None,
    }


def test_provider_models_falls_back_to_static_on_error(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_fetch(
        provider: str, *, api_key: str | None = None, api_base: str | None = None
    ) -> list[str]:
        raise ValueError("api key required")

    monkeypatch.setattr("moru_engine.server.app.fetch_live_models", fake_fetch)
    response = client.post(
        "/providers/models", headers=AUTH, json={"provider": "anthropic"}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["source"] == "static"
    assert body["error"] == "api key required"
    catalog = client.get("/providers", headers=AUTH).json()
    anthropic = next(p for p in catalog if p["id"] == "anthropic")
    assert body["models"] == anthropic["models"]


def test_provider_models_falls_back_to_static_on_empty(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_fetch(
        provider: str, *, api_key: str | None = None, api_base: str | None = None
    ) -> list[str]:
        return []

    monkeypatch.setattr("moru_engine.server.app.fetch_live_models", fake_fetch)
    response = client.post(
        "/providers/models", headers=AUTH, json={"provider": "ollama"}
    )
    body = response.json()
    assert body["source"] == "static"
    assert body["error"] == "provider returned no models"
    assert all(m.startswith("ollama_chat/") for m in body["models"])


def test_fetch_live_models_rejects_missing_key_and_unknown_provider() -> None:
    for provider in ("openai", "anthropic", "gemini", "deepseek", "xai"):
        with pytest.raises(ValueError, match="api key required"):
            asyncio.run(fetch_live_models(provider))
    with pytest.raises(ValueError, match="unknown provider"):
        asyncio.run(fetch_live_models("nope"))


# -- shutdown -----------------------------------------------------------------------


def test_shutdown_schedules_handler(
    client: TestClient, shutdown_flag: threading.Event
) -> None:
    response = client.post("/shutdown", headers=AUTH)
    assert response.status_code == 202
    assert shutdown_flag.wait(timeout=2.0), "shutdown handler was not invoked"
