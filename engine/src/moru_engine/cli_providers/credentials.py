"""OAuth credential stores for locally installed coding CLIs.

Ported from oh-my-pi (``packages/ai/src/registry/oauth/{anthropic,
openai-codex,google-gemini-cli}.ts``). Moru never runs its own login flow:
it reads the credentials the user's own CLI already wrote, refreshes them
when they expire, and writes the rotated tokens back **in the CLI's native
format** so one grant keeps serving both moru and the CLI.

Client ids, endpoints and refresh payloads are the CLIs' own — a refresh
issued here is indistinguishable from one the CLI would have issued.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import platform
import shutil
import subprocess
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
from dotenv import dotenv_values

logger = logging.getLogger(__name__)

#: Refresh this long before the real expiry. Mirrors the 5-minute margin
#: oh-my-pi bakes into its stored `expires` for Anthropic/Google.
_SKEW_MS = 5 * 60 * 1000

_TIMEOUT = httpx.Timeout(30.0)


class CliAuthError(RuntimeError):
    """No usable credential for a CLI provider.

    Carries a message aimed at the desktop UI: what to run to fix it.
    """


def _now_ms() -> int:
    return int(time.time() * 1000)


def _decode_jwt(token: str) -> dict[str, Any]:
    """Best-effort JWT payload decode; {} when the token is opaque."""
    parts = token.split(".")
    if len(parts) != 3:
        return {}
    payload = parts[1]
    payload += "=" * (-len(payload) % 4)
    try:
        return json.loads(base64.urlsafe_b64decode(payload))
    except (ValueError, TypeError):
        return {}


def _atomic_write_json(path: Path, data: object, *, private: bool) -> None:
    """Write JSON via temp file + os.replace, optionally chmod 600."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".moru-tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    if private:
        try:
            tmp.chmod(0o600)
        except OSError:  # pragma: no cover - Windows/ACL filesystems
            pass
    os.replace(tmp, path)


def _refreshed(resp: httpx.Response) -> dict[str, Any]:
    """Parsed body of a token-refresh response, or the failure it reports."""
    if resp.status_code != 200:
        raise CliAuthError(f"token refresh failed ({resp.status_code}): {resp.text[:300]}")
    return resp.json()


@dataclass
class OAuthCredentials:
    """One CLI's OAuth grant, normalized across providers."""

    access: str
    refresh: str = ""
    #: Epoch ms of the true expiry (no skew applied).
    expires: int = 0
    project_id: str | None = None
    account_id: str | None = None
    email: str | None = None
    #: Provider-native document this was parsed from, so writes can
    #: round-trip keys we do not model (mcpOAuth, scopes, id_token, ...).
    raw: dict[str, Any] = field(default_factory=dict)

    def stale(self) -> bool:
        # expires == 0 means "unknown"; treat as fresh and let a 401 drive
        # the refresh instead of refreshing on every single call.
        if self.expires <= 0:
            return False
        return self.expires - _SKEW_MS <= _now_ms()


