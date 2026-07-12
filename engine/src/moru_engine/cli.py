"""Engine CLI.

Usage:
    uv run python -m moru_engine.cli scan ./test/modpack
    uv run python -m moru_engine.cli scan ./test/modpack --json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

from .pipeline import PipelineConfig, run_pipeline
from .scanner import ScanResult, scan_modpack
from .utils.log import setup_logging

logger = logging.getLogger(__name__)


def _relative(path: Path, root: Path) -> str:
    """Render a path relative to the modpack root when possible."""
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def _scan_report(result: ScanResult) -> dict[str, object]:
    """Build a deterministic, JSON-serializable scan report."""
    root = result.modpack_path
    paired = sorted(
        {
            (
                _relative(p.source_path, root),
                _relative(p.target_path, root) if p.target_path else None,
            )
            for p in result.paired_files
        }
    )
    source_only = sorted(
        {_relative(p.source_path, root) for p in result.source_only_files}
    )
    target_only = sorted({_relative(p, root) for p in result.target_only_files})
    files = sorted(
        {
            (
                _relative(Path(f.input_path), root),
                f.file_type,
                f.lang_type,
                f.category,
            )
            for f in result.translation_files
        }
    )
    return {
        "modpack_path": str(root),
        "source_locale": result.source_locale,
        "target_locale": result.target_locale,
        "totals": {
            "source_files": result.total_source_files,
            "target_files": result.total_target_files,
            "paired": result.total_paired,
        },
        "paired": [{"source": s, "target": t} for s, t in paired],
        "source_only": source_only,
        "target_only": target_only,
        "files": [
            {"path": p, "file_type": ft, "lang_type": lt, "category": c}
            for p, ft, lt, c in files
        ],
    }


def _print_report(report: dict[str, object]) -> None:
    totals = report["totals"]
    assert isinstance(totals, dict)
    sys.stdout.write(f"Modpack: {report['modpack_path']}\n")
    sys.stdout.write(
        f"Locales: {report['source_locale']} -> {report['target_locale']}\n"
    )
    sys.stdout.write(
        "Totals: source={source_files} target={target_files} paired={paired}\n".format(
            **totals
        )
    )
    paired = report["paired"]
    assert isinstance(paired, list)
    sys.stdout.write(f"[paired] ({len(paired)})\n")
    for pair in paired:
        target = pair["target"] or "-"
        sys.stdout.write(f"  {pair['source']} -> {target}\n")
    source_only = report["source_only"]
    assert isinstance(source_only, list)
    sys.stdout.write(f"[source-only] ({len(source_only)})\n")
    for path in source_only:
        sys.stdout.write(f"  {path}\n")
    target_only = report["target_only"]
    assert isinstance(target_only, list)
    sys.stdout.write(f"[target-only] ({len(target_only)})\n")
    for path in target_only:
        sys.stdout.write(f"  {path}\n")
    files = report["files"]
    assert isinstance(files, list)
    sys.stdout.write(f"[files] ({len(files)})\n")
    for f in files:
        sys.stdout.write(
            f"  {f['path']} type={f['file_type']} lang={f['lang_type']}"
            f" category={f['category']}\n"
        )


async def _cmd_scan(args: argparse.Namespace) -> int:
    modpack = Path(args.modpack_path)
    if not modpack.exists():
        logger.error("Modpack path does not exist: %s", modpack)
        return 1
    result = await scan_modpack(
        modpack,
        source_locale=args.source,
        target_locale=args.target,
    )
    report = _scan_report(result)
    if args.json:
        sys.stdout.write(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    else:
        _print_report(report)
    return 0


async def _cmd_translate(args: argparse.Namespace) -> int:
    modpack = Path(args.modpack_path)
    if not modpack.exists():
        logger.error("Modpack path does not exist: %s", modpack)
        return 1
    config = PipelineConfig(
        modpack_path=modpack,
        output_dir=Path(args.output) if args.output else None,
        source_locale=args.source,
        target_locale=args.target,
        model=args.model,
        api_base=args.api_base,
        use_tm=not args.no_tm,
        extract_glossary=args.extract_glossary,
        max_refine=args.max_refine,
        temperature=args.temperature,
    )

    def on_event(event: str, payload: dict[str, object]) -> None:
        if event == "progress":
            sys.stderr.write(
                f"\r[{payload.get('stage')}] {payload.get('file', '')} "
                f"{payload.get('done', 0)}/{payload.get('total', 0)}    "
            )
            sys.stderr.flush()

    result = await run_pipeline(config, on_event=on_event)
    sys.stderr.write("\n")
    stats = result.stats
    sys.stdout.write(
        f"files={stats.total_files} entries={stats.total_entries} "
        f"translated={stats.translated_entries} tm_hits={stats.tm_hits} "
        f"failed={stats.failed_entries} skipped={stats.skipped_entries}\n"
        f"coverage={stats.coverage_percent}% quality={stats.quality_score} "
        f"tokens={stats.prompt_tokens}+{stats.completion_tokens} "
        f"duration={stats.duration_seconds}s\n"
    )
    for entry in result.failed[:20]:
        sys.stdout.write(f"FAILED {entry.file}:{entry.key}: {'; '.join(entry.errors)}\n")
    if len(result.failed) > 20:
        sys.stdout.write(f"... and {len(result.failed) - 20} more failures\n")
    sys.stdout.write(f"output files: {len(result.output_files)}\n")
    return 0 if stats.failed_entries == 0 else 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="moru-engine", description="Moru engine CLI")
    parser.add_argument("--verbose", action="store_true", help="debug logging")
    sub = parser.add_subparsers(dest="command", required=True)

    scan = sub.add_parser("scan", help="scan a modpack for translatable files")
    scan.add_argument("modpack_path")
    scan.add_argument("--source", default="en_us", help="source locale (xx_yy)")
    scan.add_argument("--target", default="ko_kr", help="target locale (xx_yy)")
    scan.add_argument("--json", action="store_true", help="machine-readable output")
    scan.set_defaults(func=_cmd_scan)

    translate = sub.add_parser("translate", help="translate a modpack")
    translate.add_argument("modpack_path")
    translate.add_argument("--source", default="en_us", help="source locale (xx_yy)")
    translate.add_argument("--target", default="ko_kr", help="target locale (xx_yy)")
    translate.add_argument(
        "--model",
        default="openai/gpt-4o-mini",
        help="LiteLLM model string (API key via provider env var)",
    )
    translate.add_argument("--api-base", default=None, help="override base URL (Ollama)")
    translate.add_argument("--output", default=None, help="output directory")
    translate.add_argument("--no-tm", action="store_true", help="disable translation memory")
    translate.add_argument(
        "--extract-glossary",
        action="store_true",
        help="LLM glossary term extraction before translating",
    )
    translate.add_argument("--max-refine", type=int, default=2)
    translate.add_argument(
        "--temperature",
        type=float,
        default=0.3,
        help="sampling temperature (also busts the DSPy response cache)",
    )
    translate.set_defaults(func=_cmd_translate)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    setup_logging(logging.DEBUG if args.verbose else logging.WARNING)
    return asyncio.run(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
