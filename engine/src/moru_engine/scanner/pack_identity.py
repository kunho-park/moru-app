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


# -- version compatibility -----------------------------------------------------

#: A modpack version we can *order*: a purely dotted numeric release.
#: Real-world values are not semver ("v6.5.4hotfix", "1.20.1-3.2b") and no
#: ordering can be invented for a non-numeric tail — is "3.2b" before or
#: after "3.2"? Such a version is incomparable and only ever matches itself,
#: string for string.
_NUMERIC_RELEASE_RE = re.compile(r"^\d{1,6}(?:\.\d{1,6})*$")


def pack_version_key(value: Any) -> tuple[int, ...] | None:
    """Orderable form of a modpack version, or None when incomparable.

    Cleaned by :func:`_clean_version` first, so a bound typed into the
    export form ("v4.2") normalizes exactly like a detected version.
    """
    text = _clean_version(value)
    if text is None or _NUMERIC_RELEASE_RE.match(text) is None:
        return None
    return tuple(int(part) for part in text.split("."))


def same_pack_version(left: Any, right: Any) -> bool:
    """Do two modpack version strings name the same version?

    Cleaned strings settle it; comparable releases also match through their
    numeric key, so "4.01.2" and "4.1.2" are the same release. Two unknown
    versions are never "the same" — nothing was established about either.
    """
    low, high = _clean_version(left), _clean_version(right)
    if low is None or high is None:
        return False
    if low == high:
        return True
    key = pack_version_key(low)
    return key is not None and key == pack_version_key(high)


def normalize_mc_version(value: Any) -> str | None:
    """Comparison form of a Minecraft version; None when unknown.

    Minecraft versions are only ever compared for equality here — never
    ordered — so trimming and case folding is the whole normalization
    ("1.20.1", "23w45a").
    """
    text = _str_or_none(value)
    return text.strip().lower() if text is not None else None


@dataclass(frozen=True)
class VersionRange:
    """Inclusive modpack version range a translation pack is published for
    (web-api.yaml ``TranslationPackCreate.compatible_versions``).

    ``min`` is the version the pack was actually built against; ``max`` is
    the author's "still fine up to here" claim, which defaults to ``min`` —
    exactly the exact-version behaviour that predates the field.
    """

    min: str
    max: str

    def contains(self, version: Any) -> bool:
        """Is ``version`` inside this range?

        Ordering needs all three of ``min``/``max``/``version`` to be
        comparable releases. The moment one is not, the range cannot be
        evaluated at all and only an exact match against an endpoint counts:
        a translation pack is never offered on a guess. An inverted range
        (``max`` below ``min``) likewise contains nothing.
        """
        low, high = pack_version_key(self.min), pack_version_key(self.max)
        target = pack_version_key(version)
        if low is None or high is None or target is None:
            return same_pack_version(version, self.min) or same_pack_version(
                version, self.max
            )
        return low <= target <= high


def parse_version_range(value: Any) -> VersionRange | None:
    """``{"min", "max"}`` off the wire; None when a pack declares no range.

    Packs published before the field existed carry none, which is what keeps
    them resolving by exact version.
    """
    data = _dict(value)
    low, high = _clean_version(data.get("min")), _clean_version(data.get("max"))
    if low is None or high is None:
        return None
    return VersionRange(min=low, max=high)


def declare_version_range(
    version: Any, compatible_up_to: Any = None
) -> VersionRange | None:
    """The range to publish for a pack built against ``version``.

    Defaults to the point range ``[version, version]``, so a pack never
    claims a compatibility its author did not state. ``compatible_up_to`` is
    free text from the export form: it widens the range only when it is a
    comparable release that is not below the built version, and is otherwise
    dropped rather than published as a claim nobody can check. None when the
    built version itself is unknown — there is nothing to anchor a range to.
    """
    base = _clean_version(version)
    if base is None:
        return None
    upper = _clean_version(compatible_up_to)
    if upper is not None and upper != base:
        low, high = pack_version_key(base), pack_version_key(upper)
        if low is not None and high is not None and low <= high:
            return VersionRange(min=base, max=upper)
    return VersionRange(min=base, max=base)


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
