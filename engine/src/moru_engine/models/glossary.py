"""Pydantic models for translation glossary."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator


#: Rank of a rule that carries no ``key_scope``: it applies to every key,
#: and any scoped rule covering that key outranks it.
UNSCOPED_RANK = (0, 0)


def _pattern_rank(pattern: str, segments: list[str]) -> tuple[int, int] | None:
    """Specificity of one ``key_scope`` pattern against a split lang key.

    Args:
        pattern: Dotted glob, e.g. ``effect.*`` or ``entity.minecraft.wither``.
        segments: Lang key already split on ``.``.

    Returns:
        ``None`` when the pattern does not cover the key, otherwise
        ``(literal segment count, pattern segment count)`` — the ordering
        used to pick the most specific matching pattern.
    """
    parts = pattern.split(".")
    if parts[-1] == "*":
        # A trailing "*" absorbs every remaining key segment.
        if len(parts) - 1 > len(segments):
            return None
    elif len(parts) != len(segments):
        return None

    literals = 0
    for index, part in enumerate(parts):
        if part == "*":
            continue
        if segments[index] != part:
            return None
        literals += 1
    return literals, len(parts)


def key_scope_covers(key_scope: Iterable[str], key: str) -> bool:
    """Whether a ``key_scope`` glob list covers ``key``.

    The membership half of :meth:`TermRule.scope_rank`, without a rule to
    hang it on: the shared translation memory scopes its rows by the same
    dotted globs but has no precedence contest to resolve, since one row
    per source text can match at most once.

    An empty list covers every key, exactly as an empty ``key_scope`` does
    on a rule.
    """
    if not key_scope:
        return True
    segments = key.split(".")
    return any(_pattern_rank(p, segments) is not None for p in key_scope)


class TermRule(BaseModel):
    """A terminology rule for consistent translation.

    Defines how a specific game term should be translated,
    including style preferences and alternative forms.

    ``key_scope`` narrows a rule to the lang keys it may fire on, which is
    what keeps homographs apart: vanilla "Wither" is the boss 위더 under
    ``entity.*`` but the status effect 시듦 under ``effect.*``, so a single
    unscoped rule corrupts one of the two senses.

    Scope patterns are dotted lang-key globs. Every segment is either a
    literal (matched exactly) or ``*`` (any one segment); a trailing ``*``
    absorbs all remaining segments. So ``effect.minecraft.wither`` is an
    exact key, ``effect.*`` is the whole ``effect`` namespace and
    ``subtitles.*.wither`` wildcards one segment in the middle.

    Precedence, resolved per key (see :meth:`scope_rank`):

    1. A scoped rule covering the key beats every unscoped rule for the
       same source term. An unscoped rule is the term's default reading and
       keeps applying to every key that no scoped rule claims.
    2. Between two covering scoped rules the more specific one wins, where
       specificity is ``(literal segment count, pattern segment count)``
       compared in that order: ``entity.minecraft.wither`` (3, 3) beats
       ``entity.minecraft.*`` (2, 3) beats ``entity.*`` (1, 2).
    3. On a full tie the rule listed first in ``Glossary.term_rules`` wins,
       so resolution stays deterministic.

    An empty ``key_scope`` is the pre-scope behaviour, unchanged: the rule
    applies to every key.
    """

    term_ko: str = Field(
        ...,
        description="Korean translation of the term",
        examples=["마법 부여대", "주괴"],
    )
    preferred_style: str = Field(
        ...,
        description="Style guide for the term",
        examples=["띄어쓰기 유지", "한글 표기"],
    )
    aliases: list[str] = Field(
        default_factory=list,
        description="English aliases or original terms",
        examples=[["Enchanting Table"], ["Ingot", "ingots"]],
    )
    key_scope: list[str] = Field(
        default_factory=list,
        description=(
            "Lang keys this rule applies to, as dotted globs (each segment "
            "a literal or '*'; a trailing '*' absorbs the remaining "
            "segments). Empty means the rule applies to every key."
        ),
        examples=[["effect.*"], ["entity.minecraft.wither"]],
    )
    category: Literal["item", "block", "ui", "entity", "effect", "biome", "other"] = (
        Field(
            default="other",
            description="Category of the term",
        )
    )
    notes: str = Field(
        default="",
        description="Additional notes about the term",
    )

    @field_validator("aliases")
    @classmethod
    def sort_aliases(cls, v: list[str]) -> list[str]:
        """Sort aliases for consistent deduplication."""
        return sorted(list(set(v)))

    @field_validator("key_scope")
    @classmethod
    def sort_key_scope(cls, v: list[str]) -> list[str]:
        """Drop blank patterns, then sort for consistent deduplication."""
        return sorted({pattern.strip() for pattern in v} - {""})

    def scope_rank(self, key: str) -> tuple[int, int] | None:
        """Precedence rank of this rule for ``key``.

        Args:
            key: Lang key of the entry being translated.

        Returns:
            ``UNSCOPED_RANK`` when the rule carries no scope, the most
            specific matching pattern's rank when it does, and ``None``
            when the rule is scoped away from ``key``. Higher ranks win —
            the class docstring carries the full precedence order.
        """
        if not self.key_scope:
            return UNSCOPED_RANK
        segments = key.split(".")
        best: tuple[int, int] | None = None
        for pattern in self.key_scope:
            rank = _pattern_rank(pattern, segments)
            if rank is not None and (best is None or rank > best):
                best = rank
        return best


class ProperNounRule(BaseModel):
    """A proper noun translation rule.

    Defines consistent translations for proper nouns
    like dimension names, mod names, etc.
    """

    source_like: str = Field(
        ...,
        description="Original English proper noun (primary form)",
        examples=["Nether", "Ender", "Mekanism"],
    )
    preferred_ko: str = Field(
        ...,
        description="Preferred Korean translation",
        examples=["네더", "엔더", "메카니즘"],
    )
    aliases: list[str] = Field(
        default_factory=list,
        description="Alternative forms/spellings that should also match this rule",
        examples=[["The Nether", "nether"], ["End", "The End", "Ender"]],
    )
    notes: str = Field(
        default="",
        description="Additional notes about usage",
    )

    @field_validator("aliases")
    @classmethod
    def sort_proper_noun_aliases(cls, v: list[str]) -> list[str]:
        """Sort aliases for consistent deduplication."""
        return sorted(list(set(v)))


class FormattingRule(BaseModel):
    """A formatting/style rule for translations.

    Defines general style guidelines for the translation,
    such as honorifics, punctuation, format preservation, etc.
    """

    rule_name: str = Field(
        ...,
        description="Name of the formatting rule",
        examples=["존댓말", "조사 처리", "따옴표", "레벨 표기 보존"],
    )
    description: str = Field(
        ...,
        description="Description of the rule",
    )
    examples: list[str] = Field(
        default_factory=list,
        description="Example applications of the rule",
    )
    keywords: list[str] = Field(
        default_factory=list,
        description=(
            "Trigger keywords/patterns that indicate when this rule applies. "
            "If empty, the rule is considered global and always included. "
            "Case-insensitive matching is used."
        ),
        examples=[["Lv.", "Level", "lv"], ["HP", "MP", "SP"]],
    )
    is_global: bool = Field(
        default=False,
        description=(
            "If True, this rule is always included regardless of keywords. "
            "Use for universal style rules like honorifics or punctuation."
        ),
    )


class Glossary(BaseModel):
    """Complete glossary for translation consistency.

    Contains all rules needed for consistent translation
    of a Minecraft modpack.
    """

    version: str = Field(
        default="1.0",
        description="Glossary schema version",
    )
    locale_source: str = Field(
        default="en_us",
        description="Source language locale",
    )
    locale_target: str = Field(
        default="ko_kr",
        description="Target language locale",
    )
    created_at: datetime = Field(
        default_factory=datetime.now,
        description="Glossary creation timestamp",
    )
    updated_at: datetime | None = Field(
        default=None,
        description="Last update timestamp",
    )
    term_rules: list[TermRule] = Field(
        default_factory=list,
        description="Terminology translation rules",
    )
    proper_noun_rules: list[ProperNounRule] = Field(
        default_factory=list,
        description="Proper noun translation rules",
    )
    formatting_rules: list[FormattingRule] = Field(
        default_factory=list,
        description="Formatting and style rules",
    )

    def merge_with(self, other: Glossary) -> Glossary:
        """Merge another glossary into this one.

        Args:
            other: Glossary to merge.

        Returns:
            New merged glossary.
        """
        # Create sets of existing items for deduplication. Two term rules
        # that share a target and aliases but differ in key_scope are
        # genuinely different rules (one sense per key space), so the scope
        # is part of the identity.
        existing_terms = {
            (t.term_ko, tuple(t.aliases), tuple(t.key_scope)) for t in self.term_rules
        }
        existing_nouns = {
            (n.source_like.lower(), n.preferred_ko) for n in self.proper_noun_rules
        }
        existing_rules = {r.rule_name for r in self.formatting_rules}

        # Lists for new items
        new_terms: list[TermRule] = []
        new_nouns: list[ProperNounRule] = []
        new_rules: list[FormattingRule] = []

        # Add new items with deduplication
        for t in other.term_rules:
            key = (t.term_ko, tuple(t.aliases), tuple(t.key_scope))
            if key not in existing_terms:
                existing_terms.add(key)
                new_terms.append(t)

        for n in other.proper_noun_rules:
            key = (n.source_like.lower(), n.preferred_ko)
            if key not in existing_nouns:
                existing_nouns.add(key)
                new_nouns.append(n)

        for r in other.formatting_rules:
            key = r.rule_name
            if key not in existing_rules:
                existing_rules.add(key)
                new_rules.append(r)

        return Glossary(
            version=self.version,
            locale_source=self.locale_source,
            locale_target=self.locale_target,
            created_at=self.created_at,
            updated_at=datetime.now(),
            term_rules=[*self.term_rules, *new_terms],
            proper_noun_rules=[*self.proper_noun_rules, *new_nouns],
            formatting_rules=[*self.formatting_rules, *new_rules],
        )

    @property
    def has_rules(self) -> bool:
        """Check if glossary has any rules.

        Returns:
            True if glossary has at least one rule.
        """
        return bool(self.term_rules or self.proper_noun_rules or self.formatting_rules)

    def to_context_string(self) -> str:
        """Convert glossary to a string for LLM context.

        Scoped rules carry their key space on the line, and the section
        header gains a one-line instruction so a mixed batch cannot have a
        scoped rule applied to the wrong key. Output is unchanged for a
        glossary whose rules are all unscoped.

        Returns:
            Human-readable glossary summary with full details.
        """
        lines: list[str] = []

        if self.term_rules:
            header = "## Term Rules (MUST follow these translations)"
            if any(term.key_scope for term in self.term_rules):
                header += (
                    "\n(적용 키가 붙은 규칙은 그 키에만 적용하고, 나머지 키에는 "
                    "적용 키 없는 규칙을 적용하세요)"
                )
            lines.append(header)
            for term in self.term_rules:
                aliases = ", ".join(term.aliases) if term.aliases else "N/A"
                line = f"- **{aliases}** → **{term.term_ko}**"
                if term.key_scope:
                    line += f" (적용 키: {', '.join(term.key_scope)})"
                if term.preferred_style:
                    line += f" (스타일: {term.preferred_style})"
                if term.notes:
                    line += f" — {term.notes}"
                lines.append(line)

        if self.proper_noun_rules:
            lines.append("\n## Proper Noun Rules (MUST use these translations)")
            for noun in self.proper_noun_rules:
                line = f"- **{noun.source_like}** → **{noun.preferred_ko}**"
                if noun.aliases:
                    line += f" (변형: {', '.join(noun.aliases)})"
                if noun.notes:
                    line += f" — {noun.notes}"
                lines.append(line)

        if self.formatting_rules:
            lines.append("\n## Formatting Rules (MUST follow these rules)")
            for rule in self.formatting_rules:
                lines.append(f"- **{rule.rule_name}**: {rule.description}")
                if rule.examples:
                    for example in rule.examples:
                        lines.append(f"  - 예: {example}")

        return "\n".join(lines)
