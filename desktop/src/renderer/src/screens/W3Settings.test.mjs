/**
 * W3 "export as source text" contract (kunho-park/moru-app#3): the step-3
 * export must reach the engine's export job with no translate job, no model
 * and no API key, and its result must stay in its own state slice — the W6
 * export paths and the session history row belong to the translated export.
 */
import { expect, test } from "bun:test";

// wizard.ts transitively imports lib/bridge (window.moru) and lib/web
// (window.localStorage) at module scope; merge globals before importing
// (other test files in the same bun process may have installed them).
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

/** POST bodies the store sent, so the job params can be asserted. */
const posts = [];
globalThis.fetch = async (url, init) => {
  posts.push({ url: String(url), body: JSON.parse(init.body) });
  return { ok: true, json: async () => ({ id: "export-job-1", type: "export" }) };
};

/** Captured frame sinks, one per opened job stream. */
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
bridge.moru.secrets ??= {
  get: async () => null,
  set: async () => undefined,
  delete: async () => undefined,
};
if (typeof bridge.moru.setBusy !== "function") bridge.moru.setBusy = () => undefined;

const { useWizard } = await import("../stores/wizard.ts");
const { useSessions } = await import("../stores/sessions.ts");
const { DEFAULT_MOD_BLACKLIST, useSettings } = await import("../stores/settings.ts");
const { useEngineStore } = await import("../stores/engine.ts");

useEngineStore.setState({
  info: { state: "ready", port: 59999, token: "tk", restarts: 0 },
});

/** A wizard sitting on W3 right after a scan: no run, no export, no key. */
function scannedWizard(overrides = {}) {
  useWizard.setState({
    sessionId: "sess-1",
    modpackPath: "/packs/atm10",
    modpackName: "ATM 10",
    sourceLocale: "en_us",
    targetLocale: "ko_kr",
    scanJobId: "scan-1",
    scanState: "done",
    scanResult: {
      modpack_path: "/packs/atm10",
      identity: null,
      categories: [
        { name: "lang", handler: "language", file_count: 1, entry_count: 2, char_count: 8, files: [] },
        { name: "quests", handler: "ftbquests", file_count: 1, entry_count: 3, char_count: 9, files: [] },
      ],
    },
    excludedCategories: [],
    translateJobId: null,
    runState: "idle",
    exportJobId: null,
    exportState: "idle",
    exportZipPath: null,
    exportOverridesZipPath: null,
    exportError: null,
    sourceExportJobId: null,
    sourceExportState: "idle",
    sourceExportZipPath: null,
    sourceExportOverridesZipPath: null,
    sourceExportError: null,
    ...overrides,
  });
  posts.length = 0;
  sockets.length = 0;
}

test("source export starts the export job with no translate job or api key", async () => {
  scannedWizard();
  await useWizard.getState().startSourceExport();

  expect(posts).toHaveLength(1);
  expect(posts[0].url).toContain("/jobs");
  expect(posts[0].body.type).toBe("export");
  expect(posts[0].body.params).toEqual({
    source_text: true,
    modpack_path: "/packs/atm10",
    source_locale: "en_us",
    target_locale: "ko_kr",
    // The source-text archive exists to mirror what a translated run would
    // produce, so it carries the same per-mod scope: a blacklisted mod is
    // absent from a translated pack and must be absent here too, or a diff
    // against a pack shared on moru.gg reports files that can never exist.
    // Visible on W2, never silent — the band there lists what was dropped.
    mod_blacklist: [...DEFAULT_MOD_BLACKLIST],
  });
  // Nothing that would demand a provider or a finished run was sent.
  expect(posts[0].body.params.translate_job_id).toBeUndefined();
  expect(posts[0].body.params.model).toBeUndefined();
  expect(posts[0].body.params.api_key).toBeUndefined();
  expect(useWizard.getState().sourceExportState).toBe("running");
  expect(useWizard.getState().sourceExportJobId).toBe("export-job-1");
});

test("the W2 category selection scopes the source export", async () => {
  scannedWizard({ excludedCategories: ["quests"] });
  await useWizard.getState().startSourceExport();
  expect(posts[0].body.params.include_categories).toEqual(["lang"]);

  // An untouched selection means "everything", which the engine expects as
  // an omitted field rather than a full list.
  scannedWizard();
  await useWizard.getState().startSourceExport();
  expect("include_categories" in posts[0].body.params).toBe(false);
});

test("a configured output directory rides along", async () => {
  useSettings.setState({ outputDir: "/exports/moru" });
  scannedWizard();
  await useWizard.getState().startSourceExport();
  expect(posts[0].body.params.output_dir).toBe("/exports/moru");
  useSettings.setState({ outputDir: null });
});

test("done frame fills only the source slice, never the W6 export state", async () => {
  useSessions.setState({
    sessions: [
      {
        id: "sess-1",
        modpackPath: "/packs/atm10",
        modpackName: "ATM 10",
        sourceLocale: "en_us",
        targetLocale: "ko_kr",
        model: "openai/gpt-test",
        status: "running",
        createdAt: 1_000,
        finishedAt: null,
        doneEntries: 0,
        totalEntries: 5,
        stats: null,
        error: null,
        exportZipPath: null,
        exportOverridesZipPath: null,
        sharedUrl: null,
      },
    ],
  });
  scannedWizard();
  await useWizard.getState().startSourceExport();
  sockets[0].emit({
    type: "done",
    status: "done",
    zip_path: "/exports/ATM 10 원문 (moru).zip",
    overrides_zip_path: "/exports/ATM 10 원문 (moru)_overrides.zip",
  });

  const state = useWizard.getState();
  expect(state.sourceExportState).toBe("done");
  expect(state.sourceExportZipPath).toBe("/exports/ATM 10 원문 (moru).zip");
  expect(state.sourceExportOverridesZipPath).toBe(
    "/exports/ATM 10 원문 (moru)_overrides.zip",
  );
  // The translated-export slice drives the W5/W6 nav unlock and History's
  // "open folder" button; a source-text export must not touch either.
  expect(state.exportState).toBe("idle");
  expect(state.exportZipPath).toBeNull();
  expect(state.exportOverridesZipPath).toBeNull();
  expect(useSessions.getState().sessions[0].exportZipPath).toBeNull();
});

test("failed and cancelled frames surface an error in the source slice", async () => {
  scannedWizard();
  await useWizard.getState().startSourceExport();
  sockets[0].emit({ type: "failed", status: "failed", error: "no handler" });
  expect(useWizard.getState().sourceExportState).toBe("failed");
  expect(useWizard.getState().sourceExportError).toBe("no handler");

  scannedWizard();
  await useWizard.getState().startSourceExport();
  sockets[0].emit({ type: "cancelled", status: "cancelled" });
  expect(useWizard.getState().sourceExportState).toBe("failed");
  expect(useWizard.getState().sourceExportError).toBe("source export failed");
});

test("a running source export is not started twice and resets with the wizard", async () => {
  scannedWizard();
  await useWizard.getState().startSourceExport();
  await useWizard.getState().startSourceExport();
  expect(posts).toHaveLength(1);

  useWizard.getState().reset();
  const state = useWizard.getState();
  expect(state.sourceExportState).toBe("idle");
  expect(state.sourceExportJobId).toBeNull();
  expect(state.sourceExportZipPath).toBeNull();
  expect(state.sourceExportError).toBeNull();
});

test("no modpack means no request", async () => {
  scannedWizard({ modpackPath: null });
  await useWizard.getState().startSourceExport();
  expect(posts).toHaveLength(0);
  expect(useWizard.getState().sourceExportState).toBe("idle");
});
