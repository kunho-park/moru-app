/**
 * Hand-translation queue loading — the v1.1.0 "손으로 번역하기 누르면 아무것도
 * 안뜬다" regression.
 *
 * The engine is not the problem and this file pins that down: a manual-seed
 * run settles every entry as PENDING and `filter=all` returns every one of
 * them. What broke was WHEN the desktop asked. W3 routes to W5M the instant
 * `POST /jobs` is accepted, and `GET /translate/{id}/entries` answers
 *
 *     409 {"detail": "translate job <id> is running; no result available"}
 *
 * until the run finishes. The screen made exactly one request, it failed, and
 * `open()` latched on the job id — so the queue stayed empty forever, with the
 * error rendered in a branch that only exists when an entry is focused.
 *
 * The fetch stub below replies with the two payloads a real sidecar produced
 * for this sequence (recorded verbatim, including the 409 detail string), and
 * flips from one to the other only when the job's result exists. Nothing here
 * hard-codes the fixed behaviour: the stub is the server contract, and the
 * assertions are about what the store does with it.
 */
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
win.crypto ??= globalThis.crypto;

const JOB_ID = "manual-job";
const SOURCE_FILE = "kubejs/assets/reprotest/lang/en_us.json";

/** Exactly what a manual-seed run leaves behind: PENDING, no text. */
const PENDING_ENTRIES = [
  "item.reprotest.ingot",
  "item.reprotest.gear",
  "item.reprotest.pickaxe",
  "block.reprotest.altar",
  "tooltip.reprotest.desc1",
].map((key) => ({
  key,
  file: SOURCE_FILE,
  source_text: key,
  translated_text: "",
  status: "pending",
  errors: [],
}));

/**
 * Stands in for the sidecar. `resultReady` is the engine's own condition:
 * `_get_pipeline_result` raises 409 while `record.result is None`.
 */
const server = { resultReady: false };
/** Every engine request the stores issued, so the real URL can be asserted. */
const requests = [];

globalThis.fetch = async (url, init) => {
  const href = String(url);
  requests.push({ href, method: init?.method ?? "GET" });

  if (init?.method === "POST" && href.includes("/jobs")) {
    return {
      ok: true,
      json: async () => ({
        id: JOB_ID,
        type: "translate",
        status: "running",
        error: null,
        created_at: new Date().toISOString(),
      }),
    };
  }
  if (href.includes("/placeholder/patterns")) {
    return { ok: true, json: async () => ({ patterns: [] }) };
  }
  if (href.includes("/entries/counts")) {
    if (!server.resultReady) return notReady(href);
    return {
      ok: true,
      json: async () => ({
        all: PENDING_ENTRIES.length,
        pending: PENDING_ENTRIES.length,
        failed: 0,
        warning: 0,
        modified: 0,
        flagged: 0,
        stale_source: 0,
      }),
    };
  }
  if (href.includes("/entries")) {
    if (!server.resultReady) return notReady(href);
    const filter = new URL(href).searchParams.get("filter");
    // `all` is what the screen asks for, and on a seed run every entry in it
    // is PENDING. A filter that dropped them would return 0 here.
    const entries = filter === "all" || filter === "pending" ? PENDING_ENTRIES : [];
    return { ok: true, json: async () => ({ total: entries.length, page: 1, entries }) };
  }
  return { ok: true, json: async () => ({}) };
};

/** The engine's real 409, detail string included. */
function notReady(href) {
  const id = href.split("/translate/")[1]?.split("/")[0] ?? JOB_ID;
  return {
    ok: false,
    status: 409,
    statusText: "Conflict",
    json: async () => ({ detail: `translate job ${id} is running; no result available` }),
  };
}

const sockets = [];
globalThis.WebSocket = class {
  constructor(url) {
    this.url = url;
    sockets.push(this);
  }
  close() {
    this.closed = true;
  }
  emit(frame) {
    this.onmessage({ data: JSON.stringify(frame) });
  }
};

const bridge = await import("../lib/bridge.ts");
bridge.moru.secrets ??= { get: async () => null };
if (typeof bridge.moru.setBusy !== "function") bridge.moru.setBusy = () => undefined;

const { useWizard } = await import("../stores/wizard.ts");
const { useManual } = await import("../stores/manual.ts");
const { useSessions } = await import("../stores/sessions.ts");
const { useEngineStore } = await import("../stores/engine.ts");
const { useTranslationQueue } = await import("../stores/translationQueue.ts");

useEngineStore.setState({
  info: { state: "ready", port: 59999, token: "tk", restarts: 0 },
});

