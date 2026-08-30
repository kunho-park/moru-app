/**
 * Coding-CLI subscription providers in the renderer.
 *
 * These authenticate through a coding CLI the user already installed and
 * logged into — there is no API key to paste. Everything the UI says about
 * one comes off the wire: `GET /providers` marks them `auth: "cli"` and
 * carries the login command, the signed-in account, and the reason a grant
 * cannot serve a request.
 *
 * Deliberately absent from this file: provider ids, product names and login
 * commands. The engine's `CLI_PROVIDER_CATALOG` owns all three, so a fourth
 * CLI provider — or `gemini-cli` being renamed to Antigravity while it keeps
 * its id — lands with no change on this side. A login command duplicated
 * here would go stale silently, which is the same class of bug that let a
 * provider report "connected" while a real call failed.
 */
import type { Provider } from "../../../shared/engine";

/** Trimmed value, or null for absent/blank — the wire uses both for "none". */
function nonEmpty(value: string | null | undefined): string | null {
  if (typeof value !== "string") return null;
  const trimmed = value.trim();
  return trimmed.length > 0 ? trimmed : null;
}

/**
 * True when the engine authenticates this provider through a coding CLI.
 *
 * `auth: "cli"` is the engine's own discriminator — it stamps it onto every
 * entry of `CLI_PROVIDER_CATALOG`, the same table whose `env` is None
 * because these never take a key. Branching on the wire rather than a local
 * id list is what keeps the two in step.
 */
export function isCliProvider(provider: Provider): boolean {
  return provider.auth === "cli";
}

/**
 * What the user has to do next, which is the only thing the three states
 * differ by:
 * - `ready`       — a real request would succeed; nothing to do.
 * - `needs-setup` — signed in, but the grant cannot serve a request. Logging
 *                   in again does not fix it (e.g. a Workspace Google
 *                   account with no Cloud Code Assist project).
 * - `needs-login` — no readable credential. Log in with `command`.
 */
export type CliSetupState =
  | { kind: "ready"; account: string | null }
  | { kind: "needs-setup"; reason: string | null }
  | { kind: "needs-login"; command: string | null };

/**
 * The provider's setup state, derived only from what the engine reports.
 *
 * `connected` is the engine's honest answer: for Gemini CLI it resolves a
 * Cloud Code Assist project the same way a real request does, so it cannot
 * come back true for a login that would fail on first use.
 *
 * Note the engine cannot currently tell "CLI never installed" apart from
 * "installed but not logged in" — both arrive as connected:false with no
 * error — so `needs-login` covers both, and its copy has to serve a user in
 * either position.
 */
export function cliSetupState(provider: Provider): CliSetupState {
  if (provider.connected ?? provider.has_key) {
    return { kind: "ready", account: nonEmpty(provider.account) };
  }
  // `error` is set only when a credential was readable and still unusable.
  if (nonEmpty(provider.error) !== null) {
    return { kind: "needs-setup", reason: cliFailureReason(provider.error) };
  }
  return { kind: "needs-login", command: nonEmpty(provider.login_hint) };
}

/** A Python traceback, verbatim from the engine's `str(exc)`. */
const TRACEBACK = /Traceback \(most recent call last\)|^\s+File ".+", line \d+/m;
/** `litellm.APIConnectionError`, `KeyError`, `json.decoder.JSONDecodeError`… */
const EXCEPTION_CLASS = /(?:[A-Za-z_]\w*\.)*[A-Z]\w*(?:Error|Exception)\b/;
/** Longer than this and the reason stops being a sentence the user can act on. */
const MAX_REASON = 200;

/**
 * Engine failure text fit to show a user, or null when it is not.
 *
 * The engine hands back `str(exc)` on any unexpected failure, so the same
 * field carries either hand-written guidance ("this Google account needs
 * GOOGLE_CLOUD_PROJECT…") or a raw traceback. Prose passes through; anything
 * carrying a stack or an exception class name is dropped so the caller can
 * fall back to copy that actually tells the user what to do.
 */
export function cliFailureReason(raw: string | null | undefined): string | null {
  if (typeof raw !== "string") return null;
  if (TRACEBACK.test(raw)) return null;
  const text = raw.replace(/\s+/g, " ").trim();
  if (text.length === 0) return null;
  if (EXCEPTION_CLASS.test(text)) return null;
  return text.length > MAX_REASON ? `${text.slice(0, MAX_REASON - 1)}…` : text;
}

/**
 * The engine's catalog ships display names with a Korean "(구독)" suffix
 * ("Gemini CLI (구독)"), which an English user should never read. Split the
 * marker off and let the caller render a localized one beside the product
 * name.
 *
 * The product name itself still comes from the engine. A table of names
 * keyed by id would read "Gemini CLI" for months after that provider is
 * renamed to Antigravity — its id is staying put — so the one thing this
 * must not do is decide what the product is called.
 */
const SUBSCRIPTION_MARKER = /[(（]\s*구독\s*[)）]\s*$/;

export function cliProductName(provider: Provider): string {
  const stripped = provider.name.replace(SUBSCRIPTION_MARKER, "").trim();
  return stripped.length > 0 ? stripped : provider.name;
}
