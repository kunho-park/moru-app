"""Launcher-metadata pack identity detection.

Answers "which published modpack is this folder?" from files launchers
leave behind (CurseForge app instance, CurseForge export manifest,
Modrinth pack index, Prism/MultiMC instance config), so the desktop can
prefill the upload form and link the pack to its CurseForge/Modrinth
project without any network calls. Every parser is failure-tolerant: a
missing or corrupt file just means "not this source", and detection
always ends at the folder-name fallback. Lookups probe a fixed set of
candidate paths only — never a recursive walk.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

#: Prism/MultiMC game dirs nested inside an instance root.
_GAME_DIR_NAMES = (".minecraft", "minecraft")

#: mmc-pack.json component uid -> loader name.
_MMC_LOADER_UIDS = {
    "net.minecraftforge": "forge",
    "net.neoforged": "neoforge",
    "net.fabricmc.fabric-loader": "fabric",
    "org.quiltmc.quilt-loader": "quilt",
}

#: modrinth.index.json dependency key -> loader name.
_MODRINTH_LOADER_KEYS = {
    "forge": "forge",
    "neoforge": "neoforge",
    "fabric-loader": "fabric",
    "quilt-loader": "quilt",
}


@dataclass
class PackIdentity:
    """Identity of a local modpack derived from launcher metadata.

    Mirrors the ScanResult ``identity`` object in
    contracts/engine-api.yaml. ``confident`` is False only for the
    folder-name fallback, where nothing but the directory name is known.
    """

    name: str | None = None
    version: str | None = None
    mc_version: str | None = None
    loader: str | None = None
    curseforge_project_id: int | None = None
    curseforge_file_id: int | None = None
    modrinth_project_id: str | None = None
    modrinth_version_id: str | None = None
    source: str = "folder"
    confident: bool = False


def detect_pack_identity(modpack_path: Path) -> PackIdentity:
    """Detect the pack identity for a modpack folder; first hit wins.

    Source priority: CurseForge app instance > CurseForge export
    manifest > Modrinth pack index > Prism/MultiMC instance config >
    folder-name fallback. Manifest-style files are also probed one level
    down (``.minecraft``/``minecraft`` game subdirs) and — for the
    CurseForge files — one level up when the given path *is* a game dir,
    since users may point at either the instance root or the game dir.
    """
    root = Path(modpack_path)
    down = [root, *(root / d for d in _GAME_DIR_NAMES)]
    up = [root.parent] if root.name in _GAME_DIR_NAMES else []

    for directory in (*down, *up):
        identity = _from_curseforge_instance(directory / "minecraftinstance.json")
        if identity is not None:
            return identity
    for directory in (*down, *up):
        identity = _from_curseforge_manifest(directory / "manifest.json")
        if identity is not None:
            return identity
    for directory in down:
        identity = _from_modrinth_index(directory / "modrinth.index.json")
        if identity is not None:
            return identity
    # Prism keeps instance.cfg in the instance root, one level above the
    # game dir the user typically selects.
    for directory in (root, root.parent):
        identity = _from_prism_instance(directory / "instance.cfg")
        if identity is not None:
            return identity

    name = root.name
    if name in _GAME_DIR_NAMES and root.parent.name:
        name = root.parent.name  # ".minecraft" names the game dir, not the pack
    return PackIdentity(name=name or None, source="folder", confident=False)


# -- shared tolerant readers ---------------------------------------------------


def _load_json(path: Path) -> Any:
    """Parse a JSON file; any I/O or syntax problem means "not this source"."""
    try:
        with path.open(encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _str_or_none(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value
    return None


def _int_or_none(value: Any) -> int | None:
    """Positive int from an int or numeric string (Prism stores ids as text)."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip()) or None
    return None


def _loader_prefix(value: Any) -> str | None:
    """CurseForge encodes loaders as "name-version": 'forge-47.2.0' -> 'forge'."""
    text = _str_or_none(value)
    if text is None:
        return None
    return text.split("-", 1)[0].strip().lower() or None


#: Archive suffixes launchers leave on file-derived version strings.
_ARCHIVE_SUFFIX_RE = re.compile(r"\.(zip|mrpack)$", re.IGNORECASE)

#: First token that *starts* with a digit (optionally prefixed with a lone
#: ``v``). Tokens are ``-``/``_``/space separated; a digit inside a token
#: ("ATM10") does not count.
_VERSION_TAIL_RE = re.compile(r"(?:^|(?<=[-_ ]))[vV]?(?=\d)")


def _clean_version(value: Any) -> str | None:
    """Human version string from launcher metadata, minus the noise.

    CurseForge file display names repeat the whole pack name
    ("Boosted FPS Fabric-26.2-1.7.4", "ATM10-2.32"); every surface that
    shows the version (upload form, web version matrix) already shows the
    pack name next to it, so keep only the tail from the first
    digit-leading token on ("26.2-1.7.4", "2.32"). A leading ``v`` marker
    is dropped too — displays add their own. Strings with no digit-leading
    token pass through unchanged (minus archive suffixes).
    """
    text = _str_or_none(value)
    if text is None:
        return None
    text = _ARCHIVE_SUFFIX_RE.sub("", text.strip()).strip()
    match = _VERSION_TAIL_RE.search(text)
    if match is not None:
        text = text[match.end() :]
    return text or None


