/**
 * The undeliverable-translations band must stay invisible unless real text
 * was lost.
 *
 * The engine only counts a jar-internal guidebook definition once this run
 * actually produced translations for it — a definition holding nothing but
 * lang keys yields no entries and no loss, because that title IS translated
 * through the jar's own language file. So a non-zero count here always
 * means text the user will see in English, and zero must render nothing
 * rather than an empty warning beside the export panel.
 */
import { expect, test } from "bun:test";

// W6Export.tsx pulls in lib/bridge (window.moru) and lib/web
// (window.localStorage) at module scope; merge globals before importing.
const storage = new Map();
const localStorageStub = {
  getItem: (k) => storage.get(k) ?? null,
  setItem: (k, v) => void storage.set(k, String(v)),
  removeItem: (k) => void storage.delete(k),
};
globalThis.localStorage ??= localStorageStub;
const win = (globalThis.window ??= {});
win.localStorage ??= localStorageStub;
win.location ??= { search: "" };
win.moru ??= {};

const bridge = await import("../lib/bridge.ts");
bridge.moru.secrets ??= {
  get: async () => null,
  set: async () => undefined,
  delete: async () => undefined,
};
const { undeliverableSummary } = await import("./W6Export.tsx");

/** Measured on a real 330-jar pack: one mod inlines guidebook prose. */
const REAL_LOSS = {
  undeliverable_jar_files: 1,
  undeliverable_jar_entries: 2,
  undeliverable_jar_mods: ["mimimod-1.20.1-4.3.1.BETA.2-forge.jar"],
};

test("the real one-mod case surfaces its mod and both counts", () => {
  expect(undeliverableSummary(REAL_LOSS)).toEqual({
    files: 1,
    entries: 2,
    mods: ["mimimod-1.20.1-4.3.1.BETA.2-forge.jar"],
  });
});

test("a pack that lost nothing renders no band", () => {
  // The normal case. Four of the five jars carrying a data/ guidebook
  // definition on that same pack hold only lang keys, so nothing is lost
  // and nothing is reported.
  expect(
    undeliverableSummary({
      undeliverable_jar_files: 0,
      undeliverable_jar_entries: 0,
      undeliverable_jar_mods: [],
    }),
  ).toBeNull();
});

test("stats from before the fields existed render no band", () => {
  // A session restored from an older record has no such keys at all.
  expect(undeliverableSummary({ total_entries: 10 })).toBeNull();
  expect(undeliverableSummary(null)).toBeNull();
  expect(undeliverableSummary(undefined)).toBeNull();
});

test("a loss with no attributable jar still shows, with no mod chips", () => {
  // entry_count is what the user feels; an unattributable path must not
  // invent a mod name.
  expect(
    undeliverableSummary({ undeliverable_jar_files: 2, undeliverable_jar_entries: 5 }),
  ).toEqual({ files: 2, entries: 5, mods: [] });
});

test("both locales define every key the band renders", async () => {
  const en = await import("../i18n/en/w6.json");
  const ko = await import("../i18n/ko/w6.json");
  const keys = ["header", "summary", "body", "hint"];
  for (const bundle of [en.default, ko.default]) {
    expect(Object.keys(bundle.undeliverable ?? {}).sort()).toEqual([...keys].sort());
  }
  // The interpolations the component passes must exist in both.
  for (const bundle of [en.default, ko.default]) {
    expect(bundle.undeliverable.summary).toContain("{{files}}");
    expect(bundle.undeliverable.summary).toContain("{{entries}}");
  }
  // Wording must not collide with the hardcoded-text band on W2Scan: that
  // one is "cannot translate", this one is "cannot install".
  const w2en = await import("../i18n/en/w2.json");
  expect(w2en.default.hardcoded.header).not.toBe(en.default.undeliverable.header);
});
