/**
 * Key-scope row identity: one source term carries several readings, so a
 * glossary row is identified by source AND key scope. Matching on source
 * alone collapsed vanilla "Wither" (위더 the boss, 시듦 the status effect)
 * into a single row and lost one of them.
 */
import { expect, test } from "bun:test";

// Glossary.tsx transitively imports lib/api -> lib/bridge (window.moru) and
// lib/web (window.localStorage) at module scope; merge globals before
// importing (other test files in the same bun process may have installed
// them).
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
const {
  buildCsv,
  extractCsvTerms,
  parseCsv,
  parseCsvKeyScope,
  parseCsvOrigin,
  parseKeyScope,
  upsertTerm,
} = await import("./Glossary.tsx");

function term(source, target, key_scope = []) {
  return { source, target, origin: "manual", key_scope };
}

test("scope input is split, trimmed, deduplicated and sorted", () => {
  expect(parseKeyScope("effect.*, entity.minecraft.wither")).toEqual([
    "effect.*",
    "entity.minecraft.wither",
  ]);
  // Whitespace-only and repeated patterns collapse; order is normalised to
  // match the engine's TermRule validator so a saved row stays put.
  expect(parseKeyScope("  entity.*   effect.*  , effect.* ")).toEqual([
    "effect.*",
    "entity.*",
  ]);
  expect(parseKeyScope("")).toEqual([]);
  expect(parseKeyScope("   ")).toEqual([]);
});

test("same source with a different scope is a separate row", () => {
  const existing = [term("Wither", "위더")];
  const next = upsertTerm(existing, term("Wither", "시듦", ["effect.*"]), "start");
  expect(next.map((t) => [t.target, t.key_scope])).toEqual([
    ["시듦", ["effect.*"]],
    ["위더", []],
  ]);
});

test("same source and same scope replaces in place", () => {
  const existing = [term("Ingot", "주괴"), term("Wither", "시듦", ["effect.*"])];
  const next = upsertTerm(existing, term("Wither", "쇠약", ["effect.*"]), "start");
  // Replaced where it stood - not moved to the top, not duplicated.
  expect(next.map((t) => t.target)).toEqual(["주괴", "쇠약"]);
});

test("unscoped rows keep matching on source alone", () => {
  const existing = [term("Wither", "위더")];
  const next = upsertTerm(existing, term("Wither", "위더 보스"), "end");
  expect(next.map((t) => t.target)).toEqual(["위더 보스"]);
});

test("append position puts a new row last", () => {
  const next = upsertTerm([term("Ingot", "주괴")], term("Wither", "위더"), "end");
  expect(next.map((t) => t.source)).toEqual(["Ingot", "Wither"]);
});

// -- CSV round-trip -----------------------------------------------------------

/** Reset keeps only server-owned rows (Glossary.tsx resetGlossary). */
function survivorsOfReset(terms) {
  return terms.filter((t) => t.origin === "vanilla" || t.origin === "community");
}

test("export/import/reset keeps server-owned rows instead of duplicating them", () => {
  // The reproduced chain: export dropped `origin`, import hardcoded "manual",
  // and row identity is source + key scope — so the imported copy REPLACED the
  // vanilla row, Reset deleted it as user-generated, and the next community
  // sync re-appended the real term. One cycle turned a synced baseline into
  // duplicates, and Reset kept 0 of the 2 vanilla rows.
  const before = [
    { source: "Wither", target: "위더", origin: "vanilla", key_scope: [] },
    { source: "Wither", target: "시듦", origin: "vanilla", key_scope: ["effect.*"] },
    { source: "Ingot", target: "주괴", origin: "manual", key_scope: [] },
  ];

  const csv = buildCsv(before);
  expect(csv).toBe(
    [
      "source,target,key_scope,origin",
      "Wither,위더,,vanilla",
      "Wither,시듦,effect.*,vanilla",
      "Ingot,주괴,,manual",
    ].join("\n"),
  );

  let merged = [];
  for (const entry of extractCsvTerms(parseCsv(csv))) {
    merged = upsertTerm(merged, entry, "end");
  }
  // Origin and scope both survive the trip, so nothing was rewritten as a
  // manual copy and no row was duplicated.
  expect(merged).toEqual(before);

  const survivors = survivorsOfReset(merged);
  expect(survivors).toHaveLength(2);
  expect(survivors.map((t) => [t.target, t.origin, t.key_scope])).toEqual([
    ["위더", "vanilla", []],
    ["시듦", "vanilla", ["effect.*"]],
  ]);
});

test("a multi-pattern scope survives the CSV cell in both separators", () => {
  const term = {
    source: "Wither",
    target: "시듦",
    origin: "vanilla",
    key_scope: ["effect.*", "status_effect.*"],
  };
  // Written with ";" — the separator moru-web reads.
  expect(buildCsv([term])).toBe(
    "source,target,key_scope,origin\nWither,시듦,effect.*;status_effect.*,vanilla",
  );
  expect(extractCsvTerms(parseCsv(buildCsv([term])))).toEqual([term]);
  // And a file this app exported BEFORE the fix (", "-joined, hence quoted)
  // still imports as two patterns rather than one dead one.
  const legacy =
    'source,target,key_scope,origin\nWither,시듦,"effect.*, status_effect.*",vanilla';
  expect(extractCsvTerms(parseCsv(legacy))).toEqual([term]);
});

test("the origin column is optional and only accepts real origins", () => {
  expect(parseCsvOrigin("vanilla")).toBe("vanilla");
  expect(parseCsvOrigin(" Community ")).toBe("community");
  // Anything unrecognised imports as manual rather than failing the row.
  expect(parseCsvOrigin("nonsense")).toBe("manual");
  expect(parseCsvOrigin("")).toBe("manual");
  expect(parseCsvOrigin(undefined)).toBe("manual");

  // A pre-origin export (three columns) still imports, all manual.
  const threeCol = "source,target,key_scope\nWither,시듦,effect.*";
  expect(extractCsvTerms(parseCsv(threeCol))).toEqual([
    { source: "Wither", target: "시듦", origin: "manual", key_scope: ["effect.*"] },
  ]);
  // And a headerless two-column file is unchanged: manual and unscoped.
  expect(extractCsvTerms(parseCsv("Ingot,주괴"))).toEqual([
    { source: "Ingot", target: "주괴", origin: "manual", key_scope: [] },
  ]);
});

test("the CSV cell grammar is lenient but the text input is not", () => {
  // The CSV cell is a boundary shared with moru-web, so it takes ";" too.
  expect(parseCsvKeyScope("effect.*;entity.*")).toEqual(["effect.*", "entity.*"]);
  expect(parseCsvKeyScope("effect.*, entity.*")).toEqual(["effect.*", "entity.*"]);
  // The scope text box is not a boundary; ";" is not a separator there, so it
  // stays part of the token and the engine rejects it (PUT /glossary -> 422)
  // rather than storing a scope that matches no key.
  expect(parseKeyScope("effect.*;entity.*")).toEqual(["effect.*;entity.*"]);
});