# -- per-source detectors ------------------------------------------------------


def _from_curseforge_instance(path: Path) -> PackIdentity | None:
    """CurseForge app launcher instance (minecraftinstance.json)."""
    data = _load_json(path)
    if not isinstance(data, dict):
        return None
    modpack = _dict(data.get("installedModpack"))
    installed = _dict(modpack.get("installedFile"))
    base_loader = _dict(data.get("baseModLoader"))
    version = _clean_version(installed.get("displayName"))
    if version is None:
        file_name = _str_or_none(installed.get("fileNameOnDisk"))
        if file_name is not None:
            version = _clean_version(Path(file_name).stem or file_name)
    return PackIdentity(
        name=_str_or_none(data.get("name")),
        version=version,
        mc_version=_str_or_none(data.get("gameVersion"))
        or _str_or_none(base_loader.get("minecraftVersion")),
        loader=_loader_prefix(base_loader.get("name")),
        curseforge_project_id=_int_or_none(modpack.get("addonID"))
        or _int_or_none(installed.get("projectId")),
        curseforge_file_id=_int_or_none(installed.get("id")),
        source="curseforge_instance",
        confident=True,
    )


def _from_curseforge_manifest(path: Path) -> PackIdentity | None:
    """CurseForge export zip manifest (manifest.json). Carries no project id."""
    data = _load_json(path)
    if not isinstance(data, dict) or data.get("manifestType") != "minecraftModpack":
        return None
    minecraft = _dict(data.get("minecraft"))
    loaders = minecraft.get("modLoaders")
    first = loaders[0] if isinstance(loaders, list) and loaders else None
    return PackIdentity(
        name=_str_or_none(data.get("name")),
        version=_clean_version(data.get("version")),
        mc_version=_str_or_none(minecraft.get("version")),
        loader=_loader_prefix(_dict(first).get("id")),
        source="curseforge_manifest",
        confident=True,
    )


def _from_modrinth_index(path: Path) -> PackIdentity | None:
    """Modrinth pack index (modrinth.index.json). versionId doubles as version."""
    data = _load_json(path)
    if not isinstance(data, dict):
        return None
    deps = _dict(data.get("dependencies"))
    version_id = _str_or_none(data.get("versionId"))
    return PackIdentity(
        name=_str_or_none(data.get("name")),
        version=_clean_version(version_id),
        mc_version=_str_or_none(deps.get("minecraft")),
        loader=next(
            (name for key, name in _MODRINTH_LOADER_KEYS.items() if key in deps),
            None,
        ),
        modrinth_version_id=version_id,
        source="modrinth_pack",
        confident=True,
    )


def _parse_cfg(path: Path) -> dict[str, str] | None:
    """instance.cfg as key=value lines, tolerating the INI ``[General]``
    header newer Prism versions write. A raw parser instead of
    configparser: configparser lowercases keys (breaking ManagedPackType
    lookups) and rejects header-less MultiMC files.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    values: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith(("[", "#", ";")):
            continue
        key, sep, value = line.partition("=")
        if sep:
            values[key.strip()] = value.strip()
    return values or None


def _from_prism_instance(path: Path) -> PackIdentity | None:
    """Prism/MultiMC instance.cfg, plus sibling mmc-pack.json components.

    ManagedPack instances know their upstream project (flame = CurseForge,
    modrinth); unmanaged ones still carry the launcher-given name, which is
    real metadata — hence confident=True either way.
    """
    cfg = _parse_cfg(path)
    if cfg is None:
        return None
    identity = PackIdentity(
        name=_str_or_none(cfg.get("name")),
        source="prism_instance",
        confident=True,
    )
    if cfg.get("ManagedPack", "").strip().lower() == "true":
        pack_type = cfg.get("ManagedPackType", "").strip().lower()
        if pack_type == "flame":
            identity.source = "prism_managed"
            identity.curseforge_project_id = _int_or_none(cfg.get("ManagedPackID"))
            identity.curseforge_file_id = _int_or_none(cfg.get("ManagedPackVersionID"))
            identity.version = _clean_version(cfg.get("ManagedPackVersionName"))
        elif pack_type == "modrinth":
            identity.source = "prism_managed"
            identity.modrinth_project_id = _str_or_none(cfg.get("ManagedPackID"))
            identity.modrinth_version_id = _str_or_none(cfg.get("ManagedPackVersionID"))
            identity.version = _clean_version(cfg.get("ManagedPackVersionName"))
    _apply_mmc_pack(path.parent / "mmc-pack.json", identity)
    return identity


def _apply_mmc_pack(path: Path, identity: PackIdentity) -> None:
    """Fill mc_version/loader from the mmc-pack.json component list."""
    components = _dict(_load_json(path)).get("components")
    if not isinstance(components, list):
        return
    for component in components:
        if not isinstance(component, dict):
            continue
        uid = component.get("uid")
        if uid == "net.minecraft":
            identity.mc_version = _str_or_none(component.get("version"))
        elif uid in _MMC_LOADER_UIDS:
            identity.loader = _MMC_LOADER_UIDS[uid]