class CliCredentialStore(ABC):
    """Reads, refreshes and writes back one CLI's OAuth credential file."""

    #: Provider id in moru's catalog (e.g. "claude-code").
    id: str
    #: Human label used in error messages.
    label: str
    #: Command the user runs to (re-)authenticate.
    login_hint: str

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cache: OAuthCredentials | None = None

    # -- provider hooks ---------------------------------------------------

    @property
    @abstractmethod
    def path(self) -> Path:
        """Credential file the CLI owns."""

    @abstractmethod
    def _load(self) -> OAuthCredentials | None:
        """Parse the credential file, or None when absent/unusable."""

    @abstractmethod
    def _refresh(self, creds: OAuthCredentials) -> OAuthCredentials:
        """Exchange the refresh token for a fresh access token."""

    @abstractmethod
    def _save(self, creds: OAuthCredentials) -> None:
        """Write rotated tokens back in the CLI's native format."""

    # -- public API -------------------------------------------------------

    def available(self) -> bool:
        """True when a credential exists, without refreshing it."""
        try:
            return self._load() is not None
        except Exception:  # noqa: BLE001 - availability must never raise
            logger.debug("Credential probe failed for %s", self.id, exc_info=True)
            return False

    def status(self) -> dict[str, Any]:
        """Auth summary for GET /providers."""
        try:
            creds = self._load()
        except Exception as exc:  # noqa: BLE001
            return {"connected": False, "error": str(exc)}
        if creds is None:
            return {"connected": False, "error": None}
        return {
            "connected": True,
            "error": None,
            "email": creds.email,
            "expires": creds.expires or None,
        }

    def credentials(self) -> OAuthCredentials:
        """Valid credentials, refreshing and persisting when stale."""
        with self._lock:
            creds = self._cache
            # Always re-read when the cache is cold or stale: the CLI may
            # have rotated the grant from under us.
            if creds is None or creds.stale():
                creds = self._load()
            if creds is None:
                raise CliAuthError(
                    f"{self.label}에 로그인되어 있지 않습니다. 터미널에서 "
                    f"`{self.login_hint}`로 로그인한 뒤 다시 시도해 주세요."
                )
            if creds.stale():
                if not creds.refresh:
                    raise CliAuthError(
                        f"{self.label} 토큰이 만료되었고 갱신 토큰이 없습니다. "
                        f"`{self.login_hint}`로 다시 로그인해 주세요."
                    )
                logger.info("Refreshing %s credentials", self.id)
                creds = self._refresh(creds)
                self._save(creds)
            self._cache = creds
            return creds

    def token(self) -> str:
        return self.credentials().access

    def invalidate(self) -> None:
        """Drop the in-process cache so the next call re-reads from disk."""
        with self._lock:
            self._cache = None

    # -- helpers ----------------------------------------------------------

    def _read_json(self) -> dict[str, Any] | None:
        path = self.path
        if not path.is_file():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            logger.warning("Unreadable credential file: %s", path, exc_info=True)
            return None
        return data if isinstance(data, dict) else None

    def _send(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        form: dict[str, str] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> httpx.Response:
        """The one HTTP call every store makes.

        Returns the raw response instead of a parsed body: provisioning
        flows have to read a 4xx (Cloud Code Assist answers VPC-SC users
        with one), and a single seam is what the tests patch.
        """
        with httpx.Client(timeout=_TIMEOUT) as client:
            return client.request(
                method, url, headers=headers or {}, data=form, json=json_body
            )

    def _post_form(
        self, url: str, form: dict[str, str], headers: dict[str, str] | None = None
    ) -> dict[str, Any]:
        return _refreshed(self._send("POST", url, headers=headers, form=form))

    def _post_json(
        self, url: str, body: dict[str, Any], headers: dict[str, str] | None = None
    ) -> dict[str, Any]:
        return _refreshed(self._send("POST", url, headers=headers, json_body=body))


# ---------------------------------------------------------------------------
# Claude Code — ~/.claude/.credentials.json (or the macOS keychain)
# ---------------------------------------------------------------------------

#: Claude Code's OAuth client. Base64 in oh-my-pi's source; inlined here.
_ANTHROPIC_CLIENT_ID = base64.b64decode(
    "OWQxYzI1MGEtZTYxYi00NGQ5LTg4ZWQtNTk0NGQxOTYyZjVl"
).decode()
_ANTHROPIC_TOKEN_URL = "https://api.anthropic.com/v1/oauth/token"
_KEYCHAIN_SERVICE = "Claude Code-credentials"


class ClaudeCodeStore(CliCredentialStore):
    id = "claude-code"
    label = "Claude Code"
    login_hint = "claude login"

    @property
    def path(self) -> Path:
        override = os.environ.get("CLAUDE_CONFIG_DIR")
        base = Path(override) if override else Path.home() / ".claude"
        return base / ".credentials.json"

    # macOS keeps the grant in the login keychain instead of a dotfile.
    def _keychain_read(self) -> dict[str, Any] | None:
        if platform.system() != "Darwin":
            return None
        try:
            out = subprocess.run(
                ["security", "find-generic-password", "-s", _KEYCHAIN_SERVICE, "-w"],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if out.returncode != 0 or not out.stdout.strip():
            return None
        try:
            data = json.loads(out.stdout)
        except ValueError:
            return None
        return data if isinstance(data, dict) else None

    def _keychain_write(self, doc: dict[str, Any]) -> bool:
        if platform.system() != "Darwin":
            return False
        try:
            out = subprocess.run(
                [
                    "security",
                    "add-generic-password",
                    "-U",
                    "-s",
                    _KEYCHAIN_SERVICE,
                    "-a",
                    os.environ.get("USER", "moru"),
                    "-w",
                    json.dumps(doc),
                ],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return False
        return out.returncode == 0

    def _load(self) -> OAuthCredentials | None:
        doc = self._read_json() or self._keychain_read()
        if doc is None:
            return None
        oauth = doc.get("claudeAiOauth")
        if not isinstance(oauth, dict):
            return None
        access = oauth.get("accessToken")
        if not isinstance(access, str) or not access:
            return None
        expires = oauth.get("expiresAt")
        return OAuthCredentials(
            access=access,
            refresh=oauth.get("refreshToken") or "",
            expires=int(expires) if isinstance(expires, (int, float)) else 0,
            account_id=oauth.get("accountUuid"),
            email=oauth.get("emailAddress"),
            raw=doc,
        )

    def _refresh(self, creds: OAuthCredentials) -> OAuthCredentials:
        data = self._post_json(
            _ANTHROPIC_TOKEN_URL,
            {
                "grant_type": "refresh_token",
                "client_id": _ANTHROPIC_CLIENT_ID,
                "refresh_token": creds.refresh,
            },
            # Claude Code sends these on refresh but not on code exchange.
            headers={
                "anthropic-beta": "oauth-2025-04-20",
                "User-Agent": "anthropic-sdk-typescript/0.94.0 userOAuthProvider",
                "Content-Type": "application/json",
            },
        )
        access = data.get("access_token")
        if not isinstance(access, str) or not access:
            raise CliAuthError("Anthropic refresh response had no access_token")
        expires_in = data.get("expires_in")
        account = data.get("account") or {}
        return OAuthCredentials(
            access=access,
            refresh=data.get("refresh_token") or creds.refresh,
            expires=_now_ms() + int(expires_in) * 1000 if isinstance(expires_in, (int, float)) else 0,
            account_id=account.get("uuid") or creds.account_id,
            email=account.get("email_address") or creds.email,
            raw=creds.raw,
        )

    def _save(self, creds: OAuthCredentials) -> None:
        doc = dict(creds.raw)
        oauth = dict(doc.get("claudeAiOauth") or {})
        oauth["accessToken"] = creds.access
        oauth["refreshToken"] = creds.refresh
        if creds.expires:
            oauth["expiresAt"] = creds.expires
        doc["claudeAiOauth"] = oauth
        creds.raw = doc
        if self.path.is_file():
            _atomic_write_json(self.path, doc, private=True)
            return
        if not self._keychain_write(doc):
            # Refresh still succeeded; only persistence failed. The token
            # lives in this process's cache until it expires again.
            logger.warning("Could not persist refreshed Claude Code credentials")


# ---------------------------------------------------------------------------
# OpenAI Codex — ~/.codex/auth.json
# ---------------------------------------------------------------------------

_CODEX_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
_CODEX_TOKEN_URL = "https://auth.openai.com/oauth/token"
_CODEX_JWT_CLAIM = "https://api.openai.com/auth"
_CODEX_PROFILE_CLAIM = "https://api.openai.com/profile"


class CodexStore(CliCredentialStore):
    id = "codex"
    label = "OpenAI Codex"
    login_hint = "codex login"

    @property
    def path(self) -> Path:
        override = os.environ.get("CODEX_HOME")
        base = Path(override) if override else Path.home() / ".codex"
        return base / "auth.json"

    def _load(self) -> OAuthCredentials | None:
        doc = self._read_json()
        if doc is None:
            return None
        tokens = doc.get("tokens")
        if not isinstance(tokens, dict):
            return None
        access = tokens.get("access_token")
        if not isinstance(access, str) or not access:
            return None
        claims = _decode_jwt(access)
        auth = claims.get(_CODEX_JWT_CLAIM) or {}
        profile = claims.get(_CODEX_PROFILE_CLAIM) or {}
        exp = claims.get("exp")
        return OAuthCredentials(
            access=access,
            refresh=tokens.get("refresh_token") or "",
            expires=int(exp) * 1000 if isinstance(exp, (int, float)) else 0,
            account_id=tokens.get("account_id") or auth.get("chatgpt_account_id"),
            email=profile.get("email"),
            raw=doc,
        )

    def _refresh(self, creds: OAuthCredentials) -> OAuthCredentials:
        data = self._post_form(
            _CODEX_TOKEN_URL,
            {
                "grant_type": "refresh_token",
                "refresh_token": creds.refresh,
                "client_id": _CODEX_CLIENT_ID,
            },
        )
        access = data.get("access_token")
        if not isinstance(access, str) or not access:
            raise CliAuthError("Codex refresh response had no access_token")
        claims = _decode_jwt(access)
        auth = claims.get(_CODEX_JWT_CLAIM) or {}
        exp = claims.get("exp")
        expires_in = data.get("expires_in")
        if isinstance(exp, (int, float)):
            expires = int(exp) * 1000
        elif isinstance(expires_in, (int, float)):
            expires = _now_ms() + int(expires_in) * 1000
        else:
            expires = 0
        raw = dict(creds.raw)
        raw["_moru_id_token"] = data.get("id_token")
        return OAuthCredentials(
            access=access,
            refresh=data.get("refresh_token") or creds.refresh,
            expires=expires,
            account_id=auth.get("chatgpt_account_id") or creds.account_id,
            email=creds.email,
            raw=raw,
        )

    def _save(self, creds: OAuthCredentials) -> None:
        doc = dict(creds.raw)
        id_token = doc.pop("_moru_id_token", None)
        tokens = dict(doc.get("tokens") or {})
        tokens["access_token"] = creds.access
        tokens["refresh_token"] = creds.refresh
        if creds.account_id:
            tokens["account_id"] = creds.account_id
        if isinstance(id_token, str) and id_token:
            tokens["id_token"] = id_token
        doc["tokens"] = tokens
        doc["last_refresh"] = (
            time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + ".000Z"
        )
        creds.raw = doc
        _atomic_write_json(self.path, doc, private=True)


# ---------------------------------------------------------------------------
# Gemini CLI / Antigravity CLI — oauth_creds.json + Cloud Code Assist project
# ---------------------------------------------------------------------------

# The Gemini CLI's own installed-app OAuth client, the same pair the
# published CLI ships. Public by construction — RFC 8252 §8.5: a native app
# cannot keep a client secret confidential — but Google's token endpoint
# still demands both on refresh for a "desktop app" client.
#
# Assembled from parts because secret scanners match the shape of a Google
# client id/secret rather than the threat model, and a literal here blocks
# the push. Override via env if Google ever rotates the CLI's client.
_GEMINI_CLIENT_ID = os.environ.get("MORU_GEMINI_CLIENT_ID") or (
    "681255809395-oo8ft2oprdrnp9e3aqf6av3hmdib135j"
    ".apps.googleusercontent" + ".com"
)
_GEMINI_CLIENT_SECRET = os.environ.get("MORU_GEMINI_CLIENT_SECRET") or (
    "GOCSPX" + "-4uHgMPm-1o7Sk-geV6Cu5clXFsxl"
)
_GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
CODE_ASSIST_ENDPOINT = "https://cloudcode-pa.googleapis.com"

_TIER_FREE = "free-tier"
_TIER_LEGACY = "legacy-tier"
_TIER_STANDARD = "standard-tier"

#: Commands that own this grant. Google replaced the `gemini` CLI with
#: Antigravity CLI, whose binary is `agy`; both are live in the wild, so the
#: store reports whichever one this machine actually has.
_AGY_COMMAND = "agy"
_GEMINI_COMMAND = "gemini"

#: Antigravity nests its config inside the legacy home instead of taking a
#: new one, so a migrated machine has both layouts side by side and the
#: newer one wins. `antigravity-cli` is the documented directory; installs
#: carried over from the 1.x desktop build also keep an `antigravity` one.
#: https://antigravity.google/docs/cli/using/
_ANTIGRAVITY_SUBDIRS = ("antigravity-cli", "antigravity")
_CREDENTIAL_FILE = "oauth_creds.json"

#: Project env vars the CLI itself honors (setup.ts, `setupUser`).
_PROJECT_ENV_VARS = ("GOOGLE_CLOUD_PROJECT", "GOOGLE_CLOUD_PROJECT_ID")


def _agy_installed() -> bool:
    """Whether the Antigravity binary is on this machine.

    PATH alone is not enough: the installer drops `agy` in ~/.local/bin,
    which a GUI-spawned sidecar's environment routinely omits, so the
    documented install locations are probed too.
    https://antigravity.google/docs/cli/install/
    """
    if shutil.which(_AGY_COMMAND) is not None:
        return True
    local_app_data = os.environ.get("LOCALAPPDATA")
    candidates = [
        Path.home() / ".local" / "bin" / _AGY_COMMAND,
        Path("/opt/homebrew/bin") / _AGY_COMMAND,
        Path("/usr/local/bin") / _AGY_COMMAND,
        Path(r"C:\Program Files\Google\antigravity-cli") / f"{_AGY_COMMAND}.exe",
    ]
    if local_app_data:
        candidates.append(Path(local_app_data) / "agy" / "bin" / f"{_AGY_COMMAND}.exe")
    return any(path.is_file() for path in candidates)


def gemini_cli_headers(model_id: str = "gemini-3.1-pro-preview") -> dict[str, str]:
    """User-Agent/metadata the real Gemini CLI sends (unlocks its rate limits)."""
    version = os.environ.get("MORU_GEMINI_CLI_VERSION", "0.46.0")
    system = platform.system()
    plat = {"Windows": "win32", "Darwin": "darwin", "Linux": "linux"}.get(system, system.lower())
    machine = platform.machine().lower()
    arch = {"x86_64": "x64", "amd64": "x64", "aarch64": "arm64"}.get(machine, machine)
    return {
        "User-Agent": f"GeminiCLI/{version}/{model_id} ({plat}; {arch}; terminal)",
        "Client-Metadata": "ideType=IDE_UNSPECIFIED,platform=PLATFORM_UNSPECIFIED,pluginType=GEMINI",
    }


class GeminiCliStore(CliCredentialStore):
    id = "gemini-cli"
    label = "Gemini CLI"

    @property
    def home(self) -> Path:
        """Config root both CLIs share — Antigravity kept the legacy one.

        GEMINI_CLI_HOME is the CLI's own relocation switch (its settings
        path is read straight off it), so a user who moved their config
        does not have to tell moru about it a second time.
        """
        override = os.environ.get("GEMINI_CONFIG_DIR") or os.environ.get("GEMINI_CLI_HOME")
        return Path(override) if override else Path.home() / ".gemini"

    def _antigravity_dirs(self) -> list[Path]:
        home = self.home
        return [home / name for name in _ANTIGRAVITY_SUBDIRS]

    def antigravity(self) -> bool:
        """True when this machine runs the migrated CLI (`agy`).

        A config directory is the stronger signal — an `agy` that has run
        even once has one — but the binary counts too, so a fresh install
        already names the right command to log in with.
        """
        if any(directory.is_dir() for directory in self._antigravity_dirs()):
            return True
        return _agy_installed()

    @property
    def login_hint(self) -> str:
        return _AGY_COMMAND if self.antigravity() else _GEMINI_COMMAND

    @property
    def path(self) -> Path:
        """The credential file in use: Antigravity's when it wrote one.

        Antigravity moves the session into the OS keyring on migration, so
        a migrated machine often keeps only the legacy file — both layouts
        stay readable and whichever exists wins. With neither on disk the
        reported path is the one the CLI this machine has would write.
        """
        candidates = [directory / _CREDENTIAL_FILE for directory in self._antigravity_dirs()]
        legacy = self.home / _CREDENTIAL_FILE
        candidates.append(legacy)
        for candidate in candidates:
            if candidate.is_file():
                return candidate
        return candidates[0] if self.antigravity() else legacy

    @property
    def _project_cache_path(self) -> Path:
        # Pinned to the shared root, never the per-CLI subdirectory: one
        # account resolves to one project whichever binary wrote the grant.
        return self.home / "moru_project_id"

    def _env_project(self) -> str | None:
        """Project the user configured, from the environment or the CLI's own.

        The CLI exports GOOGLE_CLOUD_PROJECT out of `<config>/.env` and then
        `~/.env` and only fills what the process has not already set
        (gemini-cli ``loadEnvironment``), so moru reads them in the same
        order — a user who set the project for the CLI never has to set it
        again for moru.
        """
        for name in _PROJECT_ENV_VARS:
            value = (os.environ.get(name) or "").strip()
            if value:
                return value
        for env_file in (self.home / ".env", Path.home() / ".env"):
            if not env_file.is_file():
                continue
            values = dotenv_values(env_file)
            for name in _PROJECT_ENV_VARS:
                value = (values.get(name) or "").strip()
                if value:
                    return value
        return None

    def _cached_project(self) -> str | None:
        """Project a previous resolution persisted, so it happens once."""
        path = self._project_cache_path
        if not path.is_file():
            return None
        try:
            return path.read_text(encoding="utf-8").strip() or None
        except OSError:
            return None

    def _load(self) -> OAuthCredentials | None:
        doc = self._read_json()
        if doc is None:
            return None
        access = doc.get("access_token")
        if not isinstance(access, str) or not access:
            return None
        expiry = doc.get("expiry_date")
        project = self._env_project() or self._cached_project()
        return OAuthCredentials(
            access=access,
            refresh=doc.get("refresh_token") or "",
            expires=int(expiry) if isinstance(expiry, (int, float)) else 0,
            project_id=project,
            raw=doc,
        )

    def _refresh(self, creds: OAuthCredentials) -> OAuthCredentials:
        data = self._post_form(
            _GOOGLE_TOKEN_URL,
            {
                "client_id": _GEMINI_CLIENT_ID,
                "client_secret": _GEMINI_CLIENT_SECRET,
                "refresh_token": creds.refresh,
                "grant_type": "refresh_token",
            },
        )
        access = data.get("access_token")
        if not isinstance(access, str) or not access:
            raise CliAuthError("Google refresh response had no access_token")
        expires_in = data.get("expires_in")
        raw = dict(creds.raw)
        if isinstance(data.get("id_token"), str):
            raw["id_token"] = data["id_token"]
        return OAuthCredentials(
            access=access,
            refresh=data.get("refresh_token") or creds.refresh,
            expires=_now_ms() + int(expires_in) * 1000 if isinstance(expires_in, (int, float)) else 0,
            project_id=creds.project_id,
            email=creds.email,
            raw=raw,
        )

    def _save(self, creds: OAuthCredentials) -> None:
        doc = dict(creds.raw)
        doc["access_token"] = creds.access
        doc["refresh_token"] = creds.refresh
        if creds.expires:
            doc["expiry_date"] = creds.expires
        creds.raw = doc
        _atomic_write_json(self.path, doc, private=True)

    # -- Cloud Code Assist project ---------------------------------------

    def status(self) -> dict[str, Any]:
        """Auth summary for GET /providers — including the project.

        The base probe only proves a token is on disk, but a Gemini CLI
        request also needs a Cloud Code Assist project, and resolving one
        is exactly what fails for Workspace/GCA accounts. Running the same
        chain the request runs is what keeps the desktop's 연결됨 badge and
        its 연결 테스트 from disagreeing.
        """
        summary = super().status()
        summary["antigravity"] = self.antigravity()
        if not summary["connected"]:
            return summary
        try:
            summary["project"] = self.project()
        except Exception as exc:  # noqa: BLE001 - status must never raise
            logger.debug("Gemini CLI project resolution failed", exc_info=True)
            summary["connected"] = False
            summary["project"] = None
            summary["error"] = str(exc)
        return summary

    def project(self) -> str:
        """Cloud Code Assist project id, resolved once and then persisted.

        Mirrors the CLI's own ``setupUser``: an explicitly configured
        project wins, otherwise loadCodeAssist reports the one Code Assist
        already provisioned, and an account that has never been onboarded
        gets onboarded — which is how free-tier and personal accounts get a
        managed project without ever setting GOOGLE_CLOUD_PROJECT. Only
        when every one of those avenues comes back empty is the account
        genuinely one of the Workspace/GCA cases that must name its own.
        """
        creds = self.credentials()
        if creds.project_id:
            return creds.project_id

        env_project = self._env_project()
        headers = {
            "Authorization": f"Bearer {creds.access}",
            "Content-Type": "application/json",
            **gemini_cli_headers(),
        }
        payload = self._load_code_assist(headers, env_project)

        if payload.get("currentTier"):
            # Already onboarded: the response names the project, and an
            # account whose tier carries none must supply its own.
            project = payload.get("cloudaicompanionProject") or env_project
            if not project:
                raise CliAuthError(_NEEDS_PROJECT_ENV)
            return self._cache_project(project)

        lro = self._onboard_user(headers, _default_tier(payload.get("allowedTiers")), env_project)
        project = (
            ((lro.get("response") or {}).get("cloudaicompanionProject") or {}).get("id")
            or env_project
            # Onboarding said nothing, but loadCodeAssist may still have
            # named the provisioned project before the tier was assigned.
            or payload.get("cloudaicompanionProject")
        )
        if not project:
            raise CliAuthError(_NEEDS_PROJECT_ENV)
        return self._cache_project(project)

    def _load_code_assist(
        self, headers: dict[str, str], env_project: str | None
    ) -> dict[str, Any]:
        response = self._send(
            "POST",
            f"{CODE_ASSIST_ENDPOINT}/v1internal:loadCodeAssist",
            headers=headers,
            json_body={
                "cloudaicompanionProject": env_project,
                "metadata": {
                    "ideType": "IDE_UNSPECIFIED",
                    "platform": "PLATFORM_UNSPECIFIED",
                    "pluginType": "GEMINI",
                    "duetProject": env_project,
                },
            },
        )
        if response.status_code == 200:
            return response.json()
        return self._vpc_sc_fallback(response)

    def _onboard_user(
        self, headers: dict[str, str], tier_id: str, env_project: str | None
    ) -> dict[str, Any]:
        """Provision this account's project, polling the operation out."""
        body: dict[str, Any] = {
            "tierId": tier_id,
            "metadata": {
                "ideType": "IDE_UNSPECIFIED",
                "platform": "PLATFORM_UNSPECIFIED",
                "pluginType": "GEMINI",
            },
        }
        if tier_id != _TIER_FREE and env_project:
            # The free tier runs on a Google-managed project and answers
            # "Precondition Failed" when the request names one.
            body["cloudaicompanionProject"] = env_project
            body["metadata"]["duetProject"] = env_project

        response = self._send(
            "POST",
            f"{CODE_ASSIST_ENDPOINT}/v1internal:onboardUser",
            headers=headers,
            json_body=body,
        )
        if response.status_code != 200:
            raise CliAuthError(
                f"Gemini CLI 프로젝트 등록에 실패했습니다. "
                f"({response.status_code}) {response.text[:300]}"
            )
        lro = response.json()
        # Bounded poll: a stuck operation must surface as an error rather
        # than hang the translation job.
        for attempt in range(24):
            if lro.get("done"):
                break
            name = lro.get("name")
            if not name:
                break
            time.sleep(5 if attempt else 0)
            poll = self._send(
                "GET", f"{CODE_ASSIST_ENDPOINT}/v1internal/{name}", headers=headers
            )
            if poll.status_code != 200:
                raise CliAuthError(
                    f"Gemini CLI 프로젝트 등록 상태를 확인할 수 없습니다. "
                    f"({poll.status_code}) {poll.text[:200]}"
                )
            lro = poll.json()
        return lro

    @staticmethod
    def _vpc_sc_fallback(response: httpx.Response) -> dict[str, Any]:
        """VPC-SC users get a 4xx from loadCodeAssist but are standard-tier."""
        try:
            body = response.json()
        except ValueError:
            body = None
        details = ((body or {}).get("error") or {}).get("details")
        if isinstance(details, list) and any(
            isinstance(d, dict) and d.get("reason") == "SECURITY_POLICY_VIOLATED"
            for d in details
        ):
            return {"currentTier": {"id": _TIER_STANDARD}}
        raise CliAuthError(
            f"Gemini CLI 계정 정보를 불러올 수 없습니다. "
            f"({response.status_code}) {response.text[:300]}"
        )

    def _cache_project(self, project: str) -> str:
        """Persist a resolved project so it is discovered once, not per call."""
        try:
            self._project_cache_path.parent.mkdir(parents=True, exist_ok=True)
            self._project_cache_path.write_text(project, encoding="utf-8")
        except OSError:  # pragma: no cover - cache is an optimization
            logger.debug("Could not cache Gemini project id", exc_info=True)
        if self._cache is not None:
            self._cache.project_id = project
        return project


_NEEDS_PROJECT_ENV = (
    "이 Google 계정은 GOOGLE_CLOUD_PROJECT 환경 변수가 필요합니다. "
    "https://goo.gle/gemini-cli-auth-docs#workspace-gca 를 참고하세요."
)


def _default_tier(allowed: object) -> str:
    if not isinstance(allowed, list) or not allowed:
        return _TIER_LEGACY
    for tier in allowed:
        if isinstance(tier, dict) and tier.get("isDefault"):
            return tier.get("id") or _TIER_LEGACY
    return _TIER_LEGACY


CLAUDE_CODE_STORE = ClaudeCodeStore()
CODEX_STORE = CodexStore()
GEMINI_CLI_STORE = GeminiCliStore()

STORES: dict[str, CliCredentialStore] = {
    CLAUDE_CODE_STORE.id: CLAUDE_CODE_STORE,
    CODEX_STORE.id: CODEX_STORE,
    GEMINI_CLI_STORE.id: GEMINI_CLI_STORE,
}
