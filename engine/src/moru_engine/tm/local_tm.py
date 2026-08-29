"""Local translation memory backed by SQLite.

Exact-match cache: a hit skips the LLM call entirely.
The cache key is ``sha256(source_text, target_lang, glossary_version)`` so any
glossary change naturally invalidates prior translations.

Both directions pass ``is_cacheable_pair``: a pair that carries no
translation (target equal to source, blank, or still holding protected
tokens) is refused on write AND never served on read, so rows written
before the gate existed are inert from the first run after upgrade
instead of needing a migration scan. ``purge_degenerate`` reclaims their
disk space when a caller asks for it.

Row precedence, resolved per entry key in ``lookup_many``: a row stored
under the run's own glossary fingerprint beats a community row stored
under the ``shared`` sentinel, and a community row is served only to lang
keys its ``key_scope`` covers.

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

from ..glossary.pair_harvester import is_untranslated_copy
from ..models.glossary import key_scope_covers
from ..placeholder import TOKEN_RE

logger = logging.getLogger(__name__)

_KEY_SEPARATOR = "\x1f"

#: ``tm_meta`` key holding the version of the last merged shared TM snapshot.
META_LAST_SHARED_VERSION = "last_shared_version"

#: ``key_scope`` patterns are stored joined by the ASCII unit separator: a
#: dotted lang-key glob can never contain one, and "" means unscoped.
_SCOPE_SEPARATOR = "\x1f"

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
    updated_at TEXT NOT NULL,
    key_scope TEXT NOT NULL DEFAULT ''
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
    translated_text, origin, created_at, updated_at, key_scope
)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(key_hash) DO UPDATE SET
    translated_text = excluded.translated_text,
    origin = excluded.origin,
    updated_at = excluded.updated_at,
    key_scope = excluded.key_scope
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


def is_cacheable_pair(source_text: str, translated_text: str) -> bool:
    """Whether a ``(source, translated)`` pair is usable as a cache entry.

    A cache entry is a PERMANENT promise: it skips the model on every later
    run under the same glossary fingerprint, so a pair that carries no
    translation would pin that string to its degenerate form forever. The
    validator deliberately reports ``UNTRANSLATED`` as a WARNING (a proper
    noun, a mod name or a bare number that correctly reads the same in the
    target language must not fail the entry and must still reach the
    output), so severity alone cannot guard the cache. This predicate does.

    It guards BOTH directions — refused on write, and never served on read.
    Guarding the read too is what makes rows written before the gate
    existed harmless immediately, with no migration scan blocking startup;
    ``LocalTM.purge_degenerate`` then only has to reclaim their disk space.

    Rejected:

    * A target that is untranslated filler, decided by the SAME rule the
      pipeline already uses to reject copied target-locale files
      (``is_untranslated_copy``): empty/whitespace-only, or equal to the
      source after formatting-code/placeholder cleanup and casefolding.
      That single rule covers the poisoning case, the empty target, and
      the "identical proper noun / bare number" case at once, and reusing
      it keeps one definition of "this is not a translation" in the engine.
    * A blank source: the row could only ever be hit by an empty string,
      which the pipeline never sends to the model in the first place.
    * A target still carrying protected tokens ("{{ARG}}", "{{COLOR1}}").
      Restoration failed or was bypassed; serving it would print the token
      into the game. The validator rates the same condition an ERROR, so
      no such pair is legitimate.

    Rejection is not failure: the entry keeps its translated value in the
    output, it simply stays out of the cache and is re-decided next run.
    """
    if not source_text.strip():
        return False
    if is_untranslated_copy(source_text, translated_text):
        return False
    return not TOKEN_RE.search(translated_text)


def _encode_scope(key_scope: Iterable[str]) -> str:
    """Serialize ``key_scope`` for storage, normalized like ``TermRule``."""
    return _SCOPE_SEPARATOR.join(
        sorted({pattern.strip() for pattern in key_scope} - {""})
    )


def _decode_scope(stored: str) -> tuple[str, ...]:
    """Read a stored ``key_scope`` back; "" is the unscoped row."""
    return tuple(stored.split(_SCOPE_SEPARATOR)) if stored else ()


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
            # A database created before row scoping keeps its table, so the
            # column is added in place. The default makes every existing
            # row unscoped, which is the behavior they already had.
            columns = {
                row[1]
                for row in self._conn.execute("PRAGMA table_info(tm_entries)")
            }
            if "key_scope" not in columns:
                self._conn.execute(
                    "ALTER TABLE tm_entries "
                    "ADD COLUMN key_scope TEXT NOT NULL DEFAULT ''"
                )
                logger.info("Added key_scope column to %s", self._db_path)
            self._conn.commit()
        logger.debug("LocalTM opened at %s", self._db_path)

    @property
    def db_path(self) -> Path:
        return self._db_path

    def lookup(
        self,
        source_text: str,
        target_lang: str,
        glossary_version: str,
        *,
        key: str = "",
    ) -> str | None:
        """Cached translation for an exact match, else None.

        Args:
            source_text: Exact source string to probe.
            target_lang: Target locale of the run.
            glossary_version: The run's glossary fingerprint.
            key: Lang key the hit would be applied to. A community row
                scoped to a set of keys can only be judged against one, so
                without it only unscoped rows can match.
        """
        hits = self.lookup_many({key: source_text}, target_lang, glossary_version)
        return hits.get(key)

    def _rows_for(self, hashes: list[str]) -> list[tuple[str, str, str, str]]:
        """``(key_hash, source_text, translated_text, key_scope)`` rows."""
        rows: list[tuple[str, str, str, str]] = []
        with self._lock:
            for start in range(0, len(hashes), _MAX_BATCH_PARAMS):
                chunk = hashes[start : start + _MAX_BATCH_PARAMS]
                marks = ",".join("?" * len(chunk))
                rows.extend(
                    self._conn.execute(
                        "SELECT key_hash, source_text, translated_text, key_scope "
                        f"FROM tm_entries WHERE key_hash IN ({marks})",
                        chunk,
                    ).fetchall()
                )
        return rows

    def lookup_many(
        self,
        entries: Mapping[str, str],
        target_lang: str,
        glossary_version: str,
    ) -> dict[str, str]:
        """Batch lookup: ``{entry_key: source_text}`` -> ``{entry_key: translated_text}``.

        Only hits appear in the result; misses are simply absent. Entry keys
        sharing the same source text all receive the same hit.

        Every source is probed twice: under the run's glossary fingerprint,
        and under the community ``shared`` sentinel. Two rules decide which
        row an entry actually gets, and both exist because a ``shared`` row
        is approved against NO glossary while its constant version makes it
        visible on every run:

        1. **The local row wins.** Its key carries the run's glossary
           fingerprint, so a local hit certifies "produced under exactly
           this glossary" — including each term's ``key_scope``, which the
           fingerprint covers. A ``shared`` row certifies the opposite, and
           a translation valid under no glossary cannot outrank one valid
           under the user's own. Community corrections are not lost by
           this: they arrive as scoped glossary terms too, and THAT channel
           changes the fingerprint, which invalidates the stale local row
           and re-decides every entry containing the term rather than only
           those whose whole text matches.
        2. **A row is served only to keys its ``key_scope`` covers.** One
           full-text match can mean different things under different lang
           keys — the homograph case ``TermRule.key_scope`` exists for —
           so an unscoped community reading must not leak into a key space
           it was never approved for. Local rows are written unscoped, so
           in practice this only constrains ``shared`` rows.

        Degenerate rows are additionally never served (see
        ``is_cacheable_pair``), which is what neutralizes rows written
        before the write gate existed.
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
        # Shared first, local second, so a local row overwrites it.
        for hash_to_keys in (shared_hash_to_keys, local_hash_to_keys):
            for key_hash, source_text, translated_text, scope in self._rows_for(
                list(hash_to_keys)
            ):
                if not is_cacheable_pair(source_text, translated_text):
                    continue
                covered = _decode_scope(scope)
                for entry_key in hash_to_keys[key_hash]:
                    if key_scope_covers(covered, entry_key):
                        hits[entry_key] = translated_text
        return hits

    def store(
        self,
        source_text: str,
        target_lang: str,
        glossary_version: str,
        translated_text: str,
        origin: str = "local",
        key_scope: Iterable[str] = (),
    ) -> None:
        """Insert or update one entry; ``updated_at`` is bumped on conflict.

        A degenerate pair is dropped rather than stored — see
        ``is_cacheable_pair`` for what that means and why. ``key_scope``
        narrows which lang keys the row may be served to; the empty default
        is the unscoped row every local write wants, since a local row's
        key already carries the glossary it was produced under.
        """
        if not is_cacheable_pair(source_text, translated_text):
            logger.debug(
                "Refusing degenerate TM pair: %r -> %r", source_text, translated_text
            )
            return
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
                    _encode_scope(key_scope),
                ),
            )
            self._conn.commit()

    def store_many(
        self,
        items: Iterable[tuple[str, str]],
        target_lang: str,
        glossary_version: str,
        origin: str = "local",
        key_scope: Iterable[str] = (),
    ) -> None:
        """Upsert ``(source_text, translated_text)`` pairs in one transaction.

        Degenerate pairs are skipped (``is_cacheable_pair``) and repeated
        sources collapse onto one parameter row carrying the last value,
        which is exactly what the upsert would have left behind: a pack
        repeats the same short string across mods, so the caller routinely
        hands over the same source many times.

        ``key_scope`` applies to every pair in the call, so a caller with
        per-row scopes groups its rows by scope — the shared-snapshot merge
        does exactly that, and the number of distinct scopes is small.
        """
        now = _utc_now_iso()
        scope = _encode_scope(key_scope)
        params: dict[str, tuple[str, ...]] = {}
        skipped = 0
        for source_text, translated_text in items:
            if not is_cacheable_pair(source_text, translated_text):
                skipped += 1
                continue
            key_hash = tm_key(source_text, target_lang, glossary_version)
            params[key_hash] = (
                key_hash,
                source_text,
                target_lang,
                glossary_version,
                translated_text,
                origin,
                now,
                now,
                scope,
            )
        if skipped:
            logger.debug("Refused %d degenerate TM pair(s)", skipped)
        if not params:
            return
        with self._lock:
            self._conn.executemany(_UPSERT_SQL, list(params.values()))
            self._conn.commit()

    def purge_degenerate(self) -> int:
        """Delete stored rows that ``is_cacheable_pair`` would reject.

        Correctness does not depend on this: such rows are already never
        served (``lookup_many`` applies the same predicate), so this only
        reclaims their disk space. It is an explicit maintenance call
        rather than something the constructor does, because the scan is
        O(rows) with a Python callback per row — measured 4.7 s for one
        million rows — which would block app start and the first run of an
        upgrade for no correctness gain.

        The predicate is handed to SQLite as a function instead of being
        restated in SQL, so the cleanup and the gates can never drift
        apart. Returns the number of rows removed.
        """
        with self._lock:
            self._conn.create_function(
                "moru_is_cacheable", 2, is_cacheable_pair, deterministic=True
            )
            cursor = self._conn.execute(
                "DELETE FROM tm_entries "
                "WHERE NOT moru_is_cacheable(source_text, translated_text)"
            )
            removed = cursor.rowcount
            self._conn.commit()
        return removed

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
