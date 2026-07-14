"""Placeholder protection utilities for translation."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# Placeholder patterns. Dict order is the overlap priority: when two
# patterns match overlapping spans, the earlier pattern keeps its match
# and the later one is dropped (see protect()).
PATTERNS = {
    # Java format specifiers: %s, %d, %1$s, %2$d, etc.
    "java_format": re.compile(r"%(?:\d+\$)?[sdifxXobeEgGaAcChHnp%]"),
    # Minecraft color codes: §a, §0-§f, §k-§o, §r
    "mc_color_section": re.compile(r"§[0-9a-fk-or]", re.IGNORECASE),
    # Alternative color codes: &a, &0-&f, etc.
    "mc_color_ampersand": re.compile(r"&[0-9a-fk-or]", re.IGNORECASE),
    # Named placeholders: {player}, {item}, etc.
    "named_placeholder": re.compile(r"\{[^}]+\}"),
    # XML-like tags: <b>, </b>, <color=red>, <font color="red">, <br />.
    # Whitespace inside a tag is allowed ONLY for name=value attributes
    # (or a spaced self-close), so prose in angle brackets
    # ("<Error occurred, plz report to %s>") stays translatable instead
    # of being frozen as one opaque token.
    "xml_tags": re.compile(
        # whitespace-free tags: <b>, </b>, <color=red>, <#FF0000>, <br/>
        r"<[^>\s]+>"
        # attribute tags — every attribute must carry =value (bare words
        # after the name read as prose): <font color="red" size=2>
        r"|<[A-Za-z][\w:.-]*"
        r"(?:\s+[A-Za-z][\w:.-]*\s*=\s*(?:\"[^\"]*\"|'[^']*'|[^\s>]+))+"
        r"\s*/?>"
        # spaced self-closing tag: <br />
        r"|<[A-Za-z][\w:.-]*\s*/>"
    ),
    # Newlines and special characters
    "special_chars": re.compile(r"\\[nrt]"),
}

# Semantic token kinds: the LLM sees WHAT each token is so it can place it
# naturally in the target language (a color span must keep wrapping the
# same words after reordering).
#
# Numbering policy: a number identifies WHICH literal a token stands for,
# so it is added only when one entry mixes two or more distinct literals
# of the same kind ("&6..&a" -> {{COLOR1}}/{{COLOR2}}). When every
# occurrence of a kind is the same literal — always true for RESET ("&r")
# and BR ("\n") — the bare {{KIND}} token is used for every occurrence:
# spurious numbers measurably confuse small models into inventing or
# renumbering tokens. Occurrences of the SAME literal share one token, so
# restore() stays a literal, order-free replacement either way.
TOKEN_KIND_BY_PATTERN = {
    "java_format": "ARG",
    "named_placeholder": "VAR",
    "xml_tags": "TAG",
    "special_chars": "BR",
}

# Any protected token: {{COLOR}}, {{RESET}}, {{ARG1}}, {{VAR2}}, ...
TOKEN_RE = re.compile(r"\{\{[A-Z]+\d*\}\}")


def _classify_kind(pattern_name: str, original: str) -> str:
    """Semantic kind for a matched literal ("&r" is RESET, "&6" COLOR)."""
    if pattern_name in ("mc_color_section", "mc_color_ampersand"):
        return "RESET" if original[1].lower() == "r" else "COLOR"
    return TOKEN_KIND_BY_PATTERN.get(pattern_name, "PH")


class PlaceholderError(ValueError):
    """Exception raised when placeholder restoration fails."""

    pass


@dataclass
class PlaceholderInfo:
    """Information about a placeholder in text."""

    original: str
    token: str
    pattern_name: str
    position: int


@dataclass
class ProtectedText:
    """Text with placeholders replaced by tokens."""

    original: str
    protected: str
    placeholders: list[PlaceholderInfo] = field(default_factory=list)

    def restore(self, translated: str) -> str:
        """Restore placeholders in translated text.

        The roundtrip is strict: every token must occur in the translation
        exactly as many times as in the protected source. Occurrences of
        one literal share one token, so the check is per unique token.

        Args:
            translated: Translated text with tokens.

        Returns:
            Translated text with original placeholders restored.

        Raises:
            PlaceholderError: On any missing, surplus, or unknown token.
        """
        result = translated

        literal_by_token: dict[str, str] = {}
        expected_counts: dict[str, int] = {}
        for placeholder in self.placeholders:
            literal_by_token[placeholder.token] = placeholder.original
            expected_counts[placeholder.token] = (
                expected_counts.get(placeholder.token, 0) + 1
            )

        count_issues = []
        # Longest token first so a numbered form can never be corrupted by
        # replacing a shorter one.
        for token in sorted(literal_by_token, key=len, reverse=True):
            actual = result.count(token)
            expected = expected_counts[token]
            if actual != expected:
                count_issues.append(f"{token} x{actual} (expected x{expected})")
                logger.error(
                    "Placeholder token '%s' occurs %d time(s), expected %d. "
                    "Original: '%s', Translated: '%s'",
                    token,
                    actual,
                    expected,
                    self.original,
                    translated,
                )
                continue
            result = result.replace(token, literal_by_token[token])

        # Anything still token-shaped is unknown or was left by a count
        # mismatch above.
        has_issues = bool(count_issues)
        if TOKEN_RE.search(result):
            logger.error(
                "Unrestored placeholder tokens remaining in translation. "
                "Original: '%s', Result: '%s'",
                self.original,
                result,
            )
            has_issues = True

        if has_issues:
            raise PlaceholderError(
                f"Placeholder validation failed for original text: '{self.original}'"
            )

        return result


class PlaceholderProtector:
    """Protects placeholders during translation.

    Replaces placeholders with unique tokens before translation
    and restores them afterward.
    """

    def __init__(self) -> None:
        """Initialize the protector (stateless; kept for API stability)."""

    def protect(self, text: str) -> ProtectedText:
        """Replace placeholders with tokens.

        Args:
            text: Original text with placeholders.

        Returns:
            Protected text info.
        """
        protected = text
        placeholders: list[PlaceholderInfo] = []

        matches: list[tuple[int, str, str]] = []  # (position, literal, kind)
        claimed: list[tuple[int, int]] = []  # spans owned by earlier patterns
        for pattern_name, pattern in PATTERNS.items():
            for match in pattern.finditer(text):
                start, end = match.span()
                # A match overlapping an earlier pattern's span is dropped
                # (PATTERNS order is the priority). Keeping both corrupts
                # restore(): replacing the inner literal shifts the text,
                # so the outer literal's recorded position/length go stale
                # ("<...%s>" used to restore as "{{TAG}}RG}}>").
                if any(start < c_end and c_start < end for c_start, c_end in claimed):
                    continue
                claimed.append((start, end))
                original = match.group(0)
                matches.append(
                    (start, original, _classify_kind(pattern_name, original))
                )

        # Number tokens per kind ONLY when that kind mixes distinct
        # literals; the number then identifies the literal (not the
        # occurrence), assigned in first-appearance order.
        matches.sort(key=lambda m: m[0])
        literals_by_kind: dict[str, list[str]] = {}
        for _, original, kind in matches:
            seen = literals_by_kind.setdefault(kind, [])
            if original not in seen:
                seen.append(original)

        for position, original, kind in matches:
            literals = literals_by_kind[kind]
            suffix = "" if len(literals) == 1 else str(literals.index(original) + 1)
            placeholders.append(
                PlaceholderInfo(
                    original=original,
                    token=f"{{{{{kind}{suffix}}}}}",
                    pattern_name=kind,
                    position=position,
                )
            )

        # Sort by position (reverse) to replace from end to start
        placeholders.sort(key=lambda p: p.position, reverse=True)

        for placeholder in placeholders:
            protected = (
                protected[: placeholder.position]
                + placeholder.token
                + protected[placeholder.position + len(placeholder.original) :]
            )

        return ProtectedText(
            original=text,
            protected=protected,
            placeholders=placeholders,
        )

    def is_only_placeholders(self, protected_text: ProtectedText) -> bool:
        """Check if protected text contains only placeholder tokens.

        Args:
            protected_text: Protected text to check.

        Returns:
            True if text contains only tokens and whitespace.
        """
        if not protected_text.placeholders:
            return False

        # Remove all tokens and check if anything remains
        temp = protected_text.protected
        for placeholder in protected_text.placeholders:
            temp = temp.replace(placeholder.token, "")

        # If only whitespace remains, it's placeholder-only
        return temp.strip() == ""

    @staticmethod
    def count_placeholders(text: str) -> dict[str, int]:
        """Count placeholders by type.

        Args:
            text: Text to analyze.

        Returns:
            Dictionary of pattern names to counts.
        """
        counts: dict[str, int] = {}

        for name, pattern in PATTERNS.items():
            matches = pattern.findall(text)
            if matches:
                counts[name] = len(matches)

        return counts
