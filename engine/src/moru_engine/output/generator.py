"""Installable output generation: resource pack + overrides.

The pipeline produces per-file translation maps; this module turns them
into the two artifacts a player actually installs:

- ``resourcepack/`` — ``pack.mcmeta`` plus ``assets/<ns>/lang/*``.
  Only *fresh* entries are written: language keys merge across packs at
  runtime, so re-shipping translations the modpack already contains is
  pure bloat — and worse, template-merging dumps would re-emit the
  English source for those keys, overriding the pack's own translations.
- ``overrides/`` — files a resource pack cannot carry (kubejs/, config/,
  scripts/, ftbquests/, patchouli_books/). They are copied over the
  modpack root and replace whole files, so they carry the *full* merged
  state (fresh + pre-existing target entries); a fresh-only file would
  wipe translations the modpack already shipped.

A file with no fresh entry is skipped entirely — the modpack already has
that translation. JAR-internal ``data/`` files would require patching the
mod ``.jar`` (unsupported); they are counted and skipped.
"""

from __future__ import annotations

import json
import logging
import shutil
import zipfile
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

import aiofiles

from ..handlers.base import create_default_registry
from ..parsers import BaseParser, LangParser, ParserError
from ..utils.locale_helper import replace_locale_in_path

logger = logging.getLogger(__name__)

#: MC 1.20.x. Packs with a mismatched format still load after a confirm
#: prompt, so one sane default beats a per-version table for now.
DEFAULT_PACK_FORMAT = 15

RESOURCEPACK_DIRNAME = "resourcepack"
OVERRIDES_DIRNAME = "overrides"

#: Bundled moru anvil icon, shipped as ``pack.png`` so the generated pack
#: is recognizable in the resource-pack selection screen.
PACK_ICON_ASSET = Path(__file__).resolve().parents[1] / "assets" / "pack.png"

#: Path fragments (lowercased, ``/``-normalized) that mark a file as a
#: modpack-root override rather than resource-pack content.
OVERRIDE_MARKERS = (
    "kubejs/",
    "config/",
    "scripts/",
    "/ftbquests/",
    "patchouli_books/",
)

#: Fragments marking content extracted out of an archive by the scanner.
EXTRACTED_MARKERS = (".mct_cache", "extracted")


class Route(Enum):
    """Destination of one translated file."""

    RESOURCE_PACK = "resource_pack"
    OVERRIDE = "override"
    #: JAR-internal data/ file — needs .jar patching, unsupported.
    SKIP_JAR_DATA = "skip_jar_data"
    #: Override-pattern file inside an extracted archive — nothing on disk
    #: to overwrite, and a resource pack cannot carry it either.
    SKIP_EXTRACTED = "skip_extracted"


@dataclass(slots=True)
class FileOutput:
    """Translation state of one source file, ready to be routed."""

    source_path: Path
    #: Entries newly produced by this run (LLM, TM hits, review edits).
    fresh: dict[str, str]
    #: ``fresh`` plus entries whose translation pre-existed in the modpack.
    full: dict[str, str]
    namespace: str = ""


@dataclass(slots=True)
class OutputConfig:
    modpack_root: Path
    output_dir: Path
    source_locale: str = "en_us"
    target_locale: str = "ko_kr"
    pack_format: int = DEFAULT_PACK_FORMAT
    description: str = "§a모루§7로 번역됨 — §amoru.gg"


@dataclass(slots=True)
class GenerationResult:
    resourcepack_dir: Path
    overrides_dir: Path
    resourcepack_files: list[Path] = field(default_factory=list)
    override_files: list[Path] = field(default_factory=list)
    pack_mcmeta: Path | None = None
    pack_icon: Path | None = None
    #: Files skipped because every entry already had a translation.
    skipped_existing: int = 0
    #: Files skipped because they would require .jar patching.
    skipped_jar_data: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def all_files(self) -> list[Path]:
        files = [*self.resourcepack_files, *self.override_files]
        if self.pack_mcmeta is not None:
            files.append(self.pack_mcmeta)
        if self.pack_icon is not None:
            files.append(self.pack_icon)
        return files


def _norm(path: Path | str) -> str:
    return str(path).replace("\\", "/").lower()


def create_zip_from_directory(source_dir: Path, zip_path: Path) -> None:
    """Zip ``source_dir`` contents (arcnames relative to it)."""
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for file_path in sorted(source_dir.rglob("*")):
            if file_path.is_file():
                zf.write(file_path, file_path.relative_to(source_dir))


