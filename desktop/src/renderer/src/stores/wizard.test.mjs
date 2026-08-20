/** Translation-session restore contracts: engine snapshot first, then a
 * cursor-based live WebSocket for running work or a result probe for review. */
import { beforeEach, expect, test } from "bun:test";

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
win.moru ??= {};

const fetchCalls = [];
let nextResponse = null;
/** url substring -> response, consulted before the shared nextResponse. */
const routeResponses = new Map();
const responses = [];
globalThis.fetch = async (url) => {
  const href = String(url);
  fetchCalls.push(href);
  for (const [needle, response] of routeResponses) {
    if (href.includes(needle)) return response;
  }
  const queued = responses.shift();
  if (queued !== undefined) return queued;
  if (nextResponse !== null) return nextResponse;
  throw new Error(`unexpected fetch: ${url}`);
};

const sockets = [];
class FakeWebSocket {
  constructor(url) {
    this.url = url;
    this.onmessage = null;
    this.onclose = null;
    sockets.push(this);
  }
  close() {}
  emit(frame) {
    this.onmessage?.({ data: JSON.stringify(frame) });
  }
}
globalThis.WebSocket = FakeWebSocket;

const { buildTranslateParams, selectedScanTotals, useWizard, useSessionJobs } = await import("./wizard.ts");
const { useSessions } = await import("./sessions.ts");
const { useEngineStore } = await import("./engine.ts");
const bridge = await import("../lib/bridge.ts");
bridge.moru.setBusy = () => undefined;

useEngineStore.setState({
  info: { state: "ready", port: 59999, token: "tk", restarts: 0 },
});

const totals = {
  files: 2,
  entries: 50,
  chars: 500,
  migrationEntries: 10,
  migrationChars: 100,
  translationEntries: 40,
  translationChars: 400,
};

const runSettings = {
  outputDir: null,
  model: "openai/gpt-test",
  temperature: 0.3,
  batchSize: 30,
  maxConcurrent: 4,
  maxRefine: 2,
  thinkingEnabled: false,
  thinkingEffort: "medium",
  useTm: true,
  useVanillaGlossary: true,
  extractGlossary: true,
  glossaryMaxTerms: 3000,
  ollamaBaseUrl: "http://localhost:11434",
  openaiCompatBaseUrl: "http://localhost:1234/v1",
  targetLocale: "ko_kr",
};

const translateState = {
  modpackPath: "C:/packs/current",
  sourceLocale: "en_us",
  targetLocale: "ko_kr",
  migrationEnabled: false,
  previousModpackPath: "C:/packs/old",
  previousResourcepackPath: "C:/translations/old-rp.zip",
  previousOverridesPath: "C:/translations/old-overrides.zip",
  scanJobId: "w2-scan",
  scanResult: null,
  excludedCategories: [],
};

function record(id, status, overrides = {}) {
  return {
    id,
    modpackPath: `/packs/${id}`,
    modpackName: `Pack ${id}`,
    sourceLocale: "en_us",
    targetLocale: "ko_kr",
    model: "openai/gpt-test",
    status,
    createdAt: 1_000,
    finishedAt: status === "running" ? null : 2_000,
    doneEntries: 42,
    totalEntries: 50,
    translateJobId: null,
    scanTotals: totals,
    stats: status === "running" ? null : stats,
    error: null,
    exportZipPath: status === "done" ? "/exports/pack.zip" : null,
    exportOverridesZipPath: null,
    sharedUrl: null,
    ...overrides,
  };
}

const stats = {
  total_files: 2,
  total_entries: 50,
  translated_entries: 30,
  failed_entries: 2,
  tm_hits: 8,
  migration_hits: 10,
  skipped_entries: 0,
  categories: {},
  prompt_tokens: 100,
  completion_tokens: 20,
  duration_seconds: 1,
  coverage_percent: 96,
  quality_score: 0.96,
};

function response(body, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    statusText: status === 404 ? "Not Found" : "OK",
    json: async () => body,
  };
}

function snapshot(id, status, events = [], overrides = {}) {
  return {
    job: {
      id,
      type: "translate",
      status,
      error: status === "failed" ? "provider failed" : null,
      created_at: new Date(1_000).toISOString(),
    },
    cursor: events.at(-1)?.seq ?? 0,
    events,
    failed_count: 0,
    translation_started_at: 1_200,
    ...overrides,
  };
}

