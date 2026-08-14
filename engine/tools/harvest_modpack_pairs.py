"""Modpack goldset harvester (OPERATOR TOOL — offline data stage).

Extracts human-translated (source, target) sentence pairs from a modpack
zip or directory that ships both locales (see evalset/modtext.py for the
sources scanned and the filter policy) and writes a goldset JSON for the
evalset's "modtext" stratum.

Usage:
    uv run python tools/harvest_modpack_pairs.py PACK.zip \
        --source en_us --target ko_kr
    uv run python tools/optimize.py ... --modtext goldsets/<pack>.json

The output is deterministic for identical inputs, so the key-level
train/val/test assignment derived from it is reproducible. Goldsets stay
local operator artifacts (goldsets/ is gitignored): they embed community
pack text and are not shipped.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from moru_engine.evalset.modtext import harvest_pack  # noqa: E402
from moru_engine.utils.log import setup_logging  # noqa: E402

logger = logging.getLogger("tools.harvest_modpack_pairs")

DEFAULT_OUT_DIR = Path(__file__).resolve().parents[1] / "goldsets"


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("pack", type=Path, help="modpack zip or directory")
    parser.add_argument("--source", default="en_us")
    parser.add_argument("--target", default="ko_kr")
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=None,
        help="output goldset path (default goldsets/<pack>__<src>-<tgt>.json)",
    )
    args = parser.parse_args()

    setup_logging(logging.INFO)
    if not args.pack.exists():
        parser.error(f"pack not found: {args.pack}")

    goldset = harvest_pack(args.pack, source_lang=args.source, target_lang=args.target)
    stats = goldset["stats"]
    if not stats["pairs"]:  # type: ignore[index]
        logger.error(
            "No usable pairs: the pack ships no %s+%s locale pairs (dropped: %s)",
            args.source,
            args.target,
            stats["dropped"],  # type: ignore[index]
        )
        return 1

    out = args.output or (
        DEFAULT_OUT_DIR / f"{args.pack.stem}__{args.source}-{args.target}.json"
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(goldset, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    logger.info(
        "Goldset written: %s (%d pairs: %s)",
        out,
        stats["pairs"],  # type: ignore[index]
        ", ".join(
            f"{cat}={n}"
            for cat, n in stats["by_category"].items()  # type: ignore[index]
        ),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