def route_for(source_path: Path | str) -> Route:
    """Classify one source file. Pure; used by generator and tests."""
    norm = _norm(source_path)
    extracted = any(marker in norm for marker in EXTRACTED_MARKERS)
    if extracted and "/data/" in norm:
        return Route.SKIP_JAR_DATA
    if any(marker in norm for marker in OVERRIDE_MARKERS):
        if not extracted:
            return Route.OVERRIDE
        # Inside an archive only asset-tree content (e.g. a mod's builtin
        # patchouli book) can ride along in the resource pack.
        if "patchouli_books/" in norm and "/assets/" in norm:
            return Route.RESOURCE_PACK
        return Route.SKIP_EXTRACTED
    return Route.RESOURCE_PACK


class OutputGenerator:
    """Writes the ``resourcepack/`` + ``overrides/`` trees for one run."""

    def __init__(self, config: OutputConfig) -> None:
        self.config = config
        self.registry = create_default_registry()

    @property
    def resourcepack_dir(self) -> Path:
        return self.config.output_dir / RESOURCEPACK_DIRNAME

    @property
    def overrides_dir(self) -> Path:
        return self.config.output_dir / OVERRIDES_DIRNAME

    async def generate(self, files: list[FileOutput]) -> GenerationResult:
        result = GenerationResult(
            resourcepack_dir=self.resourcepack_dir,
            overrides_dir=self.overrides_dir,
        )
        # Both trees are fully derived artifacts: regenerate from scratch
        # so stale files from a previous run never leak into the zips.
        for tree in (self.resourcepack_dir, self.overrides_dir):
            if tree.exists():
                shutil.rmtree(tree)

        # Language files from several sources (mod JAR + resourcepack zip
        # + overlay) can map to the same output file; merge them so the
        # last-scanned source wins per key instead of per file.
        lang_buckets: dict[Path, dict[str, str]] = {}
        bucket_sources: dict[Path, Path] = {}

        for file in files:
            routed = route_for(file.source_path)
            if routed is Route.SKIP_JAR_DATA:
                result.skipped_jar_data += 1
                logger.info(
                    "Skipping JAR-internal data file (needs .jar patching): %s",
                    file.source_path,
                )
                continue
            if routed is Route.SKIP_EXTRACTED:
                logger.debug(
                    "Skipping extracted override candidate: %s", file.source_path
                )
                continue
            if not file.fresh:
                # Everything in this file already had a translation.
                result.skipped_existing += 1
                continue

            try:
                if routed is Route.OVERRIDE:
                    written = await self._write_override(file)
                    if written is not None:
                        result.override_files.append(written)
                    continue

                handler = self.registry.get_handler(file.source_path)
                if handler is not None and handler.name != "language":
                    # Structured asset (patchouli book, tconstruct book…):
                    # whole-file format, must carry the full merged state.
                    written = await self._write_structured_asset(file)
                    if written is not None:
                        result.resourcepack_files.append(written)
                    continue

                output_path = self._lang_output_path(file)
                bucket = lang_buckets.setdefault(output_path, {})
                bucket.update(file.fresh)
                bucket_sources.setdefault(output_path, file.source_path)
            except (OSError, ValueError, TypeError, ParserError) as exc:
                message = f"Failed to generate output for {file.source_path}: {exc}"
                logger.error(message)
                result.errors.append(message)

        for output_path, data in lang_buckets.items():
            try:
                await self._write_lang_file(
                    output_path, bucket_sources[output_path], data
                )
                result.resourcepack_files.append(output_path)
            except (OSError, ValueError, TypeError, ParserError) as exc:
                message = f"Failed to write lang file {output_path}: {exc}"
                logger.error(message)
                result.errors.append(message)

        if result.resourcepack_files:
            result.pack_mcmeta = await self._write_pack_mcmeta()
            result.pack_icon = self._write_pack_icon()

        logger.info(
            "Output generation: %d resourcepack + %d override files "
            "(%d already translated, %d jar-data skipped, %d errors)",
            len(result.resourcepack_files),
            len(result.override_files),
            result.skipped_existing,
            result.skipped_jar_data,
            len(result.errors),
        )
        return result

    # -- resource pack -----------------------------------------------------

    async def _write_pack_mcmeta(self) -> Path:
        mcmeta = {
            "pack": {
                "pack_format": self.config.pack_format,
                "description": self.config.description,
            }
        }
        path = self.resourcepack_dir / "pack.mcmeta"
        path.parent.mkdir(parents=True, exist_ok=True)
        async with aiofiles.open(path, "w", encoding="utf-8") as f:
            await f.write(json.dumps(mcmeta, ensure_ascii=False, indent=2))
        return path

    def _write_pack_icon(self) -> Path | None:
        """Copy the bundled moru icon next to pack.mcmeta as ``pack.png``."""
        if not PACK_ICON_ASSET.is_file():
            logger.warning("Pack icon asset missing: %s", PACK_ICON_ASSET)
            return None
        path = self.resourcepack_dir / "pack.png"
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(PACK_ICON_ASSET, path)
        return path

    def _asset_relative(self, source_path: Path) -> str | None:
        """Path below the asset root, or None when there is no such root.

        Recognized layouts (case preserved from the original path):
        - ``…/assets/<ns>/…`` — standard resource pack / mod JAR tree.
        - ``…/resources/<ns>/…`` — 1.12.x launcher overlay where
          ``resources/`` itself is the asset root.
        """
        source_str = str(source_path).replace("\\", "/")
        lower = source_str.lower()
        for marker in ("/assets/", "/resources/"):
            idx = lower.find(marker)
            if idx >= 0:
                rel = source_str[idx + len(marker) :]
                return replace_locale_in_path(
                    rel, self.config.source_locale, self.config.target_locale
                )
        return None

    def _lang_output_path(self, file: FileOutput) -> Path:
        assets_dir = self.resourcepack_dir / "assets"
        rel = self._asset_relative(file.source_path)
        if rel is not None:
            return assets_dir / rel
        # Handler-extracted files without an assets/ segment still land
        # somewhere the game can load them.
        ns = file.namespace or "minecraft"
        suffix = file.source_path.suffix or ".json"
        return assets_dir / ns / "lang" / f"{self.config.target_locale}{suffix}"

    async def _write_lang_file(
        self, output_path: Path, source_path: Path, data: dict[str, str]
    ) -> None:
        """Write a merged, fresh-only language file.

        Deliberately NOT the template-merging JSON parser: that re-emits
        every source key, putting English text back over keys the modpack
        already translated. ``.lang`` uses the parser (its dump writes only
        the given entries and preserves ``#PARSE_ESCAPES``); everything
        else becomes a flat JSON object.
        """
        output_path.parent.mkdir(parents=True, exist_ok=True)
        if output_path.suffix.lower() == ".lang":
            await LangParser(output_path, original_path=source_path).dump(data)
            return
        payload = json.dumps(
            dict(sorted(data.items())), ensure_ascii=False, indent=2
        )
        async with aiofiles.open(output_path, "w", encoding="utf-8") as f:
            await f.write(payload)

    async def _write_structured_asset(self, file: FileOutput) -> Path | None:
        rel = self._asset_relative(file.source_path)
        if rel is None:
            logger.warning(
                "Cannot mirror structured asset (no assets/ segment): %s",
                file.source_path,
            )
            return None
        output_path = self.resourcepack_dir / "assets" / rel
        output_path.parent.mkdir(parents=True, exist_ok=True)
        handler = self.registry.get_handler(file.source_path)
        assert handler is not None  # caller routed via get_handler
        await handler.apply(file.source_path, file.full, output_path)
        return output_path

    # -- overrides -----------------------------------------------------------

    async def _write_override(self, file: FileOutput) -> Path | None:
        source_path = file.source_path
        try:
            rel = source_path.resolve().relative_to(
                self.config.modpack_root.resolve()
            ).as_posix()
        except ValueError:
            rel = source_path.name
        rel = replace_locale_in_path(
            rel, self.config.source_locale, self.config.target_locale
        )
        output_path = self.overrides_dir / rel
        output_path.parent.mkdir(parents=True, exist_ok=True)

        handler = self.registry.get_handler(source_path)
        if handler is not None:
            await handler.apply(source_path, file.full, output_path)
            return output_path

        parser = BaseParser.create_parser(output_path, source_path)
        if parser is None:
            logger.warning("No handler or parser for override: %s", source_path)
            return None
        await parser.dump(file.full)
        return output_path