const okPage = response({ entries: [], total: 0, page: 1 });
const notFound = response({ detail: "unknown job" }, 404);

beforeEach(() => {
  useWizard.getState().reset();
  useSessionJobs.setState({ jobs: {} });
  useSessions.setState({ sessions: [] });
  fetchCalls.length = 0;
  responses.length = 0;
  nextResponse = null;
  routeResponses.clear();
  sockets.length = 0;
});

test("refuses to clobber a different run in flight", async () => {
  useWizard.setState({ sessionId: "other", runState: "running" });
  useSessions.getState().upsert(record("s1", "done", { translateJobId: "job-1" }));
  expect(await useWizard.getState().reopenSession("s1")).toBe("busy");
  expect(fetchCalls).toHaveLength(0);
});

test("selected scan totals separate migration reuse inside included categories", () => {
  const scanResult = {
    categories: [
      {
        name: "Included",
        file_count: 2,
        entry_count: 7,
        char_count: 70,
        files: [
          { migration_entry_count: 2, migration_char_count: 20 },
          { migration_entry_count: 1, migration_char_count: 5 },
        ],
      },
      {
        name: "Excluded",
        file_count: 1,
        entry_count: 4,
        char_count: 40,
        files: [{ migration_entry_count: 4, migration_char_count: 40 }],
      },
    ],
  };
  expect(selectedScanTotals({ scanResult, excludedCategories: ["Excluded"] })).toEqual({
    files: 2,
    entries: 7,
    chars: 70,
    migrationEntries: 3,
    migrationChars: 25,
    translationEntries: 4,
    translationChars: 45,
  });
});

test("uses compact persisted totals after the full scan tree is gone", () => {
  expect(
    selectedScanTotals({ scanResult: null, scanTotals: totals, excludedCategories: [] }),
  ).toEqual(totals);
});

test("ordinary queued/manual translation reuses the completed v1.0 W2 scan", () => {
  const params = buildTranslateParams(translateState, runSettings, "secret");
  expect(params.scan_job_id).toBe("w2-scan");
  expect(params.previous_modpack_path).toBeUndefined();
  expect(params.previous_resourcepack_path).toBeUndefined();
  expect(params.previous_overrides_path).toBeUndefined();
  expect(params.include_categories).toBeUndefined();
});

test("A/B/C migration alone reuses its W2 scan and previous inputs", () => {
  const params = buildTranslateParams(
    { ...translateState, migrationEnabled: true },
    runSettings,
  );
  expect(params.scan_job_id).toBe("w2-scan");
  expect(params.previous_modpack_path).toBe("C:/packs/old");
  expect(params.previous_resourcepack_path).toEndWith("old-rp.zip");
  expect(params.previous_overrides_path).toEndWith("old-overrides.zip");
});

test("review stat updates reach both the live wizard and persisted session", () => {
  useSessions.getState().upsert(record("s1", "done", { translateJobId: "job-1" }));
  useWizard.setState({ sessionId: "s1", stats });
  const refreshed = {
    ...stats,
    translated_entries: 31,
    migration_hits: 9,
    coverage_percent: 96,
  };

  useWizard.getState().updateReviewStats(refreshed);

  expect(useWizard.getState().stats).toEqual(refreshed);
  expect(useSessions.getState().sessions[0].stats).toEqual(refreshed);
});

test("falls back to the persisted session id before reporting a missing job", async () => {
  useSessions.getState().upsert(record("s1", "done"));
  responses.push(notFound, notFound);
  fetchCalls.length = 0;
  expect(await useWizard.getState().reopenSession("s1")).toBe("gone");
  expect(fetchCalls).toHaveLength(2);
  expect(fetchCalls[0]).toContain("/jobs/s1/snapshot");
  expect(fetchCalls[1]).toContain("/sessions/s1/restore");
});

