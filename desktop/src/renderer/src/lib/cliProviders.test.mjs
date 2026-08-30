/**
 * CLI-subscription providers: the wire contract the setup guidance is built
 * on, and the copy it resolves to.
 *
 * Both the Settings card and the W3 provider band render `cliStatusChip` and
 * `cliGuidance` off `cliSetupState`, so what is asserted here is what both
 * screens say — a provider cannot read "설정 필요" in settings and "연결됨"
 * in the translation step while these agree.
 *
 * The locale resources are the shipped JSON, not fixtures: a state whose
 * string is missing from `en` or `ko` fails here rather than reaching a user
 * as a raw i18n key.
 */
import { expect, test } from "bun:test";
import { createInstance } from "i18next";

import en from "../i18n/en/settings.json";
import ko from "../i18n/ko/settings.json";
import {
  cliFailureReason,
  cliGuidance,
  cliSetupState,
  cliStatusChip,
  isCliProvider,
  providerLabel,
} from "./cliProviders.ts";

const i18n = createInstance();
await i18n.init({
  lng: "en",
  interpolation: { escapeValue: false },
  resources: { en: { translation: { settings: en } }, ko: { translation: { settings: ko } } },
});
const at = (lng) => i18n.getFixedT(lng);

/** A `GET /providers` row for a coding-CLI subscription. */
function wire(overrides) {
  return {
    id: "gemini-cli",
    name: "Gemini CLI / Antigravity",
    models: ["gemini-cli/gemini-3.5-flash"],
    has_key: false,
    auth: "cli",
    connected: false,
    login_hint: "agy",
    cli: "agy",
    cli_installed: true,
    account: null,
    error: null,
    ...overrides,
  };
}

test("the wire's own `auth` marks a CLI provider, not a list of ids", () => {
  expect(isCliProvider(wire({ state: "logged-out" }))).toBe(true);
  // A provider id this build has never heard of still lights up correctly.
  expect(isCliProvider(wire({ id: "some-future-cli", state: "ready" }))).toBe(true);
  expect(isCliProvider({ id: "openai", name: "OpenAI", models: [], has_key: true })).toBe(false);
});

test("each engine state carries what its own next action needs", () => {
  expect(cliSetupState(wire({ state: "ready", connected: true, account: "a@b.c" }))).toEqual({
    kind: "ready",
    account: "a@b.c",
  });
  expect(cliSetupState(wire({ state: "cli-ready" }))).toEqual({ kind: "cli-ready", command: "agy" });
  expect(cliSetupState(wire({ state: "logged-out" }))).toEqual({
    kind: "logged-out",
    command: "agy",
    cli: "agy",
  });
  expect(cliSetupState(wire({ state: "cli-missing", cli_installed: false }))).toEqual({
    kind: "cli-missing",
    cli: "agy",
  });
  expect(cliSetupState(wire({ state: "unusable", error: "needs a project id" }))).toEqual({
    kind: "unusable",
    reason: "needs a project id",
  });
});

test("an engine too old to send `state` still resolves a usable state", () => {
  expect(cliSetupState(wire({ connected: true, account: "a@b.c" })).kind).toBe("ready");
  expect(cliSetupState(wire({ error: "no project" })).kind).toBe("unusable");
  expect(cliSetupState(wire({})).kind).toBe("logged-out");
});

test("cli-missing never names a login command, which there is nothing to run", () => {
  const state = cliSetupState(wire({ state: "cli-missing", cli_installed: false }));
  for (const lng of ["en", "ko"]) {
    expect(cliGuidance(state, at(lng))).not.toContain("agy login");
  }
  // It does name the binary to install.
  expect(cliGuidance(state, at("en"))).toContain("agy");
});

test("cli-ready admits the login is unknowable instead of claiming logged-out", () => {
  const state = cliSetupState(wire({ state: "cli-ready" }));
  // Antigravity keeps the grant in the OS keyring; saying "not signed in"
  // would be a lie to a user who is.
  expect(cliGuidance(state, at("en"))).toContain("keyring");
  expect(cliGuidance(state, at("en"))).not.toMatch(/not signed in/i);
  expect(cliGuidance(state, at("ko"))).toContain("키링");
});

test("a Cloud project reason only ever reaches the screen from the engine", () => {
  // Never hardcoded renderer-side: it is legacy-transport-only, and an
  // Antigravity user must never be told to set one.
  for (const value of Object.values(en)) {
    if (typeof value === "string") expect(value).not.toContain("GOOGLE_CLOUD_PROJECT");
  }
  for (const value of Object.values(ko)) {
    if (typeof value === "string") expect(value).not.toContain("GOOGLE_CLOUD_PROJECT");
  }
});

test("every state resolves to real copy in both shipped locales", () => {
  const states = [
    wire({ state: "ready", connected: true, account: "a@b.c" }),
    wire({ state: "cli-ready" }),
    wire({ state: "logged-out" }),
    wire({ state: "cli-missing", cli_installed: false }),
    wire({ state: "unusable", error: "some readable reason" }),
    // degraded shapes: the engine reported a state but no command/name
    wire({ state: "logged-out", login_hint: null, cli: null }),
    wire({ state: "cli-missing", cli: null }),
    wire({ state: "cli-ready", login_hint: null }),
    wire({ state: "unusable", error: null }),
  ];
  for (const lng of ["en", "ko"]) {
    for (const provider of states) {
      const state = cliSetupState(provider);
      const { label } = cliStatusChip(state, at(lng));
      const guidance = cliGuidance(state, at(lng));
      for (const text of [label, guidance]) {
        expect(text.length).toBeGreaterThan(0);
        // A missing key echoes the key back; interpolation misses leave {{}}.
        expect(text).not.toContain("settings.models.");
        expect(text).not.toContain("{{");
      }
    }
  }
});

test("a raw engine failure never reaches the screen", () => {
  const traceback = [
    "litellm.APIConnectionError: CodexException",
    "Traceback (most recent call last):",
    '  File "/opt/moru/litellm/main.py", line 2841, in acompletion',
    "moru_engine.cli_providers.credentials.CliAuthError: 401 Unauthorized",
  ].join("\n");
  expect(cliFailureReason(traceback)).toBeNull();
  expect(cliFailureReason("KeyError: 'accessToken'")).toBeNull();
  expect(cliFailureReason("  ")).toBeNull();
  expect(cliFailureReason(null)).toBeNull();

  // Prose the engine wrote for a user survives, whitespace normalized.
  expect(cliFailureReason("이 계정은 프로젝트를\n직접 지정해야 합니다.")).toBe(
    "이 계정은 프로젝트를 직접 지정해야 합니다.",
  );

  // Suppressed reason still yields an actionable sentence, not a blank.
  const blocked = cliGuidance({ kind: "unusable", reason: null }, at("ko"));
  expect(blocked.length).toBeGreaterThan(10);
  expect(blocked).not.toContain("Traceback");
});

test("provider names come from the engine unless a locale overrides the id", () => {
  // The `gemini-cli` id outlives its product name, so the label follows the
  // engine rather than a table keyed by id.
  expect(providerLabel(wire({ state: "ready" }), at("en"))).toBe("Gemini CLI / Antigravity");
  expect(providerLabel(wire({ id: "x", name: "Renamed Later" }), at("ko"))).toBe("Renamed Later");
});
