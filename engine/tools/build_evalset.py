"""Evalset snapshot builder (operator tool).

Builds the v0 evalset (vanilla + stress cases) and writes a JSON snapshot
for inspection/versioning. Once the web platform ships its
GET /api/export/corrections endpoint (web-api.yaml), approved community
corrections are appended with --corrections-url.

Usage:
    uv run python tools/build_evalset.py --target ko_kr -o evalset_snapshot.json
    uv run python tools/build_evalset.py --corrections-url https://moru.gg \
        --since 2026-01-01T00:00:00Z   # MORU_WEB_TOKEN env required
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import aiohttp  # noqa: E402
import dspy  # noqa: E402

from moru_engine.evalset import build_evalset  # noqa: E402
from moru_engine.evalset.builder import INPUT_FIELDS, _protect_pair  # noqa: E402
from moru_engine.utils.log import setup_logging  # noqa: E402

logger = logging.getLogger("tools.build_evalset")


async def fetch_corrections(
    base_url: str, target_lang: str, since: str | None
) -> list[dspy.Example]:
    """Pull approved corrections and convert them to refine-style examples."""
    token = os.environ.get("MORU_WEB_TOKEN")
    if not token:
        raise SystemExit("MORU_WEB_TOKEN env var required for --corrections-url")
    params: dict[str, str] = {"lang": target_lang}
    if since:
        params["since"] = since
    url = f"{base_url.rstrip('/')}/api/export/corrections"
    async with aiohttp.ClientSession() as session:
        async with session.get(
            url, params=params, headers={"Authorization": f"Bearer {token}"}
        ) as resp:
            resp.raise_for_status()
            payload = await resp.json()

    examples: list[dspy.Example] = []
    for item in payload.get("corrections", []):
        source, gold = _protect_pair(item["source_text"], item["corrected_text"])
        examples.append(
            dspy.Example(
                source_lang="en_us",
                target_lang=item.get("target_lang", target_lang),
                context="Community-corrected modpack entry",
                glossary="",
                entries={item["entry_key"]: source},
                translations={item["entry_key"]: gold},
                term_rules=[],
            ).with_inputs(*INPUT_FIELDS)
        )
    logger.info("Fetched %d corrections", len(examples))
    return examples


def serialize(examples: list[dspy.Example]) -> list[dict[str, object]]:
    return [
        {
            "source_lang": ex.source_lang,
            "target_lang": ex.target_lang,
            "context": ex.context,
            "glossary": ex.glossary,
            "entries": ex.entries,
            "translations": ex.translations,
            "term_rules": ex.term_rules,
        }
        for ex in examples
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", default="en_us")
    parser.add_argument("--target", default="ko_kr")
    parser.add_argument("--vanilla-samples", type=int, default=400)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--corrections-url", default=None)
    parser.add_argument("--since", default=None)
    parser.add_argument("-o", "--output", default="evalset_snapshot.json")
    args = parser.parse_args()

    setup_logging(logging.INFO)
    split = build_evalset(
        args.source, args.target, vanilla_samples=args.vanilla_samples, seed=args.seed
    )
    if args.corrections_url:
        corrections = asyncio.run(
            fetch_corrections(args.corrections_url, args.target, args.since)
        )
        # Corrections join the train split only: test stays frozen for
        # regression comparability.
        split["train"] += corrections

    snapshot = {name: serialize(examples) for name, examples in split.items()}
    out = Path(args.output)
    out.write_text(
        json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    logger.info(
        "Snapshot written: %s (train=%d val=%d test=%d)",
        out,
        len(snapshot["train"]),
        len(snapshot["val"]),
        len(snapshot["test"]),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