beforeEach(() => {
  requests.length = 0;
  sockets.length = 0;
  server.resultReady = false;
  useSessions.setState({ sessions: [] });
  useTranslationQueue.setState({ phase: "idle" });
  useManual.getState().reset();
  useWizard.setState({
    sessionId: "sess-1",
    modpackPath: "/packs/repro",
    modpackName: "Repro",
    sourceLocale: "en_us",
    targetLocale: "ko_kr",
    runState: "idle",
    runError: null,
    scanJobId: "scan-1",
    scanResult: null,
    excludedCategories: [],
    translateJobId: null,
  });
});

/** The terminal frame the sidecar replays; it is what makes the result exist. */
function finishSeedRun() {
  server.resultReady = true;
  for (const socket of sockets) {
    if (socket.onmessage != null && socket.url.includes(JOB_ID)) {
      socket.emit({ type: "done", stats: null, emitted_at: Date.now() });
    }
  }
}

test("the seed run is still running when W3 hands off to W5M", async () => {
  expect(await useWizard.getState().startManualSeed()).toBe("started");
  // The screen is entered here, before any terminal frame: this is the race.
  expect(useWizard.getState().translateJobId).toBe(JOB_ID);
  expect(useWizard.getState().runState).toBe("running");
});

/**
 * The bug itself. `open()` used to latch on the job id, so the one request it
 * made while the job was still running was the only request it would ever
 * make — a permanently empty queue with no retry and no message.
 */
test("a queue snapshot that raced the run is retried once the result exists", async () => {
  await useWizard.getState().startManualSeed();

  // 1. The screen opens while the engine has no result yet.
  await useManual.getState().open(JOB_ID);
  expect(useManual.getState().refs).toHaveLength(0);
  // The failure is recorded rather than swallowed...
  expect(useManual.getState().error).toContain("no result available");

  // 2. The run finishes; the result now exists.
  finishSeedRun();
  expect(useWizard.getState().runState).toBe("done");

  // 3. ...and re-opening the same job actually re-asks.
  await useManual.getState().open(JOB_ID);
  expect(useManual.getState().error).toBeNull();
  expect(useManual.getState().refs).toHaveLength(PENDING_ENTRIES.length);
  expect(useManual.getState().total).toBe(PENDING_ENTRIES.length);
});

/** A good snapshot is still opened exactly once — the StrictMode guard. */
test("re-opening a loaded job does not re-fetch", async () => {
  server.resultReady = true;
  await useManual.getState().open(JOB_ID);
  expect(useManual.getState().refs).toHaveLength(PENDING_ENTRIES.length);

  const before = requests.filter((r) => r.href.includes("/entries?")).length;
  await useManual.getState().open(JOB_ID);
  const after = requests.filter((r) => r.href.includes("/entries?")).length;
  expect(after).toBe(before);
  expect(useManual.getState().refs).toHaveLength(PENDING_ENTRIES.length);
});

/**
 * Suspect that was NOT the cause, pinned so nobody "fixes" it by changing the
 * bucket: the screen asks for `all`, and `all` carries the PENDING entries a
 * seed run produces.
 */
test("the queue asks for filter=all and gets the pending entries back", async () => {
  server.resultReady = true;
  await useManual.getState().open(JOB_ID);

  const entriesRequest = requests.find((r) => r.href.includes("/entries?"));
  expect(entriesRequest).toBeDefined();
  expect(entriesRequest.href).toContain(`/translate/${JOB_ID}/entries`);
  expect(entriesRequest.href).toContain("filter=all");

  const loaded = Object.values(useManual.getState().entries);
  expect(loaded).toHaveLength(PENDING_ENTRIES.length);
  expect(loaded.every((e) => e.status === "pending")).toBe(true);
  expect(loaded.every((e) => e.translated_text === "")).toBe(true);
});

/**
 * The seed must ride the shared event plumbing. A raw socket left `runState`
 * pinned at "running" when a stream dropped without its terminal frame, and
 * the queue is gated on that state.
 */
test("the seed's event stream recovers the session on an unexpected close", async () => {
  await useWizard.getState().startManualSeed();
  const socket = sockets.find((s) => s.url.includes(JOB_ID));
  expect(socket).toBeDefined();

  // A stream that drops without its terminal frame. Because W5M gates the
  // queue on `runState`, letting this pass unnoticed would pin the screen on
  // "preparing" forever — so the seed has to use the same recovery hook an
  // ordinary run does, which a bare `openJobEvents` call did not install.
  socket.onclose({ code: 1006 });
  const logged = useWizard.getState().log.map((line) => line.text);
  expect(logged).toContain("event stream disconnected; restoring current engine state");
});
