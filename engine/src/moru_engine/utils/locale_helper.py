"""Locale helper functions for translation."""

from __future__ import annotations

import re

# ----------------------------------------------------------------------
# Case-preserving locale token replacement
# ----------------------------------------------------------------------
# Used by output generators to substitute the source locale (e.g. "en_us")
# with the target locale (e.g. "ko_kr") inside file paths and filenames,
# while preserving the case style of the original token. This matters for
# Minecraft 1.12.x mods that ship language files using Pascal-style locale
# codes (e.g. "en_US.lang" / "ko_KR.lang") rather than the modern
# all-lowercase "en_us.json" convention.


def _detect_locale_case_style(locale_str: str) -> str:
    """Detect the case style of a locale token like ``en_us`` / ``en_US``.

    Args:
        locale_str: A matched locale token (already known to look like ``xx_yy``).

    Returns:
        One of ``"lower"``, ``"upper"``, ``"pascal_locale"`` (lowercase
        language + uppercase region), or ``"mixed"`` for anything else.
    """
    if locale_str.islower():
        return "lower"
    if locale_str.isupper():
        return "upper"
    parts = locale_str.split("_")
    if (
        len(parts) == 2
        and parts[0].islower()
        and parts[1].isupper()
        and parts[0]
        and parts[1]
    ):
        return "pascal_locale"
    return "mixed"


def _apply_locale_case_style(target_locale: str, style: str) -> str:
    """Render ``target_locale`` (assumed lowercase ``xx_yy``) in the given style.

    Args:
        target_locale: Lowercase target locale code (e.g. ``ko_kr``).
        style: Output of :func:`_detect_locale_case_style`.

    Returns:
        Target locale rewritten in the same case style as the source.
    """
    target_lower = target_locale.lower()
    if style == "upper":
        return target_lower.upper()
    if style == "pascal_locale":
        parts = target_lower.split("_")
        if len(parts) == 2:
            return f"{parts[0]}_{parts[1].upper()}"
    # "lower" and "mixed" both fall back to lowercase (safe modern default).
    return target_lower


def replace_locale_in_path(
    path: str,
    source_locale: str,
    target_locale: str,
) -> str:
    """Replace ``source_locale`` with ``target_locale`` inside a path string,
    preserving the case style of every match.

    Locale codes appearing as substrings of unrelated words are NOT replaced,
    because the surrounding characters must be path/extension separators.

    Examples:
        >>> replace_locale_in_path("assets/foo/lang/en_us.json", "en_us", "ko_kr")
        'assets/foo/lang/ko_kr.json'

        >>> replace_locale_in_path("assets/foo/lang/en_US.lang", "en_us", "ko_kr")
        'assets/foo/lang/ko_KR.lang'

        >>> replace_locale_in_path("config/EN_US/foo.cfg", "en_us", "ko_kr")
        'config/KO_KR/foo.cfg'

    Args:
        path: Original path string (forward or backslash separators allowed).
        source_locale: Source locale code in any case (e.g. ``en_us``).
        target_locale: Target locale code in any case (e.g. ``ko_kr``).

    Returns:
        Path string with locale tokens replaced, case style preserved.
    """
    src_lower = source_locale.lower()
    if not src_lower:
        return path

    # Match the locale only when it's a standalone token: bounded by
    # path separators, dots, dashes, underscores adjacent to a separator,
    # or string boundaries. Using lookarounds keeps this simple and avoids
    # consuming the surrounding characters.
    pattern = re.compile(
        r"(?<![A-Za-z0-9])" + re.escape(src_lower) + r"(?![A-Za-z0-9])",
        re.IGNORECASE,
    )

    def _replace(match: re.Match[str]) -> str:
        matched = match.group(0)
        style = _detect_locale_case_style(matched)
        return _apply_locale_case_style(target_locale, style)

    return pattern.sub(_replace, path)
