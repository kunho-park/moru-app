/**
 * Hand-translation seed contracts.
 *
 * Two things must hold and both have a real failure mode:
 *
 * - The request must carry NO provider fields. If a model or key leaks in, the
 *   history row misreports how the pack was translated, and the feature's
 *   premise (usable with nothing configured) stops being verifiable.
 * - It must refuse while the translation queue is draining. The queue drives
 *   the same single wizard session and reads a `sessionId` change as
 *   abandonment, so seeding here mid-drain silently drops a queued pack.
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
win.moru ??= {
  platform: "win32",
  versions: { app: "test", electron: "test" },
  setBusy: () => undefined,
  secrets: { get: async () => null },
};

const posts = [];
globalThis.fetch = async (url, init) => {
  const href = String(url);
  if (init?.method === "POST" && href.includes("/jobs")) {
    posts.push(JSON.parse(init.body));
    return {
      ok: true,
      json: async () => ({
        id: "manual-job",
        type: "translate",
        status: "running",
        error: null,
        created_at: new Date().toISOString(),
      }),
    };
  }
  return { ok: true, json: async () => ({}) };
};

class FakeWebSocket {
  constructor(url) {
    this.url = url;
    this.onmessage = null;
    this.onclose = null;
  }
  close() {}
}
globalThis.WebSocket = FakeWebSocket;

const { useWizard } = await import("./wizard.ts");
const { useSessions } = await import("./sessions.ts");
const { useEngineStore } = await import("./engine.ts");
const { useTranslationQueue } = await import("./translationQueue.ts");
const bridge = await import("../lib/bridge.ts");
bridge.moru.setBusy = () => undefined;

useEngineStore.setState({
  info: { state: "ready", port: 59999, token: "tk", restarts: 0 },
});

beforeEach(() => {
  posts.length = 0;
  useSessions.setState({ sessions: [] });
  useTranslationQueue.setState({ phase: "idle" });
  useWizard.setState({
    sessionId: "sess-1",
    modpackPath: "/packs/stub",
    modpackName: "Stub",
    sourceLocale: "en_us",
    targetLocale: "ko_kr",
    runState: "idle",
    scanJobId: "scan-1",
    scanResult: null,
    excludedCategories: [],
  });
});

test("the seed request carries manual_seed and no provider fields", async () => {
  const outcome = await useWizard.getState().startManualSeed();
  expect(outcome).toBe("started");
  expect(posts).toHaveLength(1);

  const { type, params } = posts[0];
  expect(type).toBe("translate");
  expect(params.manual_seed).toBe(true);
  // The whole point: nothing here can reach a provider.
  expect(params.model).toBeUndefined();
  expect(params.api_key).toBeUndefined();
  expect(params.api_base).toBeUndefined();
  // Only glossary curation needs a model, so only it is off.
  expect(params.extract_glossary).toBe(false);
});

test("the LLM-free helper stages are still requested", async () => {
  await useWizard.getState().startManualSeed();
  const { params } = posts[0];
  // Pure local lookups, and exactly the aids a hand translator leans on.
  expect(params.use_tm).toBe(true);
  expect(params.use_vanilla_glossary).toBe(true);
});

test("the run is recorded with no model rather than a fake one", async () => {
  await useWizard.getState().startManualSeed();
  expect(useWizard.getState().model).toBeNull();
  const record = useSessions.getState().sessions.find((s) => s.id === "sess-1");
  expect(record.model).toBe("");
  expect(record.status).toBe("running");
});

test("it refuses while the queue is draining", async () => {
  useTranslationQueue.setState({ phase: "running" });
  const outcome = await useWizard.getState().startManualSeed();
  expect(outcome).toBe("busy");
  expect(posts).toHaveLength(0);
  // The queued pack's session must be untouched.
  expect(useWizard.getState().sessionId).toBe("sess-1");
  expect(useWizard.getState().runState).toBe("idle");
});

test("it refuses while the queue is pausing", async () => {
  useTranslationQueue.setState({ phase: "pausing" });
  expect(await useWizard.getState().startManualSeed()).toBe("busy");
  expect(posts).toHaveLength(0);
});

test("a paused queue does not block a manual session", async () => {
  // Paused means nothing is mid-flight, so the wizard session is free.
  useTranslationQueue.setState({ phase: "paused" });
  expect(await useWizard.getState().startManualSeed()).toBe("started");
});

test("it refuses while a translation is already running", async () => {
  useWizard.setState({ runState: "running" });
  expect(await useWizard.getState().startManualSeed()).toBe("busy");
  expect(posts).toHaveLength(0);
});

test("it refuses with no modpack selected", async () => {
  useWizard.setState({ modpackPath: null });
  expect(await useWizard.getState().startManualSeed()).toBe("busy");
  expect(posts).toHaveLength(0);
});
