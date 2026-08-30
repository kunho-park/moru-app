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
const { parseKeyScope, upsertTerm } = await import("./Glossary.tsx");

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
