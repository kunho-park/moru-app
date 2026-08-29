"""Enabled resource packs supply the effective source text.

Modpack authors rename items with a bundled resource pack rather than by
editing the mod, so the string in the JAR is not the string the player
reads. Translating the JAR's string produces a translation of text that
is not in the game. The scanner reads the instance's ``options.txt``,
resolves the packs it enables (later entries win, the way the game layers
them over ``"vanilla"``) and substitutes their entries into the source
text — values only, so keys and output routing are untouched.
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from moru_engine.scanner import scan_modpack
from moru_engine.scanner.resource_overrides import find_instance_options, parse_enabled_packs

KEY = "item.archers_expansion.deadeye_spell_book"
MOD_TEXT = "Bounty List"
JAR = "archers-expansion-1.2.3-forge.jar"


def _mod_jar(modpack: Path, extra: dict[str, str] | None = None) -> Path:
    """The mod that owns ``KEY``, shipping its own English name for it."""
    path = modpack / "mods" / JAR
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("META-INF/mods.toml", '[[mods]]\nmodId="archers_expansion"\n')
        zf.writestr(
            "assets/archers_expansion/lang/en_us.json",
            json.dumps({KEY: MOD_TEXT, **(extra or {})}, indent=2),
        )
    return path


def _pack_zip(path: Path, entries: dict[str, str], namespace: str = "archers_expansion") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("pack.mcmeta", json.dumps({"pack": {"pack_format": 15}}))
        zf.writestr(f"assets/{namespace}/lang/en_us.json", json.dumps(entries))
    return path


def _pack_dir(path: Path, entries: dict[str, str]) -> Path:
    lang = path / "assets" / "archers_expansion" / "lang"
    lang.mkdir(parents=True, exist_ok=True)
    (lang / "en_us.json").write_text(json.dumps(entries), encoding="utf-8")
    return path


def _options(path: Path, enabled: list[str]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "version:3465\n"
        f"resourcePacks:{json.dumps(enabled)}\n"
        f"incompatibleResourcePacks:{json.dumps(enabled)}\n"
        "lang:en_us\n",
        encoding="utf-8",
    )
    return path


def _extracted_source(modpack: Path) -> dict[str, str]:
    """The source text the scan hands the pipeline for the mod's lang file."""
    path = (
        modpack
        / ".mct_cache"
        / "extracted"
        / JAR
        / "assets"
        / "archers_expansion"
        / "lang"
        / "en_us.json"
    )
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.mark.asyncio
async def test_without_options_txt_the_source_text_is_the_mods_own(
    tmp_path: Path,
) -> None:
    """No instance options must reproduce the pre-feature scan exactly."""
    modpack = tmp_path / "modpack"
    _mod_jar(modpack)
    _pack_zip(modpack / "resourcepacks" / "Renamer.zip", {KEY: "Alnilam's Sight"})

    result = await scan_modpack(modpack)

    assert _extracted_source(modpack) == {KEY: MOD_TEXT}
    assert result.source_overrides == []


@pytest.mark.asyncio
async def test_enabled_pack_replaces_the_source_string(tmp_path: Path) -> None:
    modpack = tmp_path / "modpack"
    _mod_jar(modpack, extra={"item.archers_expansion.plain": "Plain Bow"})
    _pack_zip(modpack / "resourcepacks" / "Renamer.zip", {KEY: "Alnilam's Sight"})
    _options(modpack / "options.txt", ["vanilla", "file/Renamer.zip"])

    result = await scan_modpack(modpack)

    # Overridden value replaced, every other key and the key set intact.
    assert _extracted_source(modpack) == {
        KEY: "Alnilam's Sight",
        "item.archers_expansion.plain": "Plain Bow",
    }
    assert [(o.pack, o.keys) for o in result.source_overrides] == [("Renamer.zip", 1)]


@pytest.mark.asyncio
async def test_later_entry_wins_when_two_packs_override_one_key(
    tmp_path: Path,
) -> None:
    """options.txt is load order: "vanilla" leads it, so the tail wins."""
    modpack = tmp_path / "modpack"
    _mod_jar(modpack)
    _pack_zip(modpack / "resourcepacks" / "Low.zip", {KEY: "Overridden Once"})
    _pack_zip(modpack / "resourcepacks" / "High.zip", {KEY: "Alnilam's Sight"})
    _options(modpack / "options.txt", ["vanilla", "file/Low.zip", "file/High.zip"])

    result = await scan_modpack(modpack)

    assert _extracted_source(modpack)[KEY] == "Alnilam's Sight"
    assert [(o.pack, o.keys) for o in result.source_overrides] == [("High.zip", 1)]


@pytest.mark.asyncio
async def test_builtins_and_missing_packs_are_ignored(tmp_path: Path) -> None:
    """A deleted pack and the built-in layers must not disturb the scan."""
    modpack = tmp_path / "modpack"
    _mod_jar(modpack)
    _pack_zip(modpack / "resourcepacks" / "Renamer.zip", {KEY: "Alnilam's Sight"})
    _options(
        modpack / "options.txt",
        [
            "vanilla",
            "fabric",
            "mod_resources",
            "programer_art",
            "file/Deleted.zip",
            "file/Renamer.zip",
        ],
    )

    result = await scan_modpack(modpack)

    assert _extracted_source(modpack)[KEY] == "Alnilam's Sight"
    assert [o.pack for o in result.source_overrides] == ["Renamer.zip"]


@pytest.mark.asyncio
async def test_only_missing_packs_leaves_the_source_untouched(tmp_path: Path) -> None:
    modpack = tmp_path / "modpack"
    _mod_jar(modpack)
    _options(modpack / "options.txt", ["vanilla", "file/Deleted.zip"])

    result = await scan_modpack(modpack)

    assert _extracted_source(modpack) == {KEY: MOD_TEXT}
    assert result.source_overrides == []


