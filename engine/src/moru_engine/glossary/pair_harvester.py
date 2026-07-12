"""Harvest settled terminology from mods' own target-locale lang files.

A mod that ships both the configured source and target locales has
already made its terminology choices. Name-key entries (``item.``,
``block.``, ``entity.``, ...) whose source value reads like a short noun
phrase become authoritative term rules, so every OTHER file in the pack
(quests, guidebooks, sibling mods' tooltips) reuses the established
translation instead of inventing a new one.

Safety over coverage: a source term is promoted only when every occurrence
in the pack agrees on ONE target string. Conflicting translations are
dropped entirely - forcing mod A's choice onto mod B's context would be
worse than letting the model decide per batch. Terms already covered by
user/store rules are skipped, so manual curation always wins.

Only the SOURCE value is checked for Latin-script noun-phrase shape; the
target side is validated locale-agnostically (non-empty, bounded length,
actually different from the source) so CJK and other scripts survive.
"""

from __future__ import annotations

import logging
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ..models import TermRule
from .term_miner import NAME_KEY_RE, clean_text, is_name_value

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

__all__ = [
    "TranslatedTerm",
    "build_term_rules",
    "collect_translated_terms",
    "is_untranslated_copy",
]

logger = logging.getLogger(__name__)

#: Parameterised values are not stable names ("%s Ingot", "{name} Core").
_PLACEHOLDER_RE = re.compile(r"%(?:\d+\$)?[sdif%]|\{[^{}]*\}")

#: Generous cap for target-side names; translations may expand.
_MAX_TARGET_CHARS = 64

#: NAME_KEY_RE group(1) -> TermRule.category literal (rest -> "other").
_KEY_CATEGORY: dict[str, str] = {
    "item": "item",
    "block": "block",
    "entity": "entity",
    "effect": "effect",
    "biome": "biome",
}


@dataclass(frozen=True)
class TranslatedTerm:
    """One (source, target) name pair found in a mod's own lang files."""

    source: str
    target: str
    category: str


def is_untranslated_copy(source: str, target: str) -> bool:
    """Detect target values that are source-locale filler, not translations.

    Mods routinely copy the source-locale file into other locales wholesale
    or leave part of the entries untouched. A target that equals its source
    after formatting-code/placeholder cleanup and casefolding (so
    "§6Iron Ingot" vs "iron ingot" still counts as a copy) carries no
    translation signal: it must be neither harvested as a term nor reused
    as an existing translation. An empty/whitespace target is a copy by
    convention.
    """
    cleaned = clean_text(target)
    return not cleaned or cleaned.casefold() == clean_text(source).casefold()


def collect_translated_terms(
    source_data: Mapping[str, str],
    target_data: Mapping[str, str],
) -> list[TranslatedTerm]:
    """Extract name-key pairs the mod itself translated.

    The same lang key present in both files is the correctness anchor: it
    names the same game object by construction. Values whose target is
    missing, empty after formatting-code cleanup, over-long, or a verbatim
    copy of the source (mods routinely ship source-locale filler in other
    locales) are skipped.
    """
    terms: list[TranslatedTerm] = []
    for key, raw_source in source_data.items():
        match = NAME_KEY_RE.search(key)
        if match is None:
            continue
        raw_target = target_data.get(key)
        if not raw_target:
            continue
        if _PLACEHOLDER_RE.search(raw_source) or _PLACEHOLDER_RE.search(raw_target):
            continue
        if is_untranslated_copy(raw_source, raw_target):
            continue
        source = clean_text(raw_source)
        if not is_name_value(source):
            continue
        target = clean_text(raw_target)
        if len(target) > _MAX_TARGET_CHARS:
            continue
        terms.append(
            TranslatedTerm(
                source=source,
                target=target,
                category=_KEY_CATEGORY.get(match.group(1).lower(), "other"),
            )
        )
    return terms


def build_term_rules(
    terms: Iterable[TranslatedTerm],
    known_aliases: Iterable[str] = (),
) -> list[TermRule]:
    """Fold harvested occurrences into unanimous term rules.

    ``known_aliases`` (manual/store/vanilla rules) win outright: a source
    already covered there never produces a rule. Sources whose occurrences
    disagree on the target are dropped - see module docstring.
    """
    known = {alias.casefold() for alias in known_aliases}
    by_source: dict[str, list[TranslatedTerm]] = defaultdict(list)
    for term in terms:
        folded = term.source.casefold()
        if folded not in known:
            by_source[folded].append(term)

    rules: list[TermRule] = []
    dropped = 0
    for occurrences in by_source.values():
        if len({t.target for t in occurrences}) > 1:
            dropped += 1
            continue
        # Most common original casing / category. Ties break by explicit
        # sort, not Counter's first-seen order, so alias casing and the
        # downstream glossary fingerprint never depend on scan order.
        source = _most_common(t.source for t in occurrences)
        category = _most_common(t.category for t in occurrences)
        rules.append(
            TermRule(
                term_ko=occurrences[0].target,
                preferred_style="용어 고정",
                aliases=[source],
                category=category,  # type: ignore[arg-type]
                notes="existing mod translation",
            )
        )
    if dropped:
        logger.info(
            "Mod translation harvest: %d conflicting source terms dropped",
            dropped,
        )
    rules.sort(key=lambda rule: rule.aliases[0].casefold())
    return rules


def _most_common(values: Iterable[str]) -> str:
    """Highest-count value; ties resolved lexicographically."""
    counts = Counter(values)
    return min(counts.items(), key=lambda kv: (-kv[1], kv[0]))[0]
