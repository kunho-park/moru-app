"""Per-entry translation aids for the hand-translation surface.

Everything here is pure and local: regex matching, a dict scan, and one SQLite
read. No provider, no API key, no network. That is the point — the manual
surface has to be fully useful with nothing configured, so the aids a human
actually leans on (which terms apply, what the sibling lines say, which
placeholders must survive, whether this exact string was translated elsewhere)
must never sit behind a model.

Kept out of ``app.py`` so the HTTP layer gains three thin routes rather than
owning several hundred lines of matching logic.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ..batching import continuation_groups
from ..community import load_user_glossary_terms
from ..models.glossary import Glossary, TermRule
from ..models.glossary_filter import GlossaryFilter
from ..placeholder import PATTERNS, PlaceholderProtector
from ..validator import TranslationValidator

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from ..pipeline import EntryResult
    from ..tm import LocalTM


def glossary_from_store(
    store_dir: Path, source_lang: str, target_lang: str
) -> Glossary:
    """The user glossary document as a matchable ``Glossary``.

    Deliberately the same document ``GET /glossary`` serves and the glossary
    screen edits, so what a translator is shown here is what they curated —
    not a run-scoped glossary they cannot see. ``PipelineResult.glossary`` is
    not usable for this: it is dropped when a session is restored from disk.
    """
    rules: list[TermRule] = []
    for term in load_user_glossary_terms(store_dir, source_lang, target_lang):
        if not isinstance(term, dict):
            continue
        source = str(term.get("source") or "").strip()
        target = str(term.get("target") or "").strip()
        if not source or not target:
            continue
        scope = term.get("key_scope")
        rules.append(
            TermRule(
                term_ko=target,
                preferred_style="용어 고정",
                aliases=[source],
                key_scope=[str(s) for s in scope] if isinstance(scope, list) else [],
                notes=f"user glossary ({term.get('origin') or 'manual'})",
            )
        )
    return Glossary(
        locale_source=source_lang, locale_target=target_lang, term_rules=rules
    )


def _siblings(
    entry: EntryResult, entries: Sequence[EntryResult]
) -> list[dict[str, Any]]:
    """The entry's numbered run, in ordinal order, excluding itself.

    Uses the engine's own ``continuation_groups`` so what a translator is shown
    grouped is exactly what the pipeline batches as one unit. Mods split one
    sentence across ``tooltip1..tooltipN``; someone shown ``desc2`` alone, or
    shown the parts out of order, will mistranslate it.
    """
    in_file = [e for e in entries if e.file == entry.file]
    by_key = {e.key: e for e in in_file}
    for group in continuation_groups([e.key for e in in_file]):
        if entry.key not in group:
            continue
        return [
            {
                "key": sibling.key,
                "source_text": sibling.source_text,
                "translated_text": sibling.translated_text,
                "status": sibling.status.value,
            }
            for key in group
            if key != entry.key and (sibling := by_key.get(key)) is not None
        ]
    return []


def _glossary_payload(glossary: Glossary, entry: EntryResult) -> dict[str, Any]:
    """Only the rules that apply to THIS entry.

    ``filter_for_texts`` resolves ``key_scope`` per key, so the real lang key
    must be passed — a synthetic stand-in matches no scope and would silently
    drop every scoped rule.
    """
    scoped = GlossaryFilter.filter_for_texts(glossary, {entry.key: entry.source_text})
    return {
        "terms": [
            {
                "aliases": list(rule.aliases),
                "term_ko": rule.term_ko,
                "preferred_style": rule.preferred_style,
                "key_scope": list(rule.key_scope),
            }
            for rule in scoped.term_rules
        ],
        "proper_nouns": [
            {"source_like": rule.source_like, "preferred_ko": rule.preferred_ko}
            for rule in scoped.proper_noun_rules
        ],
        "formatting": [
            {"rule_name": rule.rule_name, "description": rule.description}
            for rule in scoped.formatting_rules
        ],
    }


def _same_source_elsewhere(
    entry: EntryResult, entries: Sequence[EntryResult]
) -> list[dict[str, Any]]:
    """Other entries with a byte-identical source that already have a target.

    The consistency check a hand translator most wants, and for this job it
    beats a fuzzy memory lookup (which this engine does not have anyway): the
    same English string in two mods *must* agree, and a disagreement is exactly
    the bug worth surfacing rather than hiding behind a similarity score.
    """
    out: list[dict[str, Any]] = []
    for other in entries:
        if other is entry or other.source_text != entry.source_text:
            continue
        if not other.translated_text:
            continue
        out.append(
            {
                "file": other.file,
                "key": other.key,
                "translated_text": other.translated_text,
                "agrees": other.translated_text == (entry.translated_text or ""),
            }
        )
        if len(out) >= 5:
            break
    return out


def _placeholders(entry: EntryResult) -> list[dict[str, Any]]:
    protected = PlaceholderProtector().protect(entry.source_text)
    return [
        {"token": info.token, "kind": info.pattern_name, "literal": info.original}
        for info in protected.placeholders
    ]


def build_entry_context(
    entry: EntryResult,
    entries: Sequence[EntryResult],
    *,
    glossary: Glossary,
    tm: LocalTM | None,
    target_lang: str,
    glossary_version: str,
) -> dict[str, Any]:
    """Everything a human needs to translate one entry well."""
    exact: dict[str, Any] | None = None
    if tm is not None:
        hit = tm.lookup(entry.source_text, target_lang, glossary_version, key=entry.key)
        if hit:
            exact = {"translated_text": hit, "origin": "local", "updated_at": None}

    return {
        "file": entry.file,
        "mod_id": None,
        "namespace": None,
        "content_type": None,
        "siblings": _siblings(entry, entries),
        "glossary": _glossary_payload(glossary, entry),
        "tm": {
            "exact": exact,
            "same_source_elsewhere": _same_source_elsewhere(entry, entries),
        },
        "placeholders": _placeholders(entry),
    }


def validate_pair(
    validator: TranslationValidator, entry: EntryResult, draft: str
) -> list[dict[str, Any]]:
    """Structured issues for one candidate translation.

    Returns the issue objects rather than the flattened English message list
    that ``EntryResult.errors`` carries: ``issue_type`` is the stable code a
    client needs to render its own localized text, and ``severity``
    distinguishes "will not ship" from "probably fine". Both are discarded by
    the pipeline's single-entry paths today.
    """
    report = validator.validate({entry.key: entry.source_text}, {entry.key: draft})
    return [
        {
            "issue_type": issue.issue_type.value,
            "severity": issue.severity.value,
            "key": issue.key,
            "message": issue.message,
            "suggestion": issue.suggestion,
            "source_value": issue.source_value,
            "translated_value": issue.translated_value,
        }
        for issue in report.issues
    ]


def placeholder_patterns() -> list[dict[str, str]]:
    """The engine's placeholder patterns, in overlap-priority order.

    Served so a client highlights and counts tokens with the same definitions
    the validator enforces, instead of keeping a second copy that drifts.
    Declaration order IS the priority: an inner match overlapping a span
    already claimed by an earlier pattern is dropped.
    """
    return [
        {"name": name, "regex": pattern.pattern} for name, pattern in PATTERNS.items()
    ]
