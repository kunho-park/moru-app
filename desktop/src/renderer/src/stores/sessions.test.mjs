import { expect, test } from "bun:test";

const storage = new Map();
const localStorageStub = {
  getItem: (key) => storage.get(key) ?? null,
  setItem: (key, value) => void storage.set(key, String(value)),
  removeItem: (key) => void storage.delete(key),
};
globalThis.localStorage ??= localStorageStub;
// zustand's persist reads window.localStorage: without it the store silently
// loses its storage (and its persist api) for every later test file too.
const win = (globalThis.window ??= {});
win.localStorage ??= localStorageStub;

const { migrateSessionsState, useSessions } = await import("./sessions.ts");

function legacyRecord(status) {
  return {
    id: "legacy",
    modpackPath: "C:/pack",
    modpackName: "Pack",
    sourceLocale: "en_us",
    targetLocale: "ko_kr",
    model: "openai/gpt-test",
    status,
    createdAt: 1_000,
    finishedAt: null,
    doneEntries: 12,
    totalEntries: 20,
  };
}

test("marks legacy running records without a job id as unrecoverable", () => {
  const migrated = migrateSessionsState(
    { sessions: [legacyRecord("running")] },
    5_000,
  );
  const record = migrated.sessions[0];
  expect(record.status).toBe("failed");
  expect(record.finishedAt).toBe(5_000);
  expect(record.translateJobId).toBe(null);
  expect(record.scanTotals).toBe(null);
  expect(record.error).toContain("복원할 수 없습니다");
});

test("keeps a persisted running record recoverable when it has a job id", () => {
  const migrated = migrateSessionsState({
    sessions: [
      {
        ...legacyRecord("running"),
        translateJobId: "job-live",
        scanTotals: { entries: 20 },
      },
    ],
  });
  const record = migrated.sessions[0];
  expect(record.status).toBe("running");
  expect(record.translateJobId).toBe("job-live");
  expect(record.scanTotals.entries).toBe(20);
  expect(record.error).toBe(null);
});

test("a running record with no job id is de-zombified on every hydration", async () => {
  // Current version, so migrate never fires - but the zombie is still
  // reachable at v4 when the app dies inside startTranslate's await window.
  window.localStorage.setItem(
    "moru-sessions",
    JSON.stringify({ version: 4, state: { sessions: [legacyRecord("running")] } }),
  );
  await useSessions.persist.rehydrate();

  const record = useSessions.getState().sessions[0];
  expect(record.status).toBe("failed");
  expect(record.finishedAt).not.toBe(null);
  expect(typeof useSessions.getState().upsert).toBe("function"); // actions survive merge
});
