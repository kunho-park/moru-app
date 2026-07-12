"""Local translation memory backed by SQLite.

Exact-match cache: a hit skips the LLM call entirely.
The cache key is ``sha256(source_text, target_lang, glossary_version)`` so any
glossary change naturally invalidates prior translations.

Thread-safety: a single connection is opened with ``check_same_thread=False``
and every database access is serialized through an internal
``threading.Lock``. All methods are synchronous; the pipeline wraps calls with
``asyncio.to_thread`` when needed.
"""

from __future__ import annotations

import hashlib
import logging
import sqlite3
import threading
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType

from platformdirs import user_data_dir
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

_KEY_SEPARATOR = "\x1f"

#: ``tm_meta`` key holding the version of the last merged shared TM snapshot.
META_LAST_SHARED_VERSION = "last_shared_version"

#: ``glossary_version`` sentinel for community rows: they are approved
#: against no particular local glossary, so lookups probe this version in
#: addition to the run's own fingerprint.
SHARED_GLOSSARY_VERSION = "shared"

# Stay well below SQLite's host-parameter limit when binding IN (...) clauses.
_MAX_BATCH_PARAMS = 500

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tm_entries (
    key_hash TEXT PRIMARY KEY,
    source_text TEXT NOT NULL,
    target_lang TEXT NOT NULL,
    glossary_version TEXT NOT NULL,
    translated_text TEXT NOT NULL,
    origin TEXT NOT NULL DEFAULT 'local',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tm_entries_target_lang ON tm_entries(target_lang);
CREATE TABLE IF NOT EXISTS tm_meta (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""

_UPSERT_SQL = """
INSERT INTO tm_entries (
    key_hash, source_text, target_lang, glossary_version,
    translated_text, origin, created_at, updated_at
)
VALUES (?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(key_hash) DO UPDATE SET
    translated_text = excluded.translated_text,
    origin = excluded.origin,
    updated_at = excluded.updated_at
"""


def tm_key(source_text: str, target_lang: str, glossary_version: str) -> str:
    """Compute the deterministic TM entry key.

    ``sha256`` over the three components joined by the ASCII unit separator,
    so no plausible text content can collide across fields.
    """
    raw = _KEY_SEPARATOR.join((source_text, target_lang, glossary_version))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def default_db_path() -> Path:
    """Per-user default location of the local TM database."""
    return Path(user_data_dir("moru", "moru")) / "tm.sqlite3"


class TMStats(BaseModel):
    """Aggregate statistics of the local translation memory."""

    total_entries: int = 0
    by_origin: dict[str, int] = Field(default_factory=dict)
    last_shared_version: str | None = None


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


class LocalTM:
    """Exact-match local translation memory over a single SQLite file.

    One connection is shared across threads (``check_same_thread=False``);
    ``self._lock`` serializes every access, which is sufficient because all
    operations are short-lived point reads/writes.
    """

    def __init__(self, db_path: Path | None = None) -> None:
        self._db_path = db_path if db_path is not None else default_db_path()
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self._db_path, check_same_thread=False)
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.executescript(_SCHEMA)
            self._conn.commit()
        logger.debug("LocalTM opened at %s", self._db_path)

    @property
    def db_path(self) -> Path:
        return self._db_path

    def lookup(
        self, source_text: str, target_lang: str, glossary_version: str
    ) -> str | None:
        """Cached translation for an exact match, else None.

        Checks the run's own glossary fingerprint AND the community
        ``shared`` sentinel; the shared row wins (human-approved).
        """
        hits = self.lookup_many({"k": source_text}, target_lang, glossary_version)
        return hits.get("k")

    def lookup_many(
        self,
        entries: Mapping[str, str],
        target_lang: str,
        glossary_version: str,
    ) -> dict[str, str]:
        """Batch lookup: ``{entry_key: source_text}`` -> ``{entry_key: translated_text}``.

        Only hits appear in the result; misses are simply absent. Entry keys
        sharing the same source text all receive the same hit. Every source
        is probed under both the run's glossary fingerprint and the
        community ``shared`` sentinel version; a shared hit overrides the
        local one because community rows are human-approved corrections.
        """
        if not entries:
            return {}
        local_hash_to_keys: dict[str, list[str]] = {}
        shared_hash_to_keys: dict[str, list[str]] = {}
        for entry_key, source_text in entries.items():
            local = tm_key(source_text, target_lang, glossary_version)
            local_hash_to_keys.setdefault(local, []).append(entry_key)
            if glossary_version != SHARED_GLOSSARY_VERSION:
                shared = tm_key(source_text, target_lang, SHARED_GLOSSARY_VERSION)
                shared_hash_to_keys.setdefault(shared, []).append(entry_key)

        hits: dict[str, str] = {}
        # Local first so shared rows overwrite on collision.
        for hash_to_keys in (local_hash_to_keys, shared_hash_to_keys):
            hashes = list(hash_to_keys)
            with self._lock:
                for start in range(0, len(hashes), _MAX_BATCH_PARAMS):
                    chunk = hashes[start : start + _MAX_BATCH_PARAMS]
                    marks = ",".join("?" * len(chunk))
                    rows = self._conn.execute(
                        "SELECT key_hash, translated_text FROM tm_entries "
                        f"WHERE key_hash IN ({marks})",
                        chunk,
                    ).fetchall()
                    for key_hash, translated_text in rows:
                        for entry_key in hash_to_keys[key_hash]:
                            hits[entry_key] = translated_text
        return hits

    def store(
        self,
        source_text: str,
        target_lang: str,
        glossary_version: str,
        translated_text: str,
        origin: str = "local",
    ) -> None:
        """Insert or update one entry; ``updated_at`` is bumped on conflict."""
        now = _utc_now_iso()
        key_hash = tm_key(source_text, target_lang, glossary_version)
        with self._lock:
            self._conn.execute(
                _UPSERT_SQL,
                (
                    key_hash,
                    source_text,
                    target_lang,
                    glossary_version,
                    translated_text,
                    origin,
                    now,
                    now,
                ),
            )
            self._conn.commit()

    def store_many(
        self,
        items: Iterable[tuple[str, str]],
        target_lang: str,
        glossary_version: str,
        origin: str = "local",
    ) -> None:
        """Upsert ``(source_text, translated_text)`` pairs in one transaction."""
        now = _utc_now_iso()
        params = [
            (
                tm_key(source_text, target_lang, glossary_version),
                source_text,
                target_lang,
                glossary_version,
                translated_text,
                origin,
                now,
                now,
            )
            for source_text, translated_text in items
        ]
        if not params:
            return
        with self._lock:
            self._conn.executemany(_UPSERT_SQL, params)
            self._conn.commit()

    def stats(self) -> TMStats:
        """Aggregate entry counts and the last merged shared snapshot version."""
        with self._lock:
            total = self._conn.execute(
                "SELECT COUNT(*) FROM tm_entries"
            ).fetchone()[0]
            origin_rows = self._conn.execute(
                "SELECT origin, COUNT(*) FROM tm_entries GROUP BY origin"
            ).fetchall()
            version_row = self._conn.execute(
                "SELECT value FROM tm_meta WHERE key = ?",
                (META_LAST_SHARED_VERSION,),
            ).fetchone()
        return TMStats(
            total_entries=total,
            by_origin=dict(origin_rows),
            last_shared_version=version_row[0] if version_row is not None else None,
        )

    def get_meta(self, key: str) -> str | None:
        """Read a bookkeeping value from ``tm_meta``."""
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM tm_meta WHERE key = ?", (key,)
            ).fetchone()
        return row[0] if row is not None else None

    def set_meta(self, key: str, value: str) -> None:
        """Write a bookkeeping value to ``tm_meta`` (upsert)."""
        with self._lock:
            self._conn.execute(
                "INSERT INTO tm_meta (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )
            self._conn.commit()

    def close(self) -> None:
        """Close the underlying connection; safe to call more than once."""
        with self._lock:
            self._conn.close()

    def __enter__(self) -> LocalTM:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()