@pytest.mark.asyncio
async def test_unpacked_pack_directory_is_read(tmp_path: Path) -> None:
    """Users unzip packs; a directory pack is as enabled as a ZIP."""
    modpack = tmp_path / "modpack"
    _mod_jar(modpack)
    _pack_dir(modpack / "resourcepacks" / "Loose", {KEY: "Alnilam's Sight"})
    _options(modpack / "options.txt", ["vanilla", "file/Loose"])

    result = await scan_modpack(modpack)

    assert _extracted_source(modpack)[KEY] == "Alnilam's Sight"
    assert [o.pack for o in result.source_overrides] == ["Loose"]


@pytest.mark.asyncio
async def test_pack_entry_for_another_namespace_is_not_applied(
    tmp_path: Path,
) -> None:
    """Keys are namespaced; a same-named key elsewhere is a different key."""
    modpack = tmp_path / "modpack"
    _mod_jar(modpack)
    _pack_zip(
        modpack / "resourcepacks" / "Other.zip",
        {KEY: "Wrong Namespace"},
        namespace="someothermod",
    )
    _options(modpack / "options.txt", ["file/Other.zip"])

    result = await scan_modpack(modpack)

    assert _extracted_source(modpack) == {KEY: MOD_TEXT}
    assert result.source_overrides == []


@pytest.mark.asyncio
async def test_options_txt_is_found_in_a_nested_game_dir(tmp_path: Path) -> None:
    """Prism/MultiMC users point at the instance root, not the game dir."""
    instance = tmp_path / "instance"
    modpack = instance / ".minecraft"
    _mod_jar(modpack)
    _pack_zip(modpack / "resourcepacks" / "Renamer.zip", {KEY: "Alnilam's Sight"})
    _options(modpack / "options.txt", ["file/Renamer.zip"])

    located = find_instance_options(instance)

    assert located is not None
    assert located.options_path == modpack / "options.txt"
    assert located.resourcepacks_root == modpack / "resourcepacks"


@pytest.mark.asyncio
async def test_pack_shipped_default_options_are_used(tmp_path: Path) -> None:
    """A freshly installed pack has no options.txt yet - only the author's.

    ConfiguredDefaults ships the selection at ``configureddefaults/`` and
    the game copies it on first launch, so this is the only enabled-pack
    list a never-launched instance has.
    """
    modpack = tmp_path / "modpack"
    _mod_jar(modpack)
    _pack_zip(modpack / "resourcepacks" / "Renamer.zip", {KEY: "Alnilam's Sight"})
    _options(modpack / "configureddefaults" / "options.txt", ["file/Renamer.zip"])

    result = await scan_modpack(modpack)

    assert _extracted_source(modpack)[KEY] == "Alnilam's Sight"
    assert [o.pack for o in result.source_overrides] == ["Renamer.zip"]


@pytest.mark.asyncio
async def test_real_options_txt_beats_pack_shipped_defaults(tmp_path: Path) -> None:
    """Once the user has launched the game, their own selection rules."""
    modpack = tmp_path / "modpack"
    _mod_jar(modpack)
    _pack_zip(modpack / "resourcepacks" / "Author.zip", {KEY: "Author Choice"})
    _pack_zip(modpack / "resourcepacks" / "User.zip", {KEY: "User Choice"})
    _options(modpack / "configureddefaults" / "options.txt", ["file/Author.zip"])
    _options(modpack / "options.txt", ["file/User.zip"])

    result = await scan_modpack(modpack)

    assert _extracted_source(modpack)[KEY] == "User Choice"
    assert [o.pack for o in result.source_overrides] == ["User.zip"]


@pytest.mark.asyncio
async def test_legacy_lang_pack_overrides_a_legacy_lang_mod(tmp_path: Path) -> None:
    """1.12-era packs and mods use key=value ``.lang`` files."""
    modpack = tmp_path / "modpack"
    jar = modpack / "mods" / "legacy-mod.jar"
    jar.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(jar, "w") as zf:
        zf.writestr("mcmod.info", json.dumps([{"modid": "legacymod"}]))
        zf.writestr(
            "assets/legacymod/lang/en_us.lang",
            "# a comment\nitem.legacymod.wand.name=Copper Wand\nitem.legacymod.rod.name=Rod\n",
        )
    pack = modpack / "resourcepacks" / "Legacy.zip"
    pack.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(pack, "w") as zf:
        zf.writestr("assets/legacymod/lang/en_us.lang", "item.legacymod.wand.name=Brass Wand\n")
    _options(modpack / "options.txt", ["file/Legacy.zip"])

    result = await scan_modpack(modpack)

    extracted = (
        modpack / ".mct_cache" / "extracted" / "legacy-mod.jar"
        / "assets" / "legacymod" / "lang" / "en_us.lang"
    ).read_text(encoding="utf-8")
    assert "item.legacymod.wand.name=Brass Wand" in extracted
    assert "item.legacymod.rod.name=Rod" in extracted
    assert [(o.pack, o.keys) for o in result.source_overrides] == [("Legacy.zip", 1)]


def test_incompatible_pack_line_is_not_the_enabled_list() -> None:
    """``incompatibleResourcePacks`` sits right beside the real line."""
    text = (
        'incompatibleResourcePacks:["file/Old.zip"]\n'
        'resourcePacks:["vanilla","file/New.zip"]\n'
    )
    assert parse_enabled_packs(text) == ["vanilla", "file/New.zip"]


def test_unparseable_options_line_yields_no_packs() -> None:
    assert parse_enabled_packs("resourcePacks:[not json\n") == []
    assert parse_enabled_packs("version:3465\n") == []
