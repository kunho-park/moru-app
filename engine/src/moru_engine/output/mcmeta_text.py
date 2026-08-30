"""Minecraft text metrics for the ``pack.mcmeta`` description.

The resource-pack selection screen gives a pack's description a hard budget
and silently drops whatever does not fit, so the attribution URL at the end
of our description can vanish entirely. Nothing in that screen recovers it:
``TransferableSelectionList.PackEntry`` has no tooltip, and the narrator
reads only the pack title.

Budget, from the client's own constants:

- ``MAX_DESCRIPTION_WIDTH_PIXELS = 157`` — the wrap width.
- ``maxLines = 2`` — an unnamed literal beside it.
- From 1.21.11 the budget becomes ``157 - 6`` whenever that list shows a
  scrollbar, which any real pack list does. :data:`SAFE_LINE_WIDTH` therefore
  designs against **151**, not 157.

The subtle part, and the reason this module exists rather than a naive
``"\\n".join(...)``: **the two lines are VISUAL lines, not logical ones.**
``StringSplitter`` flattens explicit ``\\n`` breaks and width wraps into one
ordered list, then the renderer keeps the first two entries. So if the first
logical line is wide enough to wrap on its own, an explicit second line is
pushed out and never renders at all — putting the URL on its own ``\\n`` line
makes the bug *worse*, not better.

Line breaking is driven only by U+000A. Text-component arrays are joined
with no separator, so they cannot break lines either.
"""

from __future__ import annotations

#: Client wrap width (``MAX_DESCRIPTION_WIDTH_PIXELS``).
LINE_WIDTH = 157

#: Width to design against: from 1.21.11 the list subtracts 6px for its
#: scrollbar, and a pack list with a scrollbar is the normal case.
SAFE_LINE_WIDTH = 151

#: Visual lines the pack entry renders.
MAX_LINES = 2

#: Advance widths of the default bitmap font, measured from ``ascii.png``.
#: The value already includes the 1px inter-character gap, so widths are
#: summed directly with nothing added between glyphs.
_ASCII_ADVANCE: dict[str, int] = {
    **{c: 2 for c in "!',.:;i|"},
    **{c: 3 for c in "`l"},
    # Space comes from the space provider (include/space.json), also 4px.
    **{c: 4 for c in '"()*I[]t{} '},
    **{c: 5 for c in "<>fk"},
    **{c: 7 for c in "@~"},
}

#: Every other printable ASCII glyph.
_ASCII_DEFAULT = 6

#: Precomposed Hangul syllables. 8px, NOT the 9px of CJK ideographs — the
#: unifont size override for this block starts the ink at column 1.
_HANGUL_SYLLABLES = range(0xAC00, 0xD7B0)

#: Full-width ranges that advance 9px (jamo, CJK, kana, fullwidth forms).
_WIDE_RANGES: tuple[tuple[int, int], ...] = (
    (0x1100, 0x11FF),
    (0x3001, 0x30FF),
    (0x3130, 0x318F),
    (0x3200, 0x9FFF),
    (0xA960, 0xA97F),
    (0xD7B0, 0xD7FF),
    (0xF900, 0xFAFF),
    (0xFF01, 0xFF5E),
)

#: Punctuation that resolves to the bitmap provider rather than unifont,
#: because the bitmap providers are searched first.
_BITMAP_PUNCTUATION: dict[str, int] = {
    "\u2014": 9,  # em dash
    "\u2013": 7,  # en dash
    "\u2026": 8,  # horizontal ellipsis
    "\u00b7": 2,  # middle dot
}

#: Minecraft's formatting-code marker. The marker and the code character are
#: both consumed for styling and contribute no width at all.
SECTION = "\u00a7"


def char_advance(char: str) -> int:
    """Advance width of one character in the default font."""
    if char in _BITMAP_PUNCTUATION:
        return _BITMAP_PUNCTUATION[char]
    if char in _ASCII_ADVANCE:
        return _ASCII_ADVANCE[char]
    code = ord(char)
    if code in _HANGUL_SYLLABLES:
        return 8
    for low, high in _WIDE_RANGES:
        if low <= code <= high:
            return 9
    if code == 0x200C:  # zero-width non-joiner
        return 0
    return _ASCII_DEFAULT


def text_width(text: str) -> int:
    """Rendered width in pixels, skipping ``§x`` formatting pairs."""
    total = 0
    index = 0
    length = len(text)
    while index < length:
        char = text[index]
        if char == SECTION and index + 1 < length:
            index += 2  # marker + code, both zero width
            continue
        total += char_advance(char)
        index += 1
    return total


def visual_lines(text: str, width: int = SAFE_LINE_WIDTH) -> list[str]:
    """Split ``text`` the way the client does: one flat list of visual lines.

    Explicit ``\\n`` breaks and width wraps land in the same list, which is
    exactly why an explicit line break cannot reserve a line for itself.
    Breaking prefers the last space before the overflow and consumes it;
    with no space to break on, the break falls mid-word.
    """
    lines: list[str] = []
    current = ""
    current_width = 0
    break_at: int | None = None  # index in `current` of the last space
    index = 0
    length = len(text)

    while index < length:
        char = text[index]
        if char == "\n":
            lines.append(current)
            current, current_width, break_at = "", 0, None
            index += 1
            continue
        if char == SECTION and index + 1 < length:
            current += text[index : index + 2]
            index += 2
            continue

        advance = char_advance(char)
        if current_width + advance > width and current:
            if break_at is not None:
                lines.append(current[:break_at])
                current = current[break_at + 1 :]
            else:
                lines.append(current)
                current = ""
            current_width = text_width(current)
            break_at = None
            continue

        if char == " ":
            break_at = len(current)
        current += char
        current_width += advance
        index += 1

    lines.append(current)
    return lines


def fits(text: str, width: int = SAFE_LINE_WIDTH, max_lines: int = MAX_LINES) -> bool:
    """Whether every part of ``text`` survives the render budget."""
    return len(visual_lines(text, width)) <= max_lines


def fit_description(
    text: str,
    width: int = SAFE_LINE_WIDTH,
    max_lines: int = MAX_LINES,
) -> str:
    """Trim ``text`` from the FRONT until it fits the render budget.

    The client drops overflow from the end, which is where our attribution
    URL lives, so an over-long description loses exactly the thing the line
    exists to show. Trimming from the front inverts that: the leading version
    prefix — the least important part, and already shown elsewhere in the
    launcher — goes first, while the translated/source-text marker and the URL
    at the tail survive.

    A description that already fits is returned untouched, so the common case
    is byte-identical to before.
    """
    if fits(text, width, max_lines):
        return text

    # Drop leading whitespace-delimited tokens one at a time. Splitting on
    # spaces keeps §-codes attached to the token they style.
    tokens = text.split(" ")
    for start in range(1, len(tokens)):
        candidate = " ".join(tokens[start:])
        if fits(candidate, width, max_lines):
            return candidate

    # Nothing survives whole; keep the final token, which carries the URL.
    return tokens[-1]
