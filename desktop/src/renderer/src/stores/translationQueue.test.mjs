import { expect, test } from "bun:test";

const storage = new Map();
const localStorageStub = {
  getItem: (key) => storage.get(key) ?? null,
  setItem: (key, value) => void storage.set(key, String(value)),
  removeItem: (key) => void storage.delete(key),
};
globalThis.localStorage ??= localStorageStub;
const win = (globalThis.window ??= {});
win.localStorage ??= localStorageStub;
win.location ??= { search: "" };
win.setTimeout ??= globalThis.setTimeout;
win.moru ??= {
  platform: "win32",
  versions: { app: "test", electron: "test" },
  setBusy: () => undefined,
  secrets: { get: async () => null },
};

const {
  appendUniqueQueueItems,
  drainTranslationQueue,
  movePendingQueueItem,
  normalizePersistedQueueState,
  retryQueueItem,
} = await import("./translationQueue.ts");

function item(id, status = "pending", path = `C:\\Packs\\${id}`) {
  return {
    id,
    path,
    name: id,
    status,
    sessionId: null,
    error: status === "failed" ? "failed" : null,
    addedAt: 1,
    startedAt: null,
    finishedAt: null,
  };
}

test("deduplicates Windows paths across case, slash, and trailing separators", () => {
  let id = 0;
  const result = appendUniqueQueueItems(
    [],
    [
      { path: "C:\\Packs\\Alpha", name: "Alpha" },
      { path: "c:/packs/alpha/", name: "Duplicate" },
      { path: "C:\\Packs\\Beta", name: "Beta" },
    ],
    "win32",
    () => `id-${++id}`,
    () => 100,
  );
  expect(result.map((entry) => entry.name)).toEqual(["Alpha", "Beta"]);
});

test("moves only among pending items and leaves active or finished rows fixed", () => {
  const original = [item("done", "done"), item("a"), item("active", "translating"), item("b")];
  const moved = movePendingQueueItem(original, "b", -1);
  expect(moved.map((entry) => entry.id)).toEqual(["done", "b", "active", "a"]);
  expect(movePendingQueueItem(moved, "active", -1)).toBe(moved);
});

test("retry resets only failed and cancelled items", () => {
  const failed = { ...item("a", "failed"), sessionId: "old", startedAt: 2, finishedAt: 3 };
  expect(retryQueueItem(failed)).toMatchObject({
    status: "pending",
    sessionId: null,
    error: null,
    startedAt: null,
    finishedAt: null,
  });
  const done = item("done", "done");
  expect(retryQueueItem(done)).toBe(done);
});

test("persisted running queues restart paused without discarding the active item", () => {
  const restored = normalizePersistedQueueState({
    items: [item("active", "translating")],
    phase: "running",
    settingsSnapshot: null,
    lastError: null,
  });
  expect(restored.phase).toBe("paused");
  expect(restored.items[0].status).toBe("translating");
});

test("drains packs strictly one at a time and continues after a failure", async () => {
  let phase = "running";
  let items = [item("a"), item("b"), item("c")];
  const order = [];
  let inFlight = 0;
  let maxInFlight = 0;

  await drainTranslationQueue({
    getPhase: () => phase,
    getNext: () => items.find((entry) => entry.status === "pending"),
    run: async (entry) => {
      order.push(entry.id);
      inFlight += 1;
      maxInFlight = Math.max(maxInFlight, inFlight);
      await Promise.resolve();
      inFlight -= 1;
      if (entry.id === "b") throw new Error("provider failed");
      return { status: "done" };
    },
    settle: (entry, outcome) => {
      items = items.map((candidate) =>
        candidate.id === entry.id
          ? { ...candidate, status: outcome.status, error: outcome.error ?? null }
          : candidate,
      );
    },
    setPhase: (next) => {
      phase = next;
    },
  });

  expect(order).toEqual(["a", "b", "c"]);
  expect(maxInFlight).toBe(1);
  expect(items.map((entry) => entry.status)).toEqual(["done", "failed", "done"]);
  expect(phase).toBe("complete");
});

test("pause request waits for the current item and leaves the next pending", async () => {
  let phase = "running";
  let items = [item("a"), item("b")];

  await drainTranslationQueue({
    getPhase: () => phase,
    getNext: () => items.find((entry) => entry.status === "pending"),
    run: async () => {
      phase = "pausing";
      return { status: "done" };
    },
    settle: (entry, outcome) => {
      items = items.map((candidate) =>
        candidate.id === entry.id ? { ...candidate, status: outcome.status } : candidate,
      );
    },
    setPhase: (next) => {
      phase = next;
    },
  });

  expect(items.map((entry) => entry.status)).toEqual(["done", "pending"]);
  expect(phase).toBe("paused");
});
