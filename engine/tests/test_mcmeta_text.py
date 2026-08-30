"""pack.mcmeta description: real Minecraft text metrics and the render budget.

Reference values come from the client's own constants and font assets:
``MAX_DESCRIPTION_WIDTH_PIXELS = 157`` with an unnamed ``maxLines = 2``,
minus 6px for the scrollbar from 1.21.11.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from moru_engine.output import (
    BILINGUAL_DESCRIPTION_SUFFIX,
    OutputConfig,
    OutputGenerator,
)
from moru_engine.output.mcmeta_text import (
    LINE_WIDTH,
    MAX_LINES,
    SAFE_LINE_WIDTH,
    fit_description,
    fits,
    text_width,
    visual_lines,
)

URL = "§amoru.gg"
NOTE_TRANSLATED = "§a모루§7로 번역됨 — §amoru.gg"
NOTE_SOURCE = "§7원문 그대로 — §amoru.gg"


# -- metrics -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # Independently derived reference widths.
        ("moru.gg", 38),
        (" — moru.gg", 55),
        ("§amoru.gg", 38),
        # §-codes are consumed for styling and cost nothing.
        ("§a§7§8moru.gg", 38),
        # Space is 4px, from the space provider.
        (" ", 4),
        ("  ", 8),
        # Narrow ASCII glyphs.
        ("i", 2),
        ("l", 3),
        ("t", 4),
        ("f", 5),
        ("a", 6),
        ("@", 7),
        # A precomposed Hangul syllable is 8px, NOT the 9px of a CJK
        # ideograph — the unifont override starts its ink at column 1.
        ("모", 8),
        ("모루", 16),
        # Em dash comes from the bitmap provider, not unifont.
        ("—", 9),
    ],
)
def test_text_width(text: str, expected: int) -> None:
    assert text_width(text) == expected


def test_hangul_and_cjk_advance_differently() -> None:
    assert text_width("한") == 8
    assert text_width("漢") == 9


# -- the flat visual-line split ---------------------------------------------


def test_explicit_newline_does_not_reserve_a_line() -> None:
    """The crux: `\\n` breaks and width wraps share one flat list.

    A first logical line wide enough to wrap on its own pushes the second
    logical line past ``maxLines``, so putting the URL on its own line makes
    the truncation worse rather than better.
    """
    long_first = "가" * 30  # 240px, wraps on its own at 151px
    lines = visual_lines(f"{long_first}\n{URL}")
    assert len(lines) == 3
    assert lines[2] == URL
    # ...and the renderer only keeps MAX_LINES, so the URL is dropped.
    assert not fits(f"{long_first}\n{URL}")
    assert URL not in lines[:MAX_LINES]

def test_wrapping_breaks_on_the_last_space_and_consumes_it() -> None:
    # 12 'a' (72px) + space (4px) then 'b's; the overflow happens partway
    # through the b-run, so the break falls back to the space at index 12
    # and that space is consumed rather than kept on either line.
    lines = visual_lines("aaaaaaaaaaaa bbbbbbbbbbbb", 100)
    assert lines[0] == "aaaaaaaaaaaa"
    assert lines[1].startswith("b")
    assert all(not line.startswith(" ") for line in lines)


def test_wrapping_breaks_mid_word_when_there_is_no_space() -> None:
    lines = visual_lines("a" * 40, 60)
    assert len(lines) > 1
    assert all(text_width(line) <= 60 for line in lines)


def test_every_produced_line_is_within_the_budget() -> None:
    for note in (NOTE_TRANSLATED, NOTE_SOURCE):
        for prefix in ("", "v4.1.2 / ", "v2.0.19-ko-rev3 / "):
            for line in visual_lines(prefix + note):
                assert text_width(line) <= SAFE_LINE_WIDTH


def test_safe_width_is_the_scrollbar_adjusted_budget() -> None:
    assert LINE_WIDTH == 157
    assert SAFE_LINE_WIDTH == 151
    assert MAX_LINES == 2


# -- fitting -----------------------------------------------------------------


def test_a_description_that_already_fits_is_untouched() -> None:
    """The common case must be byte-identical to before the fix."""
    for note in (NOTE_TRANSLATED, NOTE_SOURCE):
        text = f"v4.1.2 / {note}"
        assert fits(text)
        assert fit_description(text) == text


def test_overlong_description_keeps_the_url_by_dropping_the_version() -> None:
    # A version prefix no real modpack would carry, purely to force the
    # front-trim path: the shortened note leaves so much margin that every
    # realistic prefix now fits within the two visual lines on its own.
    text = f"v1.2.3-nightly-build-20260830-experimental-branch / {NOTE_TRANSLATED}"
    assert not fits(text)
    fitted = fit_description(text)
    assert fits(fitted)
    assert "moru.gg" in "".join(visual_lines(fitted)[:MAX_LINES])
    # The variant marker survives with it; only the version prefix is lost.
    assert "번역됨" in fitted


def test_url_survives_even_a_pathological_description() -> None:
    text = "가" * 200 + " " + URL
    fitted = fit_description(text)
    assert "moru.gg" in "".join(visual_lines(fitted)[:MAX_LINES])


@pytest.mark.parametrize("note", [NOTE_TRANSLATED, NOTE_SOURCE])
@pytest.mark.parametrize(
    "prefix", ["", "v4.1.2 / ", "v6.5.4hotfix / ", "v2.0.19-ko-rev3 / "]
)
@pytest.mark.parametrize("suffix", ["", BILINGUAL_DESCRIPTION_SUFFIX])
def test_moru_gg_is_always_visible(note: str, prefix: str, suffix: str) -> None:
    """The whole point: attribution must reach the player in every case."""
    fitted = fit_description(prefix + note + suffix)
    rendered = visual_lines(fitted)[:MAX_LINES]
    assert any("moru.gg" in line for line in rendered), fitted


@pytest.mark.parametrize(
    "prefix", ["", "v4.1.2 / ", "v6.5.4hotfix / ", "v2.0.19-ko-rev3 / "]
)
def test_both_variants_stay_distinguishable(prefix: str) -> None:
    """The description is the only in-game way to tell the packs apart."""
    plain = visual_lines(fit_description(prefix + NOTE_TRANSLATED))[:MAX_LINES]
    source = visual_lines(fit_description(prefix + NOTE_SOURCE))[:MAX_LINES]
    bilingual = visual_lines(
        fit_description(prefix + NOTE_TRANSLATED + BILINGUAL_DESCRIPTION_SUFFIX)
    )[:MAX_LINES]

    assert "번역됨" in "".join(plain)
    assert "원문 그대로" in "".join(source)
    assert "병기" in "".join(bilingual)
    assert plain != source
    assert plain != bilingual


def test_bilingual_marker_fits_without_trimming_the_worst_real_case() -> None:
    """The marker is sized so no realistic pack needs the front-trim path."""
    worst = f"v2.0.19-ko-rev3 / {NOTE_TRANSLATED}{BILINGUAL_DESCRIPTION_SUFFIX}"
    assert fits(worst), visual_lines(worst)


# -- generator integration ---------------------------------------------------


@pytest.mark.asyncio
async def test_written_mcmeta_description_is_fitted(tmp_path: Path) -> None:
    overlong = f"v1.2.3-an-absurdly-long-version-tag / {NOTE_TRANSLATED}"
    generator = OutputGenerator(
        OutputConfig(
            modpack_root=tmp_path / "modpack",
            output_dir=tmp_path / "out",
            description=overlong,
        )
    )
    path = await generator._write_pack_mcmeta()
    written = json.loads(path.read_text(encoding="utf-8"))["pack"]["description"]

    assert fits(written)
    assert "moru.gg" in "".join(visual_lines(written)[:MAX_LINES])


@pytest.mark.asyncio
async def test_written_mcmeta_keeps_a_fitting_description_verbatim(
    tmp_path: Path,
) -> None:
    description = f"v4.1.2 / {NOTE_TRANSLATED}"
    generator = OutputGenerator(
        OutputConfig(
            modpack_root=tmp_path / "modpack",
            output_dir=tmp_path / "out",
            description=description,
        )
    )
    path = await generator._write_pack_mcmeta()
    written = json.loads(path.read_text(encoding="utf-8"))["pack"]["description"]
    assert written == description