test("hydrates a finished run from snapshot then verifies review result", async () => {
  useSessions.getState().upsert(record("s1", "done", { translateJobId: "job-1" }));
  responses.push(
    response(
      snapshot("job-1", "done", [
        {
          type: "progress",
          stage: "translate",
          file: "a.json",
          done: 50,
          total: 50,
          seq: 1,
          emitted_at: 1_500,
        },
        {
          type: "tokens",
          prompt_tokens: 100,
          completion_tokens: 20,
          seq: 2,
          emitted_at: 1_800,
        },
        { type: "done", status: "done", stats, seq: 3, emitted_at: 2_000 },
      ]),
    ),
    okPage,
    notFound,
  );

  expect(await useWizard.getState().reopenSession("s1")).toBe("ok");
  expect(fetchCalls[0]).toContain("/jobs/job-1/snapshot");
  expect(fetchCalls[1]).toContain("/translate/job-1/entries");
  const wizard = useWizard.getState();
  expect(wizard.runState).toBe("done");
  expect(wizard.scanTotals).toEqual(totals);
  expect(wizard.fileProgress["a.json"].done).toBe(50);
  expect(wizard.promptTokens).toBe(100);
  expect(wizard.stats.coverage_percent).toBe(96);
  expect(wizard.exportState).toBe("done");
});

test("restores a running job and consumes only post-snapshot events", async () => {
  useSessions.getState().upsert(record("s1", "running", { translateJobId: "job-live" }));
  responses.push(
    response(
      snapshot(
        "job-live",
        "running",
        [
          {
            type: "progress",
            stage: "translate",
            file: "a.json",
            done: 20,
            total: 40,
            seq: 8,
            emitted_at: 1_500,
          },
          {
            type: "batch_started",
            request_id: 4,
            file: "a.json",
            key: "entry.a",
            entries: 5,
            seq: 9,
            emitted_at: 1_600,
          },
        ],
        { cursor: 9, failed_count: 12 },
      ),
    ),
    notFound,
  );

  expect(await useWizard.getState().reopenSession("s1")).toBe("ok");
  expect(useWizard.getState().runState).toBe("running");
  expect(useWizard.getState().doneEntries).toBe(20);
  expect(useWizard.getState().failedEntryCount).toBe(12);
  expect(Object.keys(useWizard.getState().activeBatches)).toEqual(["4"]);
  expect(sockets).toHaveLength(1);
  expect(sockets[0].url).toContain("after=9");

  sockets[0].emit({
    type: "progress",
    stage: "translate",
    file: "a.json",
    done: 25,
    total: 40,
    seq: 10,
    emitted_at: 1_700,
  });
  expect(useWizard.getState().doneEntries).toBe(25);
});

test("invalidates a persisted running job when the sidecar lost it", async () => {
  useSessions.getState().upsert(record("s1", "running", { translateJobId: "dead" }));
  useWizard.setState({ sessionId: "s1", runState: "running", translateJobId: "dead" });
  responses.push(notFound);

  expect(await useWizard.getState().reopenSession("s1")).toBe("gone");
  const stored = useSessions.getState().sessions.find((item) => item.id === "s1");
  expect(stored.translateJobId).toBe(null);
  expect(stored.status).toBe("failed");
  expect(useWizard.getState().runState).toBe("failed");
});

test("failed history records are not reopened", async () => {
  useSessions.getState().upsert(record("s1", "failed", { translateJobId: "job-1" }));
  expect(await useWizard.getState().reopenSession("s1")).toBe("gone");
  expect(fetchCalls).toHaveLength(0);
});

test("restores the scan screen only when the session carries a scan payload", async () => {
  const scanPayload = { modpack_path: "/packs/s4", categories: [], identity: null };
  useSessions.getState().upsert(record("s4", "done"));
  useSessionJobs.getState().register("s4", "job-4");
  routeResponses.set("/jobs/job-4/snapshot", response(snapshot("job-4", "done")));
  routeResponses.set("/translate/job-4/entries", okPage);
  routeResponses.set("/scan/job-4/result", response(scanPayload));

  expect(await useWizard.getState().reopenSession("s4")).toBe("ok");
  expect(useWizard.getState().scanResult).toEqual(scanPayload);
  expect(useWizard.getState().scanState).toBe("done"); // otherwise w3 is unreachable

  // A different session with no persisted payload: the scan screen stays
  // idle rather than half-restored. (Reopening s4 again would short-circuit
  // on the already-live path and keep its state.)
  useSessions.getState().upsert(record("s5", "done", { scanTotals: null }));
  useSessionJobs.getState().register("s5", "job-5");
  routeResponses.set("/jobs/job-5/snapshot", response(snapshot("job-5", "done")));
  routeResponses.set("/translate/job-5/entries", okPage);
  routeResponses.set("/scan/job-5/result", notFound);
  expect(await useWizard.getState().reopenSession("s5")).toBe("ok");
  expect(useWizard.getState().scanResult).toBe(null);
  expect(useWizard.getState().scanState).toBe("idle");
  routeResponses.clear();
});
