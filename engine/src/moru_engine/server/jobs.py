"""Async job manager backing the sidecar's /jobs API.

The desktop GUI starts long-running work (scan / translate / export)
through ``POST /jobs`` and follows it over a WebSocket. Every job keeps a
full event history, so a subscriber that connects after events already
fired first replays the past, then streams live frames.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import shutil
import tempfile
import uuid
import zipfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any

from platformdirs import user_data_dir
from pydantic import ValidationError

from .. import __version__
from ..handlers.base import create_default_registry
from ..output import (
    OVERRIDES_DIRNAME,
    RESOURCEPACK_DIRNAME,
    create_zip_from_directory,
)
from ..pipeline import (
    PipelineConfig,
    PipelineResult,
    apply_entry_edits,
    output_root,
    run_pipeline,
)
from ..scanner import ScanResult, scan_modpack
from ..scanner.pack_identity import PackIdentity, detect_pack_identity
from . import upload

if TYPE_CHECKING:
    from collections.abc import Coroutine, Mapping

logger = logging.getLogger(__name__)

#: Concurrent handler.extract() calls during the scan parse pass.
PARSE_CONCURRENCY = 8
#: Sample entries included per file in the enriched scan payload.
SAMPLE_ENTRIES = 3
#: Sample values are truncated to this many characters.
SAMPLE_TEXT_LIMIT = 160


@dataclass
class FileParseMeta:
    """Per-source-file volumes from the scan parse pass."""

    entry_count: int = 0
    char_count: int = 0
    sample: dict[str, str] = field(default_factory=dict)


@dataclass
class EnrichedScanResult:
    """Scan discovery plus parsed entry volumes (desktop scan-result screen).

    ``files`` is keyed by ``str(pair.source_path)``; files whose handler
    failed to parse stay at zero counts rather than failing the scan.
    ``identity`` is the launcher-metadata match used to prefill the
    upload form (contracts/engine-api.yaml ScanResult.identity).
    """

    scan: ScanResult
    identity: PackIdentity | None = None
    files: dict[str, FileParseMeta] = field(default_factory=dict)


#: Event types that end a job's stream; the manager emits exactly one of
#: these as the final frame of every job.
TERMINAL_EVENT_TYPES = frozenset({"done", "failed", "cancelled"})


class JobError(Exception):
    """Base class for job-manager errors mapped to HTTP responses."""


class UnknownJobError(JobError):
    """Referenced job id does not exist (HTTP 404)."""


class JobStateError(JobError):
    """Job exists but is in the wrong state for the operation (HTTP 409)."""


class JobParamsError(JobError):
    """Job parameters failed validation (HTTP 422)."""


class JobType(str, Enum):
    SCAN = "scan"
    TRANSLATE = "translate"
    EXPORT = "export"
    UPLOAD = "upload"


class JobStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class JobRecord:
    """One job: contract-facing fields plus execution internals."""

    id: str
    type: JobType
    params: dict[str, Any]
    status: JobStatus = JobStatus.PENDING
    error: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    #: Stored outcome: EnrichedScanResult (scan), PipelineResult (translate),
    #: dict with zip metadata (export). Preserved on cancellation.
    result: object | None = None
    #: Extra payload merged into the terminal ``done`` frame (e.g. pipeline
    #: stats, export zip path).
    done_payload: dict[str, Any] | None = None

    cancel_requested: bool = False
    finished: bool = False
    history: list[dict[str, Any]] = field(default_factory=list)
    subscribers: set[asyncio.Queue[dict[str, Any] | None]] = field(
        default_factory=set
    )
    task: asyncio.Task[None] | None = None

    def to_public(self) -> dict[str, Any]:
        """Contract ``Job`` schema representation."""
        return {
            "id": self.id,
            "type": self.type.value,
            "status": self.status.value,
            "error": self.error,
            "created_at": self.created_at.isoformat(),
        }


def _default_export_dir() -> Path:
    return Path(user_data_dir("moru", "moru")) / "exports"


_FILENAME_UNSAFE_RE = re.compile(r'[\\/:*?"<>|\x00-\x1f]+')


def _export_stem(result: PipelineResult, identity: PackIdentity) -> str:
    """Filename stem for the export zips.

    The resource-pack UI titles a zip pack by its filename, so
    "All the Mods 10 2.32 한국어 (moru)" beats a job UUID.
    """
    locale = result.config.target_locale
    parts = [
        identity.name or result.config.modpack_path.name,
        identity.version,
        "한국어" if locale == "ko_kr" else locale,
        "(moru)",
    ]
    stem = _FILENAME_UNSAFE_RE.sub(" ", " ".join(p for p in parts if p))
    return " ".join(stem.split()) or "moru-pack"


class JobManager:
    """Creates, runs, cancels, and streams jobs on the server event loop."""

    def __init__(self, glossary_store_dir: Path | None = None) -> None:
        self._jobs: dict[str, JobRecord] = {}
        self._loop: asyncio.AbstractEventLoop | None = None
        #: Injected by the app so pipeline runs read the same user glossary
        #: store (hub manual terms + synced community terms) the HTTP
        #: endpoints write.
        self._glossary_store_dir = glossary_store_dir

    # -- lookup --------------------------------------------------------------

    def get(self, job_id: str) -> JobRecord:
        try:
            return self._jobs[job_id]
        except KeyError:
            raise UnknownJobError(f"unknown job: {job_id}") from None

    # -- creation ------------------------------------------------------------

    def create_job(self, type_: str, params: Mapping[str, Any]) -> JobRecord:
        """Validate params, register the job, and start it as a task.

        Must be called from within the running event loop (route handlers).
        """
        job_type = JobType(type_)
        record = JobRecord(
            id=str(uuid.uuid4()), type=job_type, params=dict(params)
        )
        runner: Coroutine[Any, Any, object]
        if job_type is JobType.SCAN:
            self._require_modpack_path(record.params)
            runner = self._run_scan(record)
        elif job_type is JobType.TRANSLATE:
            self._require_modpack_path(record.params)
            try:
                config = PipelineConfig(**record.params)
            except ValidationError as exc:
                raise JobParamsError(f"invalid translate params: {exc}") from exc
            if config.glossary_store_dir is None:
                config.glossary_store_dir = self._glossary_store_dir
            runner = self._run_translate(record, config)
        elif job_type is JobType.EXPORT:
            source = self._resolve_translate_source(record.params, "export")
            runner = self._run_export(record, source)
        else:  # JobType.UPLOAD
            if not record.params.get("modpack_name"):
                raise JobParamsError("params.modpack_name is required")
            curseforge_id = record.params.get("curseforge_id")
            if curseforge_id is not None and (
                isinstance(curseforge_id, bool)
                or not isinstance(curseforge_id, int)
                or curseforge_id <= 0
            ):
                raise JobParamsError(
                    "params.curseforge_id must be a positive integer"
                )
            source = self._resolve_translate_source(record.params, "upload")
            runner = self._run_upload(record, source)

        self._jobs[record.id] = record
        record.task = asyncio.create_task(
            self._run(record, runner), name=f"moru-job-{record.id}"
        )
        return record

    @staticmethod
    def _require_modpack_path(params: Mapping[str, Any]) -> None:
        path = params.get("modpack_path")
        if not path:
            raise JobParamsError("params.modpack_path is required")
        if not Path(str(path)).exists():
            raise JobParamsError(f"modpack_path does not exist: {path}")

    def _resolve_translate_source(
        self, params: Mapping[str, Any], purpose: str
    ) -> JobRecord:
        """Resolve a completed or user-cancelled translate job with results."""
        translate_job_id = params.get("translate_job_id")
        if not translate_job_id:
            raise JobParamsError("params.translate_job_id is required")
        source = self.get(str(translate_job_id))
        if source.type is not JobType.TRANSLATE:
            raise JobStateError(
                f"job {source.id} is a {source.type.value} job, not translate"
            )
        if source.status not in (JobStatus.DONE, JobStatus.CANCELLED) or not isinstance(
            source.result, PipelineResult
        ):
            raise JobStateError(
                f"translate job {source.id} is {source.status.value}; "
                f"{purpose} requires a completed translate job"
            )
        return source

    # -- cancellation ----------------------------------------------------------

    def cancel(self, job_id: str) -> JobRecord:
        """Request cancellation; partial results stay on the record."""
        record = self.get(job_id)
        record.cancel_requested = True
        if record.task is not None and not record.task.done():
            record.task.cancel()
        return record

    async def aclose(self) -> None:
        """Cancel every running job (server shutdown)."""
        tasks = [
            r.task
            for r in self._jobs.values()
            if r.task is not None and not r.task.done()
        ]
        for record in self._jobs.values():
            record.cancel_requested = True
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    # -- event stream ----------------------------------------------------------

    def subscribe(
        self, job_id: str
    ) -> tuple[list[dict[str, Any]], asyncio.Queue[dict[str, Any] | None] | None]:
        """Return (history snapshot, live queue).

        The queue is None when the job already finished: history then ends
        with the terminal frame and there is nothing live to wait for.
        Registration and snapshot happen in one synchronous step on the
        event loop thread, so no frame can fall between them.
        """
        record = self.get(job_id)
        history = list(record.history)
        if record.finished:
            return history, None
        queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
        record.subscribers.add(queue)
        return history, queue

    def unsubscribe(
        self, job_id: str, queue: asyncio.Queue[dict[str, Any] | None]
    ) -> None:
        record = self._jobs.get(job_id)
        if record is not None:
            record.subscribers.discard(queue)

    def _emit(
        self, record: JobRecord, event_type: str, payload: Mapping[str, object]
    ) -> None:
        """Append a frame to history and fan it out to live subscribers.

        Pipeline/scanner ``on_event`` callbacks fire synchronously from
        coroutines running on this process's single event loop, so touching
        the history list and calling ``Queue.put_nowait`` directly is safe:
        we are already on the loop thread and nothing else can interleave.
        ``asyncio.Queue`` is *not* thread-safe, though, so if a callback ever
        arrives from a worker thread (code under ``asyncio.to_thread``), we
        hop back onto the loop with ``call_soon_threadsafe`` instead.
        """
        frame: dict[str, Any] = {"type": event_type, **payload}
        loop = self._loop
        try:
            running: asyncio.AbstractEventLoop | None = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if loop is None or running is loop:
            self._deliver(record, frame)
        else:
            loop.call_soon_threadsafe(self._deliver, record, frame)

    @staticmethod
    def _deliver(record: JobRecord, frame: dict[str, Any]) -> None:
        record.history.append(frame)
        for queue in record.subscribers:
            queue.put_nowait(frame)

    # -- execution ---------------------------------------------------------------

    async def _run(
        self, record: JobRecord, runner: Coroutine[Any, Any, object]
    ) -> None:
        self._loop = asyncio.get_running_loop()
        record.status = JobStatus.RUNNING
        terminal_type: str
        terminal: dict[str, Any]
        try:
            result = await runner
        except asyncio.CancelledError:
            # Both task.cancel() and the pipeline's cancel_check land here.
            record.status = JobStatus.CANCELLED
            terminal_type, terminal = "cancelled", {"status": "cancelled"}
            logger.info("Job %s (%s) cancelled", record.id, record.type.value)
        except Exception as exc:
            record.status = JobStatus.FAILED
            record.error = str(exc)
            terminal_type = "failed"
            terminal = {"status": "failed", "error": str(exc)}
            logger.exception("Job %s (%s) failed", record.id, record.type.value)
        else:
            record.result = result
            if record.cancel_requested:
                record.status = JobStatus.CANCELLED
                terminal_type, terminal = "cancelled", {"status": "cancelled"}
            else:
                record.status = JobStatus.DONE
                terminal_type, terminal = "done", {"status": "done"}
            if record.done_payload:
                terminal.update(record.done_payload)
        self._emit(record, terminal_type, terminal)
        record.finished = True
        for queue in record.subscribers:
            queue.put_nowait(None)  # stream-end sentinel for live listeners

    async def _run_scan(self, record: JobRecord) -> EnrichedScanResult:
        params = record.params

        def progress(stage: str, current: int, total: int, message: str) -> None:
            self._emit(
                record,
                "progress",
                {
                    "stage": stage,
                    "current": current,
                    "total": total,
                    "message": message,
                },
            )

        scan = await scan_modpack(
            params["modpack_path"],
            source_locale=str(params.get("source_locale", "en_us")),
            target_locale=str(params.get("target_locale", "ko_kr")),
            progress_callback=progress,
        )

        # Launcher metadata (CurseForge/Modrinth/Prism files) tells us which
        # published pack this folder is; a handful of fixed-path reads, run
        # off-loop so a slow disk never stalls event delivery.
        identity = await asyncio.to_thread(
            detect_pack_identity, Path(str(params["modpack_path"]))
        )

        # Parse pass: entry/char counts + samples per source file, so the
        # scan screen can show real volumes and cost estimates before any
        # LLM call. Parse failures degrade to zero counts, never fail scan.
        registry = create_default_registry()
        pairs = scan.all_translation_pairs
        enriched = EnrichedScanResult(scan=scan, identity=identity)
        semaphore = asyncio.Semaphore(PARSE_CONCURRENCY)
        parsed_count = 0

        async def parse_one(source_path: Path) -> None:
            nonlocal parsed_count
            meta = FileParseMeta()
            async with semaphore:
                handler = registry.get_handler(source_path)
                if handler is not None:
                    try:
                        data = await handler.extract(source_path)
                        meta.entry_count = len(data)
                        meta.char_count = sum(len(v) for v in data.values())
                        meta.sample = {
                            k: v[:SAMPLE_TEXT_LIMIT]
                            for k, v in list(data.items())[:SAMPLE_ENTRIES]
                        }
                    except Exception as exc:  # noqa: BLE001 — parse-only pass
                        logger.warning("Scan parse failed for %s: %s", source_path, exc)
            enriched.files[str(source_path)] = meta
            parsed_count += 1
            progress("parse", parsed_count, len(pairs), source_path.name)

        if pairs:
            progress("parse", 0, len(pairs), "")
            await asyncio.gather(*(parse_one(p.source_path) for p in pairs))
        return enriched

    async def _run_translate(
        self, record: JobRecord, config: PipelineConfig
    ) -> PipelineResult:
        def on_event(event: str, payload: dict[str, object]) -> None:
            if event == "done":
                # The pipeline's own "done" {stats} would collide with the
                # manager's terminal frame; stash it so the terminal
                # {type: done, status: done} carries the stats instead.
                record.done_payload = dict(payload)
                return
            self._emit(record, event, payload)

        def cancel_check() -> bool:
            return record.cancel_requested

        result = await run_pipeline(
            config, on_event=on_event, cancel_check=cancel_check
        )
        # Normal and partial-cancelled results expose the same cumulative
        # counters to the desktop terminal frame.
        record.done_payload = {"stats": result.stats.model_dump()}
        return result

    async def _apply_review_edits(
        self, source: JobRecord, stage: str
    ) -> PipelineResult:
        """Fold review-screen edits (manual PATCH / AI retranslate) back
        into the output trees so export/upload archives carry the
        reviewed state."""
        result = source.result
        assert isinstance(result, PipelineResult)  # _resolve_translate_source
        rewritten = await apply_entry_edits(result)
        if rewritten:
            logger.info(
                "%s: re-applied review edits to %d files", stage, rewritten
            )
        return result

    async def _build_reviewed_zip(
        self, record: JobRecord, source: JobRecord, zip_path: Path, stage: str
    ) -> tuple[int, int]:
        """One archive of the whole output root (resourcepack/ + overrides/).

        Used by the web upload; the local export builds per-tree zips in
        :meth:`_run_export` instead. Emits {stage, current, total} progress
        around the blocking build and returns (files written, total).
        """
        result = await self._apply_review_edits(source, stage)
        files = [Path(p) for p in result.output_files]
        base = output_root(result.config)

        self._emit(
            record,
            "progress",
            {"stage": stage, "current": 0, "total": len(files)},
        )

        def build() -> int:
            zip_path.parent.mkdir(parents=True, exist_ok=True)
            written = 0
            with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
                for file in files:
                    if not file.exists():
                        logger.warning("%s: missing output file %s", stage, file)
                        continue
                    try:
                        arcname = file.relative_to(base)
                    except ValueError:
                        arcname = Path(file.name)
                    zf.write(file, arcname=str(arcname))
                    written += 1
            return written

        # Zip construction is blocking I/O; keep the loop responsive.
        written = await asyncio.to_thread(build)
        self._emit(
            record,
            "progress",
            {"stage": stage, "current": written, "total": len(files)},
        )
        return written, len(files)

    async def _run_export(
        self, record: JobRecord, source: JobRecord
    ) -> dict[str, Any]:
        """Build the installable archives from the generated output trees.

        Two artifacts: ``<name>.zip`` — the resource pack, droppable into
        the game's ``resourcepacks/`` folder as-is — and
        ``<name>_overrides.zip`` — files to merge over the modpack root
        (kubejs/, config/, ftbquests/ …). Either is null when the run
        produced no files for that tree.
        """
        result = await self._apply_review_edits(source, "export")
        root = output_root(result.config)
        pack_dir = root / RESOURCEPACK_DIRNAME
        overrides_dir = root / OVERRIDES_DIRNAME

        output_zip = record.params.get("output_zip")
        if output_zip:
            zip_path = Path(str(output_zip))
        else:
            # Off-loop like the scan: identity probes launcher JSON on disk.
            identity = await asyncio.to_thread(
                detect_pack_identity, result.config.modpack_path
            )
            zip_path = (
                _default_export_dir() / f"{_export_stem(result, identity)}.zip"
            )
        overrides_zip = zip_path.with_name(f"{zip_path.stem}_overrides.zip")

        self._emit(
            record,
            "progress",
            {"stage": "export", "current": 0, "total": 2},
        )

        def has_files(tree: Path) -> bool:
            return tree.is_dir() and any(p.is_file() for p in tree.rglob("*"))

        def build() -> dict[str, Any]:
            payload: dict[str, Any] = {
                "zip_path": None,
                "overrides_zip_path": None,
                "file_count": len(result.output_files),
            }
            if has_files(pack_dir):
                create_zip_from_directory(pack_dir, zip_path)
                payload["zip_path"] = str(zip_path)
            if has_files(overrides_dir):
                create_zip_from_directory(overrides_dir, overrides_zip)
                payload["overrides_zip_path"] = str(overrides_zip)
            return payload

        # Zip construction is blocking I/O; keep the loop responsive.
        payload = await asyncio.to_thread(build)
        self._emit(
            record,
            "progress",
            {"stage": "export", "current": 2, "total": 2},
        )
        record.done_payload = dict(payload)
        return payload

    async def _run_upload(
        self, record: JobRecord, source: JobRecord
    ) -> dict[str, Any]:
        """Publish a completed translate job's pack to the moru web platform.

        Sequence (contracts/web-api.yaml): build the reviewed zip, request a
        presigned upload slot, PUT the archive, then register the pack. The
        api_token is forwarded as a Bearer header on every call; the web
        platform rejects unauthenticated uploads with 401.
        """
        params = record.params
        web_url = str(params.get("web_url") or "https://moru.gg").rstrip("/")
        api_token = params.get("api_token") or None

        staging = Path(tempfile.mkdtemp(prefix="moru-upload-"))
        try:
            zip_path = staging / f"{source.id}.zip"
            await self._build_reviewed_zip(record, source, zip_path, "pack")

            def digest() -> tuple[str, int]:
                sha = hashlib.sha256()
                with zip_path.open("rb") as fh:
                    for chunk in iter(lambda: fh.read(1 << 20), b""):
                        sha.update(chunk)
                return sha.hexdigest(), zip_path.stat().st_size

            sha256, size = await asyncio.to_thread(digest)

            def step(current: int, message: str) -> None:
                self._emit(
                    record,
                    "progress",
                    {
                        "stage": "upload",
                        "current": current,
                        "total": 3,
                        "message": message,
                    },
                )

            step(0, "requesting upload slot")
            slot = await upload.request_upload_slots(
                web_url, api_token, size, sha256
            )
            step(1, "uploading archive")
            await upload.put_archive(str(slot["url"]), zip_path)
            step(2, "registering pack")
            registered = await upload.register_pack(
                web_url, api_token, self._pack_payload(record, source, slot)
            )
            step(3, "registered")
        finally:
            shutil.rmtree(staging, ignore_errors=True)

        payload = {
            "pack_id": str(registered["pack_id"]),
            "url": str(registered["url"]),
        }
        record.done_payload = dict(payload)
        return payload

    @staticmethod
    def _pack_payload(
        record: JobRecord, source: JobRecord, slot: Mapping[str, Any]
    ) -> dict[str, Any]:
        """web-api.yaml TranslationPackCreate body from the translate result."""
        result = source.result
        assert isinstance(result, PipelineResult)  # _resolve_translate_source
        stats = result.stats
        params = record.params
        payload: dict[str, Any] = {
            "modpack_name": str(params["modpack_name"]),
            "target_lang": result.config.target_locale,
            "source_lang": result.config.source_locale,
            "files": [
                {"kind": "resource_pack", "object_key": slot["object_key"]}
            ],
            "engine_version": __version__,
            "stats": {
                "total_entries": stats.total_entries,
                "translated_entries": stats.translated_entries,
                "failed_entries": stats.failed_entries,
                "coverage_percent": stats.coverage_percent,
                "quality_score": stats.quality_score,
                "tm_hits": stats.tm_hits,
                "model": result.config.model,
                "duration_seconds": stats.duration_seconds,
            },
        }
        if result.artifact_id:
            payload["artifact_id"] = result.artifact_id
        for key in ("modpack_version", "description", "changelog"):
            value = params.get(key)
            if value:
                payload[key] = str(value)
        # Validated as a positive int in create_job; lets the web platform
        # link the pack to its CurseForge project page.
        curseforge_id = params.get("curseforge_id")
        if curseforge_id:
            payload["curseforge_id"] = int(curseforge_id)
        return payload

