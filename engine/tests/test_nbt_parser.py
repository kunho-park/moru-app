"""Binary NBT translation must preserve every original tag type."""

from __future__ import annotations

import gzip
import struct
from pathlib import Path

import pytest

from moru_engine.parsers.nbt import (
    NBTParser,
    TAG_BYTE,
    TAG_BYTE_ARRAY,
    TAG_COMPOUND,
    TAG_DOUBLE,
    TAG_END,
    TAG_FLOAT,
    TAG_INT,
    TAG_INT_ARRAY,
    TAG_LIST,
    TAG_LONG,
    TAG_LONG_ARRAY,
    TAG_SHORT,
    TAG_STRING,
)


def _string(value: str) -> bytes:
    encoded = value.encode("utf-8")
    return struct.pack(">H", len(encoded)) + encoded


def _named(tag_type: int, name: str, payload: bytes) -> bytes:
    return bytes([tag_type]) + _string(name) + payload


def _sample_nbt() -> bytes:
    payload = b"".join(
        [
            _named(TAG_BYTE, "byte", struct.pack(">b", -5)),
            _named(TAG_SHORT, "short", struct.pack(">h", 7)),
            _named(TAG_INT, "int", struct.pack(">i", 1)),
            _named(TAG_LONG, "long", struct.pack(">q", 2)),
            _named(TAG_FLOAT, "float", struct.pack(">f", 1.5)),
            _named(TAG_DOUBLE, "double", struct.pack(">d", 2.5)),
            _named(TAG_BYTE_ARRAY, "bytes", struct.pack(">i", 3) + bytes([0, 127, 255])),
            _named(TAG_STRING, "text", _string("Hello")),
            _named(
                TAG_LIST,
                "ints",
                bytes([TAG_INT])
                + struct.pack(">i", 2)
                + struct.pack(">i", 1)
                + struct.pack(">i", 2),
            ),
            _named(
                TAG_LIST,
                "names",
                bytes([TAG_STRING])
                + struct.pack(">i", 2)
                + _string("First")
                + _string("Second"),
            ),
            _named(
                TAG_LIST,
                "emptyLongs",
                bytes([TAG_LONG]) + struct.pack(">i", 0),
            ),
            _named(
                TAG_INT_ARRAY,
                "intsArray",
                struct.pack(">i", 2) + struct.pack(">ii", 1, 2),
            ),
            _named(
                TAG_LONG_ARRAY,
                "longsArray",
                struct.pack(">i", 2) + struct.pack(">qq", 1, 2),
            ),
            _named(
                TAG_COMPOUND,
                "nested",
                _named(TAG_STRING, "label", _string("Nested"))
                + bytes([TAG_END]),
            ),
            bytes([TAG_END]),
        ]
    )
    return bytes([TAG_COMPOUND]) + _string("root") + payload


@pytest.mark.asyncio
async def test_dump_preserves_binary_tag_types_and_compression(tmp_path: Path) -> None:
    source = tmp_path / "source.nbt"
    source.write_bytes(gzip.compress(_sample_nbt()))

    parsed = await NBTParser(source).parse()
    assert parsed["root.text"] == "Hello"
    assert parsed["root.names[0]"] == "First"
    assert parsed["root.nested.label"] == "Nested"

    output = tmp_path / "translated.nbt"
    await NBTParser(output, original_path=source).dump(
        {
            **parsed,
            "root.text": "안녕하세요",
            "root.names[0]": "첫째",
            "root.nested.label": "중첩",
        }
    )

    compressed = output.read_bytes()
    assert compressed.startswith(b"\x1f\x8b")
    raw = gzip.decompress(compressed)

    # These values intentionally fit in narrower types. Their headers must
    # still retain the source tag ids instead of being inferred from value size.
    assert _named(TAG_SHORT, "short", b"")[:8] in raw
    assert _named(TAG_INT, "int", b"")[:6] in raw
    assert _named(TAG_LONG, "long", b"")[:7] in raw
    assert _named(TAG_FLOAT, "float", b"")[:8] in raw
    assert _named(TAG_DOUBLE, "double", b"")[:9] in raw
    assert _named(TAG_BYTE_ARRAY, "bytes", b"")[:8] in raw
    assert _named(TAG_INT_ARRAY, "intsArray", b"")[:12] in raw
    assert _named(TAG_LONG_ARRAY, "longsArray", b"")[:13] in raw
    assert _named(TAG_LIST, "ints", b"") + bytes([TAG_INT]) in raw
    assert _named(TAG_LIST, "emptyLongs", b"") + bytes([TAG_LONG]) in raw

    reparsed = await NBTParser(output).parse()
    assert reparsed["root.text"] == "안녕하세요"
    assert reparsed["root.names[0]"] == "첫째"
    assert reparsed["root.nested.label"] == "중첩"
