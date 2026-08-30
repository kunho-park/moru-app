/**
 * The "somebody already translated this" band.
 *
 * This band exists to stop a user paying to redo work that is already
 * published, so the numbers it shows have to be trustworthy in one specific
 * direction: `uncovered_entries` is a LOWER bound on the entries the
 * published pack does not have, never a coverage percentage. Offering a
 * 40%-covered translation as if it were complete is worse than offering
 * nothing, and the band must stay invisible when the lookup found nothing or
 * failed — an optional network call is not something the scan screen has
 * anything to say about.
 */
import { expect, test } from "bun:test";

// W2Scan.tsx pulls in lib/bridge (window.moru) and lib/web
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
const { communityOffer } = await import("./W2Scan.tsx");

/** A match as the engine serialises TranslationMatch. */
function match(overrides = {}) {
  return {
    pack_id: "pack-1",
    modpack_version: "4.1.2",
    exact: true,
    compatible_versions: null,
    total_entries: 8036,
    uncovered_entries: 40,
    uncovered_by_category: { "": 40 },
    url: "https://moru.gg/ko/pack/pack-1",
    download_url: "https://moru.gg/api/packs/pack-1/download",
    note: "4.1.2 버전용으로 제작된 번역팩입니다.",
    ...overrides,
  };
}

test("a match carries its coverage figures through to the band", () => {
  const offer = communityOffer(match(), 8076);
  expect(offer).not.toBeNull();
  expect(offer.packId).toBe("pack-1");
  expect(offer.exact).toBeTrue();
  expect(offer.uncovered).toBe(40);
  expect(offer.localEntries).toBe(8076);
  expect(offer.packEntries).toBe(8036);
});

test("a failed or empty lookup renders no band at all", () => {
  // react-query's `data` is undefined while the query is in flight and after
  // it fails with retry:false; the engine answers `match: null` for every
  // other miss. None of the three may put anything on screen.
  expect(communityOffer(undefined, 8076)).toBeNull();
  expect(communityOffer(null, 8076)).toBeNull();
});

test("a match with nothing to open or download is not an offer", () => {
  // A notification with no next step is noise on a screen that already has
  // three bands competing for the user's attention.
  expect(
    communityOffer(match({ url: null, download_url: null }), 8076),
  ).toBeNull();
  // Either one alone is still actionable.
  expect(communityOffer(match({ download_url: null }), 8076)).not.toBeNull();
  expect(communityOffer(match({ url: null }), 8076)).not.toBeNull();
});

test("the biggest gap is listed first", () => {
  // "mods +412" is the line that decides whether the offer is worth taking;
  // it must not sit underneath three small ones.
  const offer = communityOffer(
    match({ uncovered_by_category: { quests: 12, mods: 412, scripts: 90 } }),
    41_588,
  );
  expect(offer.gaps).toEqual([
    { category: "mods", entries: 412 },
    { category: "scripts", entries: 90 },
    { category: "quests", entries: 12 },
  ]);
});

test("nothing measured and nothing missing stay distinguishable", () => {
  // null means the local side was never compared; 0 means it was compared
  // and no missing entry could be established. Collapsing them would turn
  // "we do not know" into "it is complete".
  expect(communityOffer(match({ uncovered_entries: null }), 8076).uncovered).toBeNull();
  expect(communityOffer(match({ uncovered_entries: 0 }), 8076).uncovered).toBe(0);
});

test("both locales define every key the band renders", async () => {
  const en = await import("../i18n/en/w2.json");
  const ko = await import("../i18n/ko/w2.json");
  const keys = [
    "header",
    "summaryExact",
    "summaryCompatible",
    "packEntries",
    "uncovered",
    "covered",
    "unmeasured",
    "gapsHeader",
    "gapLine",
    "gapTotal",
    "reuseReady",
    "needsPrevious",
    "sameVersionHint",
    "attached",
    "failed",
    "open",
    "reuse",
    "pickPrevious",
    "sameVersionReuse",
    "working",
  ];
  for (const bundle of [en.default, ko.default]) {
    expect(Object.keys(bundle.community ?? {}).sort()).toEqual([...keys].sort());
  }
  // The interpolations the component passes must exist in both.
  for (const bundle of [en.default, ko.default]) {
    expect(bundle.community.uncovered).toContain("{{uncovered}}");
    expect(bundle.community.uncovered).toContain("{{local}}");
    expect(bundle.community.summaryExact).toContain("{{version}}");
    expect(bundle.community.gapLine).toContain("{{category}}");
    expect(bundle.community.packEntries).toContain("{{entries}}");
  }
});

test("no locale promises a coverage percentage", async () => {
  // The engine reports a lower bound on missing entries precisely because
  // only a key-level diff of the downloaded pack could justify a percentage.
  // A "%" here would be the band inventing a precision nothing measured.
  for (const locale of ["en", "ko"]) {
    const bundle = (await import(`../i18n/${locale}/w2.json`)).default;
    for (const [key, value] of Object.entries(bundle.community)) {
      expect(`${key}:${value}`).not.toContain("%");
    }
  }
});

test("the unverified path names what it gives up", async () => {
  // Reusing without the previous modpack skips the source-text check. It is
  // allowed, but only because the copy says so; if this wording is ever
  // softened into a plain "reuse", the band starts applying stale
  // translations silently, which is the one thing it must never do.
  const en = (await import("../i18n/en/w2.json")).default;
  const ko = (await import("../i18n/ko/w2.json")).default;
  expect(en.community.sameVersionHint).toContain("skips the check");
  expect(ko.community.sameVersionHint).toContain("대조를 건너뜁니다");
  expect(en.community.needsPrevious).toContain("stale");
  expect(ko.community.needsPrevious).toContain("다시 번역하는 편이 낫습니다");
});
