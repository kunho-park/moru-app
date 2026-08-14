/**
 * allFailedRefs contract: a bulk retry must reach failures on every page.
 *
 * The review table holds one PAGE_SIZE slice, so driving the retry off the
 * rows in view left later pages untouched while the footer still counted
 * them as failed.
 */
import { expect, test } from "bun:test";

// api.ts transitively imports lib/bridge (window.moru, window.location) and
// lib/web (window.localStorage) at module scope. Other test files in the same
// bun process may have installed window first, so merge globals rather than
// replacing them.
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

const { api } = await import("./api.ts");
const { useEngineStore } = await import("../stores/engine.ts");
useEngineStore.setState({ info: { state: "ready", port: 59998, token: "tk" } });
const calls = [];

/** Serves `total` failed entries in pages of the size the caller asks for. */
function serveFailed(total) {
  globalThis.fetch = async (url) => {
    const parsed = new URL(String(url));
    calls.push(parsed.pathname + parsed.search);
    const page = Number(parsed.searchParams.get("page"));
    const size = Number(parsed.searchParams.get("page_size"));
    const start = (page - 1) * size;
    const entries = [];
    for (let i = start; i < Math.min(start + size, total); i += 1) {
      entries.push({ key: `k${i}`, file: `f${i % 2}.json`, status: "failed" });
    }
    return { ok: true, json: async () => ({ total, page, entries }) };
  };
}

test("walks every page so failures past the first slice are included", async () => {
  calls.length = 0;
  serveFailed(1200);

  const refs = await api.allFailedRefs("job-1");

  expect(refs.length).toBe(1200);
  expect(refs[0]).toEqual({ key: "k0", file: "f0.json" });
  expect(refs[1199]).toEqual({ key: "k1199", file: "f1.json" });
  // 1200 entries at 500 per page: three requests, not one.
  expect(calls.length).toBe(3);
  expect(calls[0]).toContain("filter=failed");
  expect(calls[0]).toContain("page_size=500");
});

test("keeps the file so a key living in two source files stays distinct", async () => {
  calls.length = 0;
  serveFailed(4);

  const refs = await api.allFailedRefs("job-1");

  expect(refs.map((r) => r.file)).toEqual([
    "f0.json",
    "f1.json",
    "f0.json",
    "f1.json",
  ]);
});

test("a single short page costs one request", async () => {
  calls.length = 0;
  serveFailed(7);

  expect((await api.allFailedRefs("job-1")).length).toBe(7);
  expect(calls.length).toBe(1);
});

test("no failures means no work and no second request", async () => {
  calls.length = 0;
  serveFailed(0);

  expect(await api.allFailedRefs("job-1")).toEqual([]);
  expect(calls.length).toBe(1);
});

test("stops instead of looping when total overstates the served entries", async () => {
  calls.length = 0;
  // A run that finished between the count and the walk: total says 900 but
  // the pages dry up. The walk must end on the empty page, not spin.
  let served = 0;
  globalThis.fetch = async (url) => {
    calls.push(String(url));
    const entries = served === 0 ? [{ key: "k0", file: "f.json" }] : [];
    served += 1;
    return { ok: true, json: async () => ({ total: 900, page: served, entries }) };
  };

  expect((await api.allFailedRefs("job-1")).length).toBe(1);
  expect(calls.length).toBe(2);
});
