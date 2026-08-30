"""Antigravity CLI (`agy`) support: model listing and process invocation.

Antigravity is the successor to the `gemini` CLI. It keeps its OAuth session
in the OS keyring rather than a JSON file, which is why this module talks to
the binary instead of reading credentials: the CLI is the only authority on
its own session, and reimplementing Keychain / libsecret / Credential Manager
access would break the first time Google changed storage.

Everything asserted here was verified against the real binary (v1.1.22,
sha512 matched against Google's own release manifest) or its official docs;
nothing is inferred from the model names' shape.
https://antigravity.google/docs/cli/headless
"""

from __future__ import annotations

import logging
import re
import subprocess
from pathlib import Path

from .credentials import agy_path

logger = logging.getLogger(__name__)

#: Offline fallback lineup. Deliberately a small, current subset rather than
#: an exhaustive dump: every slug here is a literal copied from the official
#: `agy models` sample output, so none of it is guessed.
#:
#: Note the shape — Antigravity slugs carry a reasoning-effort suffix, which
#: is why the legacy ids (`gemini-3.5-flash`, `gemini-3.1-pro-preview`) are
#: NOT valid here. Those remain correct for the legacy HTTP transport, whose
#: model namespace is the Gemini API's, not this one.
#:
#: Claude Opus and GPT-OSS are offered by Antigravity per /docs/models but
#: their slugs never appear literally in any sample, so they are omitted
#: rather than extrapolated from `claude-sonnet-4-6`. A live list supplies
#: them; this fallback stays honest.
#: https://antigravity.google/docs/cli/headless
AGY_FALLBACK_MODELS: tuple[str, ...] = (
    "gemini-3.7-flash-high",
    "gemini-3.7-flash-medium",
    "gemini-3.6-flash-high",
    "gemini-3.6-flash-medium",
    "gemini-3.5-flash-medium",
    "gemini-3.1-pro-high",
    "claude-sonnet-4-6",
)

#: Default for translation: newest Flash at medium effort. Flash because a
#: modpack is thousands of short requests, and 3.7 because /docs/models
#: marks it available on every plan tier including Free — a default that
#: only paid plans can call breaks first-run for everyone else.
AGY_DEFAULT_MODEL = "gemini-3.7-flash-medium"

#: A slug as `agy models` prints it in the first column.
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9.\-]*$")

#: Progress chatter to ignore. `agy models` writes this before the list.
_NOISE_PREFIXES = ("fetching available models", "fetching models")

_MODELS_TIMEOUT = 30.0


class AgyError(RuntimeError):
    """`agy` could not answer. Message is aimed at the desktop UI."""


def parse_models_output(text: str) -> list[str]:
    """Slugs out of `agy models`' decorated console output.

    THE one place this format is parsed. `agy models` has no JSON mode —
    verified: the subcommand accepts only `-h/--help`, and passing
    `--output-format json` fails with "flags provided but not defined". So
    a text parse is forced on us, and isolating it here means the blast
    radius of Google restyling that output is a single function.

    Deliberately conservative: a line contributes a slug only when its
    first token really looks like one. Anything else — banners, blank
    lines, a trailing "..." continuation marker, error prose — is dropped
    rather than guessed at, so a restyle degrades to the fallback list
    instead of inventing a model id that fails at request time.

    Real observed shape (official sample):
        gemini-3.7-flash-medium   Gemini 3.7 Flash (Medium)
        claude-sonnet-4-6         Claude Sonnet 4.6 (Thinking)
    """
    slugs: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("-"):
            continue
        lowered = line.lower()
        if lowered.startswith(_NOISE_PREFIXES) or lowered.startswith("error:"):
            continue
        token = line.split()[0]
        if not _SLUG_RE.match(token):
            continue
        # A bare "..." or a display-name-only row has no usable slug.
        if token not in slugs:
            slugs.append(token)
    return slugs


def _reports_error(text: str) -> str | None:
    """The error `agy` printed, if any.

    Necessary because exit status is unusable: verified on the real binary,
    `agy models` while signed out prints
    "Error: Please sign in to view available models." and STILL exits 0. So
    rc==0 is not success, and an error line has to be treated as first
    class or a signed-out user silently gets an empty lineup.
    """
    for raw in text.splitlines():
        line = raw.strip()
        if line.lower().startswith("error:"):
            return line[len("error:") :].strip() or line
    return None


def run_agy(
    args: list[str],
    *,
    timeout: float,
    cwd: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    """Invoke `agy` non-interactively, never letting it block on a prompt.

    stdin is closed rather than inherited. This is not defensive padding:
    an unauthenticated `agy -p` was observed to print an OAuth URL, wait
    60s, and prompt "paste the authorization code here" on stdin, despite
    the docs claiming it exits instead of hanging. Inside a 15-way
    concurrent translation run, fifteen processes each holding a terminal
    prompt open is the worst failure mode available, so the prompt is
    denied any input and the wait is bounded twice — by `--print-timeout`
    where the caller sets it, and by this hard kill.
    """
    binary = agy_path()
    if binary is None:
        raise AgyError(
            "Antigravity CLI(agy)를 찾을 수 없습니다. "
            "https://antigravity.google/docs/cli/install 를 참고해 설치해 주세요."
        )
    try:
        return subprocess.run(  # noqa: S603 - fixed binary, no shell
            [str(binary), *args],
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=timeout,
            cwd=str(cwd) if cwd else None,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise AgyError(
            f"Antigravity CLI(agy) 응답이 {timeout:.0f}초 안에 오지 않았습니다."
        ) from exc
    except OSError as exc:
        raise AgyError(f"Antigravity CLI(agy) 실행에 실패했습니다: {exc}") from exc


def list_models(*, timeout: float = _MODELS_TIMEOUT) -> list[str]:
    """Models this account may call, falling back to the documented list.

    Never returns empty. An empty lineup reads to a user as "my
    subscription has no models", which is a worse lie than a slightly
    stale list — and the fallback slugs are real, so picking one still
    produces a working request for any plan that carries it.
    """
    try:
        proc = run_agy(["models"], timeout=timeout)
    except AgyError as exc:
        logger.debug("agy models unavailable: %s", exc)
        return list(AGY_FALLBACK_MODELS)

    combined = f"{proc.stdout}\n{proc.stderr}"
    reported = _reports_error(combined)
    if reported:
        # Signed out, or the backend refused. Not a parse failure, so log
        # it distinctly -- this is the case the exit code lies about.
        logger.info("agy models refused: %s", reported)
        return list(AGY_FALLBACK_MODELS)

    slugs = parse_models_output(proc.stdout)
    if not slugs:
        logger.warning(
            "agy models returned nothing parseable; falling back to the "
            "documented lineup. Output format may have changed."
        )
        return list(AGY_FALLBACK_MODELS)
    return slugs
