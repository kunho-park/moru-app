"""Safe extraction of KubeJS ``.displayName(<string>)`` calls.

KubeJS startup scripts frequently register user-facing item names directly in
JavaScript instead of language JSON.  Translating the whole script would put
executable code in the model's hands, so this handler exposes only direct
string/template-literal arguments and splices the reviewed translations back
into an otherwise byte-for-byte copy of the source file.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar

import aiofiles

from .base import ContentHandler

if TYPE_CHECKING:
    from collections.abc import Mapping


_DISPLAY_NAME_CALL_RE = re.compile(r"\.displayName\s*\(\s*")


@dataclass(frozen=True, slots=True)
class _DisplayNameLiteral:
    key: str
    quote: str
    text: str
    start: int
    end: int


def _code_mask(source: str) -> list[bool]:
    """Mark characters in executable JS, excluding strings and comments."""
    mask = [False] * len(source)
    state = "code"
    cursor = 0
    while cursor < len(source):
        char = source[cursor]
        next_char = source[cursor + 1] if cursor + 1 < len(source) else ""
        if state == "code":
            if char == "/" and next_char == "/":
                state = "line_comment"
                cursor += 2
                continue
            if char == "/" and next_char == "*":
                state = "block_comment"
                cursor += 2
                continue
            if char in {"'", '"', "`"}:
                state = char
            else:
                mask[cursor] = True
            cursor += 1
            continue
        if state == "line_comment":
            if char in "\r\n":
                state = "code"
                mask[cursor] = True
            cursor += 1
            continue
        if state == "block_comment":
            if char == "*" and next_char == "/":
                state = "code"
                cursor += 2
            else:
                cursor += 1
            continue
        # A quote-named state is a JS string/template literal.
        if char == "\\":
            cursor += 2
            continue
        if char == state:
            state = "code"
        cursor += 1
    return mask


def _display_name_literals(source: str) -> list[_DisplayNameLiteral]:
    """Return direct quoted arguments, ignoring expressions and comments.

    The small scanner deliberately understands only JavaScript string and
    comment boundaries. Calls such as
    ``displayName(Component.translatable(...))`` remain untouched, while both
    ordinary strings and template literals such as ```Orb of ${name}``` are
    safe to translate.
    """
    found: list[_DisplayNameLiteral] = []
    code = _code_mask(source)
    for call in _DISPLAY_NAME_CALL_RE.finditer(source):
        if not code[call.start()]:
            continue
        start = call.end()
        if start >= len(source) or source[start] not in {"'", '"', "`"}:
            continue
        quote = source[start]
        cursor = start + 1
        while cursor < len(source):
            if source[cursor] == "\\":
                cursor += 2
                continue
            if source[cursor] == quote:
                break
            cursor += 1
        if cursor >= len(source):
            continue
        tail = cursor + 1
        while tail < len(source) and source[tail].isspace():
            tail += 1
        if tail >= len(source) or source[tail] != ")":
            continue
        found.append(
            _DisplayNameLiteral(
                key=f"display_name.{len(found) + 1:04d}",
                quote=quote,
                text=source[start + 1 : cursor],
                start=start + 1,
                end=cursor,
            )
        )
    return found


def _escape_delimiter(text: str, delimiter: str) -> str:
    """Escape unescaped delimiters without disturbing existing JS escapes."""
    output: list[str] = []
    backslashes = 0
    for char in text:
        if char == delimiter and backslashes % 2 == 0:
            output.append("\\")
        output.append(char)
        if char == "\\":
            backslashes += 1
        else:
            backslashes = 0
    return "".join(output)


class KubeJSDisplayNameHandler(ContentHandler):
    """Translate only direct ``displayName`` literals in KubeJS JS/TS."""

    name: ClassVar[str] = "kubejs_display_name"
    priority: ClassVar[int] = 14
    extensions: ClassVar[tuple[str, ...]] = (".js", ".ts")

    def can_handle(self, path: Path) -> bool:
        normalized = path.as_posix().lower()
        in_kubejs = normalized.startswith("kubejs/") or "/kubejs/" in normalized
        return in_kubejs and path.suffix.lower() in self.extensions

    async def extract(self, path: Path) -> Mapping[str, str]:
        async with aiofiles.open(path, encoding="utf-8", errors="replace") as file:
            source = await file.read()
        return {literal.key: literal.text for literal in _display_name_literals(source)}

    async def apply(
        self,
        path: Path,
        translations: Mapping[str, str],
        output_path: Path | None = None,
    ) -> None:
        async with aiofiles.open(path, encoding="utf-8", errors="replace") as file:
            source = await file.read()
        replacements = []
        for literal in _display_name_literals(source):
            translated = translations.get(literal.key)
            if translated is None:
                continue
            replacements.append(
                (
                    literal.start,
                    literal.end,
                    _escape_delimiter(translated, literal.quote),
                )
            )
        for start, end, translated in reversed(replacements):
            source = source[:start] + translated + source[end:]

        target = output_path or path
        target.parent.mkdir(parents=True, exist_ok=True)
        async with aiofiles.open(target, "w", encoding="utf-8") as file:
            await file.write(source)
