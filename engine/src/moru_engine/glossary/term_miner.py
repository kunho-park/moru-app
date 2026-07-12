"""Deterministic glossary candidate mining over the whole modpack corpus.

Instead of showing the LLM an arbitrary slice of raw strings and hoping
it spots terms, we scan EVERY extracted entry deterministically and hand
the LLM a ranked candidate list to curate and translate. Coverage is
total, selection is reproducible, and the LLM budget scales with the
number of candidates - not the corpus size.

Signals, in priority order:
1. Name keys - lang keys like ``item.modid.storm_hammer`` whose value is a
   short noun phrase are, by construction, content names (count 1 is fine).
2. Recurring capitalized phrases - multi-word Title Case runs appearing in
   at least ``min_count`` entries anywhere in the corpus.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

__all__ = [
    "NAME_KEY_RE",
    "TermCandidate",
    "clean_text",
    "is_name_value",
    "mine_candidates",
]

#: Lang-key prefixes whose values name game content. Group 1 captures the
#: matched prefix word (e.g. "item") for category mapping in pair_harvester.
NAME_KEY_RE = re.compile(
    r"(?:^|[./:])"
    r"(item|block|entity|effect|enchantment|biome|material|fluid|spell"
    r"|structure|dimension|attribute)s?[./:]",
    re.IGNORECASE,
)

#: Minecraft formatting codes / placeholders stripped before mining.
_NOISE_RE = re.compile(r"§.|&[0-9a-fk-or](?=[A-Za-z])|%(?:\d+\$)?[sdif%]|\{[^{}]*\}")

#: Title Case run of 2+ words, allowing one lowercase connector between them
#: ("Sunken Shrine", "Heart of the Machine", "Vault Hunters' Tools").
_CAP_WORD = r"[A-Z][A-Za-z'\u2019\-]+"
#: Consecutive Title Case words, optionally joined by "of/and(the)":
#: "Sunken Shrine", "Heart of the Machine". A bare lowercase "the" is NOT
#: a joiner, so sentence-leading verbs ("Bring the Void Orb") cannot glue
#: onto the phrase.
_CAP_PHRASE_RE = re.compile(
    rf"\b{_CAP_WORD}(?:(?: (?:of|and)(?: the)?)? {_CAP_WORD})+\b"
)

#: Sentence-initial articles stripped off matched phrases ("The Void Orb"
#: -> "Void Orb") so counts merge with mid-sentence occurrences.
_LEAD_STOPWORDS = frozenset({"the", "a", "an"})

_WORD_RE = re.compile(r"[A-Za-z]")

_MAX_NAME_WORDS = 5
_MAX_NAME_CHARS = 48
_MAX_CONTEXT_CHARS = 90
_MAX_CONTEXTS = 2


@dataclass
class TermCandidate:
    """One mined candidate with corpus evidence for the curation LLM."""

    term: str
    count: int = 0
    from_name_key: bool = False
    contexts: list[str] = field(default_factory=list)

    def as_line(self) -> str:
        """Single prompt line: term, occurrence count, one usage context."""
        ctx = f' — e.g. "{self.contexts[0]}"' if self.contexts else ""
        return f"{self.term} (x{self.count}){ctx}"


def clean_text(value: str) -> str:
    """Strip formatting codes/placeholders and collapse whitespace."""
    return " ".join(_NOISE_RE.sub(" ", value).split())


def is_name_value(value: str) -> bool:
    """Short noun-phrase check for (cleaned) name-key values."""
    if not value or len(value) > _MAX_NAME_CHARS:
        return False
    if not _WORD_RE.search(value):
        return False
    # Sentences (tooltips under item.* keys) are not names.
    if value.endswith((".", "!", "?")) or "," in value:
        return False
    return len(value.split()) <= _MAX_NAME_WORDS


def mine_candidates(
    entries: dict[str, str],
    existing_terms: set[str],
    *,
    max_terms: int | None = 300,
    min_count: int = 2,
) -> list[TermCandidate]:
    """Mine ranked term candidates from ``{entry_key: source_text}``.

    ``existing_terms`` (lowercased aliases of already-fixed rules - vanilla,
    community, manual) are excluded so the LLM never re-translates settled
    vocabulary. Deterministic: same corpus in, same candidates out.

    ``max_terms=None`` keeps every ranked candidate.
    """
    excluded = {t.lower() for t in existing_terms}
    by_term: dict[str, TermCandidate] = {}

    def bump(term: str, cleaned_value: str, *, name_key: bool) -> None:
        key = term.lower()
        if key in excluded or len(term) < 3:
            return
        cand = by_term.get(key)
        if cand is None:
            cand = by_term[key] = TermCandidate(term=term)
        cand.count += 1
        cand.from_name_key = cand.from_name_key or name_key
        if (
            len(cand.contexts) < _MAX_CONTEXTS
            and cleaned_value != term
            and cleaned_value not in cand.contexts
        ):
            cand.contexts.append(cleaned_value[:_MAX_CONTEXT_CHARS])

    for entry_key, raw in entries.items():
        value = clean_text(raw)
        if not value:
            continue
        # One bump per term per entry - a name-key value that also matches
        # the phrase regex must not double-count.
        found: dict[str, bool] = {}
        if NAME_KEY_RE.search(entry_key) and is_name_value(value):
            found[value] = True
        for match in _CAP_PHRASE_RE.finditer(value):
            words = match.group(0).split()
            while words and words[0].lower() in _LEAD_STOPWORDS:
                words.pop(0)
            if len(words) >= 2:
                term = " ".join(words)
                found[term] = found.get(term, False)
        for term, name_key in found.items():
            bump(term, value, name_key=name_key)

    kept = [
        c
        for c in by_term.values()
        if c.from_name_key or c.count >= min_count
    ]
    kept.sort(key=lambda c: (not c.from_name_key, -c.count, c.term.lower()))
    return kept if max_terms is None else kept[:max_terms]
