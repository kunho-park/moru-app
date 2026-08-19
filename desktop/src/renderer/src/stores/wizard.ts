/**
 * Wizard session state (W1-W6). One active translation session at a time;
 * job lifecycles (scan/translate/export) and their WS event streams are
 * driven from here so screens stay declarative.
 */

import { create } from "zustand";

import type {
  JobEventFrame,
  JobSnapshot,
  PipelineStats,
  ScanResult,
  TranslateParams,
} from "../../../shared/engine";
import type { ModpackProbe } from "../../../shared/bridge";
import { api, openJobEvents } from "../lib/api";
import { providerIdOf } from "../lib/models";
import { moru } from "../lib/bridge";
import { WEB_URL } from "../lib/web";
import { useSessions, type SessionScanTotals } from "./sessions";
import {
  snapshotTranslationSettings,
  useSettings,
  type TranslationRunSettings,
} from "./settings";

export interface TickerPair {
  key: string;
  source: string;
  translated: string;
}

export interface ActiveBatch {
  requestId: number;
  file: string;
  key: string;
  entries: number;
  startedAt: number;
}

export interface FileProgress {
  file: string;
  done: number;
  total: number;
}

export interface GlossaryProgress {
  /** extraction chunks completed / total */
  done: number;
  total: number;
  newTerms: number;
  /** attempt number currently being retried after a schema error */
  retrying: number | null;
  lastError: string | null;
}

export interface LogLine {
  ts: number;
  level: "info" | "warn" | "error";
  text: string;
}

/**
 * Hot runtime cache of session id -> engine translate job id. SessionRecord
 * also persists the id so a renderer reload can probe the still-running
 * sidecar. Every restore verifies it; a sidecar restart invalidates the id.
 */
interface SessionJobsStore {
  jobs: Record<string, string>;
  register: (sessionId: string, jobId: string) => void;
  unregister: (sessionId: string) => void;
}

export const useSessionJobs = create<SessionJobsStore>((set) => ({
  jobs: {},
  register: (sessionId, jobId) =>
    set((state) => ({ jobs: { ...state.jobs, [sessionId]: jobId } })),
  unregister: (sessionId) =>
    set((state) => {
      if (!(sessionId in state.jobs)) return state;
      const jobs = { ...state.jobs };
      delete jobs[sessionId];
      return { jobs };
    }),
}));

interface WizardStore {
  /* W1 */
  sessionId: string | null;
  modpackPath: string | null;
  modpackName: string;
  probe: ModpackProbe | null;
  sourceLocale: string;
  targetLocale: string;
  migrationEnabled: boolean;
  previousModpackPath: string | null;
  previousResourcepackPath: string | null;
  previousOverridesPath: string | null;

  /* W2 */
  scanJobId: string | null;
  scanState: "idle" | "running" | "done" | "failed";
  scanProgress: { current: number; total: number; message: string };
  scanError: string | null;
  scanResult: ScanResult | null;
  /** Selected W2 totals retained when a restored run no longer has its tree. */
  scanTotals: SessionScanTotals | null;
  /** category names excluded from translation (default: all included) */
  excludedCategories: string[];

  /* W4 */
  translateJobId: string | null;
  /** Model the run was started with; settings.model may drift afterwards. */
  model: string | null;
  runState: "idle" | "running" | "done" | "failed" | "cancelled";
  runError: string | null;
  startedAt: number | null;
  /** First provider batch start; excludes scan/glossary from live throughput. */
  translationStartedAt: number | null;
  finishedAt: number | null;
  doneEntries: number;
  fileProgress: Record<string, FileProgress>;
  glossaryProgress: GlossaryProgress | null;
  failedKeys: Record<string, string[]>;
  /** Total failures; failedKeys intentionally keeps only the useful tail. */
  failedEntryCount: number;
  promptTokens: number;
  completionTokens: number;
  /** prompt tokens served from the provider cache (subset of promptTokens) */
  cachedTokens: number;
  ticker: TickerPair[];
  activeBatches: Record<number, ActiveBatch>;
  log: LogLine[];
  stats: PipelineStats | null;

  /* W6 */
  exportJobId: string | null;
  exportState: "idle" | "running" | "done" | "failed";
  exportZipPath: string | null;
  exportOverridesZipPath: string | null;
  exportError: string | null;

  /* actions */
  startSession: (path: string, probe: ModpackProbe, targetLocale?: string) => void;
  resumeSession: (sessionId: string) => boolean;
  /** Reopens a finished session on W5/W6 after verifying the engine job. */
  reopenSession: (sessionId: string) => Promise<"ok" | "busy" | "gone">;
  setTargetLocale: (locale: string) => void;
  setMigrationEnabled: (enabled: boolean) => void;
  setMigrationInput: (
    kind: "modpack" | "resourcepack" | "overrides",
    path: string | null,
  ) => void;
  toggleCategory: (name: string, included: boolean) => void;
  setCategories: (names: string[], included: boolean) => void;
  startScan: () => Promise<void>;
  startTranslate: (settingsOverride?: TranslationRunSettings) => Promise<void>;
  handleTranslationFrame: (frame: JobEventFrame, sessionId: string) => void;
  cancelTranslate: () => Promise<void>;
  updateReviewStats: (stats: PipelineStats) => void;
  startExport: () => Promise<void>;
  appendLog: (level: LogLine["level"], text: string) => void;
  reset: () => void;
}

/** Entry volume of the current selection, from the enriched scan result. */
export function selectedScanTotals(state: {
  scanResult: ScanResult | null;
  excludedCategories: string[];
  scanTotals?: SessionScanTotals | null;
}): SessionScanTotals {
  if (state.scanResult === null && state.scanTotals != null) {
    return state.scanTotals;
  }
  let files = 0;
  let entries = 0;
  let chars = 0;
  let migrationEntries = 0;
  let migrationChars = 0;
  if (state.scanResult !== null) {
    for (const cat of state.scanResult.categories) {
      if (state.excludedCategories.includes(cat.name)) continue;
      files += cat.file_count;
      entries += cat.entry_count;
      chars += cat.char_count;
      migrationEntries += cat.files.reduce(
        (sum, file) => sum + (file.migration_entry_count ?? 0),
        0,
      );
      migrationChars += cat.files.reduce(
        (sum, file) => sum + (file.migration_char_count ?? 0),
        0,
      );
    }
  }
  return {
    files,
    entries,
    chars,
    migrationEntries,
    migrationChars,
    translationEntries: Math.max(0, entries - migrationEntries),
    translationChars: Math.max(0, chars - migrationChars),
  };
}

type TranslateParamState = Pick<
  WizardStore,
  | "sourceLocale"
  | "targetLocale"
  | "migrationEnabled"
  | "previousModpackPath"
  | "previousResourcepackPath"
  | "previousOverridesPath"
  | "scanJobId"
  | "scanResult"
  | "excludedCategories"
> & { modpackPath: string };

/** Pure request builder shared by manual and queued ordinary translations. */
export function buildTranslateParams(
  state: TranslateParamState,
  settings: TranslationRunSettings,
  apiKey?: string,
): TranslateParams {
  const providerId = providerIdOf(settings.model);
  const apiBase =
    providerId === "ollama"
      ? settings.ollamaBaseUrl
      : providerId === "openai-compatible"
        ? settings.openaiCompatBaseUrl
        : undefined;
  return {
    modpack_path: state.modpackPath,
    source_locale: state.sourceLocale,
    target_locale: state.targetLocale,
    model: settings.model,
    api_key: apiKey,
    api_base: apiBase,
    temperature: settings.temperature,
    batch_size: settings.batchSize,
    max_concurrent: settings.maxConcurrent,
    max_refine: settings.maxRefine,
    reasoning_effort: settings.thinkingEnabled ? settings.thinkingEffort : undefined,
    use_tm: settings.useTm,
    use_vanilla_glossary: settings.useVanillaGlossary,
    extract_glossary: settings.extractGlossary,
    glossary_max_terms: settings.glossaryMaxTerms,
    include_categories:
      state.excludedCategories.length > 0 && state.scanResult !== null
        ? state.scanResult.categories
            .map((category) => category.name)
            .filter((name) => !state.excludedCategories.includes(name))
        : undefined,
    output_dir: settings.outputDir ?? undefined,
    // Reuse the completed W2 scan through the official v1.0 session pipeline.
    // Migration scans receive an additional A/B/C fingerprint check server-side.
    scan_job_id: state.scanJobId ?? undefined,
    ...(state.migrationEnabled && state.previousModpackPath !== null
      ? { previous_modpack_path: state.previousModpackPath }
      : {}),
    ...(state.migrationEnabled && state.previousResourcepackPath !== null
      ? { previous_resourcepack_path: state.previousResourcepackPath }
      : {}),
    ...(state.migrationEnabled && state.previousOverridesPath !== null
      ? { previous_overrides_path: state.previousOverridesPath }
      : {}),
  };
}

let closeScanEvents: (() => void) | null = null;
let closeTranslateEvents: (() => void) | null = null;
let closeExportEvents: (() => void) | null = null;
let lastSessionProgressPersistedAt = 0;

const MAX_LOG_LINES = 500;
const MAX_TICKER = 24;
const MAX_FAILED_KEY_SAMPLES = 50;

function attachTranslationEvents(jobId: string, sessionId: string, after?: number): void {
  closeTranslateEvents?.();
  closeTranslateEvents = openJobEvents(
    jobId,
    (frame) => useWizard.getState().handleTranslationFrame(frame, sessionId),
    (_event, manuallyClosed) => {
      const state = useWizard.getState();
      if (
        manuallyClosed ||
        state.sessionId !== sessionId ||
        state.translateJobId !== jobId ||
        state.runState !== "running"
      ) {
        return;
      }
      state.appendLog("warn", "event stream disconnected; restoring current engine state");
      window.setTimeout(() => {
        const current = useWizard.getState();
        if (
          current.sessionId === sessionId &&
          current.translateJobId === jobId &&
          current.runState === "running"
        ) {
          void current.reopenSession(sessionId);
        }
      }, 500);
    },
    after,
  );
}

function withLog(
  log: LogLine[],
  level: LogLine["level"],
  text: string,
  ts: number,
): LogLine[] {
  if (log.at(-1)?.text === text) return log;
  return [...log.slice(-MAX_LOG_LINES + 1), { ts, level, text }];
}

/** Pure event reducer shared by first-run streaming and snapshot restoration. */
function translationFramePatch(
  prev: WizardStore,
  frame: JobEventFrame,
): Partial<WizardStore> {
  const ts = frame.emitted_at ?? Date.now();
  switch (frame.type) {
    case "progress": {
      const file = frame.file;
      if (frame.stage === "translate" && file !== undefined) {
        const done = frame.done ?? 0;
        const total = frame.total ?? 0;
        const previousDone = prev.fileProgress[file]?.done ?? 0;
        return {
          fileProgress: {
            ...prev.fileProgress,
            [file]: { file, done, total },
          },
          doneEntries: Math.max(0, prev.doneEntries - previousDone + done),
        };
      }
      return { log: withLog(prev.log, "info", `stage: ${frame.stage}`, ts) };
    }
    case "batch_started":
      return {
        translationStartedAt: prev.translationStartedAt ?? ts,
        activeBatches: {
          ...prev.activeBatches,
          [frame.request_id]: {
            requestId: frame.request_id,
            file: frame.file,
            key: frame.key,
            entries: frame.entries,
            startedAt: ts,
          },
        },
      };
    case "batch_finished": {
      const activeBatches = { ...prev.activeBatches };
      delete activeBatches[frame.request_id];
      return { activeBatches };
    }
    case "tokens":
      return {
        promptTokens: frame.prompt_tokens,
        completionTokens: frame.completion_tokens,
        ...(frame.cached_tokens !== undefined ? { cachedTokens: frame.cached_tokens } : {}),
      };
    case "entry_done":
      return {
        ticker: [
          { key: frame.key, source: frame.source, translated: frame.translated },
          ...prev.ticker,
        ].slice(0, MAX_TICKER),
      };
    case "entry_failed": {
      const failedKeys = { ...prev.failedKeys };
      failedKeys[frame.key] = frame.errors;
      while (Object.keys(failedKeys).length > MAX_FAILED_KEY_SAMPLES) {
        const oldest = Object.keys(failedKeys)[0];
        if (oldest === undefined) break;
        delete failedKeys[oldest];
      }
      return {
        failedKeys,
        failedEntryCount: prev.failedEntryCount + 1,
        log: withLog(
          prev.log,
          "warn",
          `entry failed: ${frame.key} — ${frame.errors.join("; ")}`,
          ts,
        ),
      };
    }
    case "glossary_progress": {
      const failedAttempt = frame.error !== undefined;
      const patch: Partial<WizardStore> = {
        glossaryProgress: {
          done: frame.done,
          total: frame.total,
          newTerms: frame.new_terms,
          retrying: failedAttempt && !frame.skipped ? (frame.attempt ?? 1) : null,
          lastError: failedAttempt ? (frame.error ?? null) : null,
        },
      };
      if (failedAttempt) {
        patch.log = withLog(
          prev.log,
          "warn",
          `glossary chunk ${frame.chunk ?? frame.done + 1}/${frame.total} ` +
            (frame.skipped ? "skipped" : `retry ${frame.attempt}`) +
            `: ${frame.error}`,
          ts,
        );
      }
      return patch;
    }
    case "glossary_extracted":
      return {
        log: withLog(prev.log, "info", `glossary: +${frame.new_terms} terms extracted`, ts),
      };
    case "done":
      return {
        runState: "done",
        runError: null,
        finishedAt: ts,
        activeBatches: {},
        stats: frame.stats ?? null,
        log: withLog(prev.log, "info", "translate done", ts),
      };
    case "failed": {
      const error = frame.error ?? "translate failed";
      return {
        runState: "failed",
        runError: error,
        finishedAt: ts,
        activeBatches: {},
        log: withLog(prev.log, "error", error, ts),
      };
    }
    case "cancelled":
      return {
        runState: "cancelled",
        finishedAt: ts,
        activeBatches: {},
        stats: frame.stats ?? null,
        log: withLog(prev.log, "warn", "translate cancelled; partial result preserved", ts),
      };
  }
}

const initialJobState = {
  scanJobId: null,
  scanState: "idle" as const,
  scanProgress: { current: 0, total: 0, message: "" },
  scanError: null,
  scanResult: null,
  scanTotals: null,
  excludedCategories: [],
  translateJobId: null,
  model: null,
  runState: "idle" as const,
  runError: null,
  startedAt: null,
  translationStartedAt: null,
  finishedAt: null,
  doneEntries: 0,
  fileProgress: {},
  glossaryProgress: null,
  failedKeys: {},
  failedEntryCount: 0,
  promptTokens: 0,
  completionTokens: 0,
  cachedTokens: 0,
  ticker: [],
  activeBatches: {},
  log: [],
  stats: null,
  exportJobId: null,
  exportState: "idle" as const,
  exportZipPath: null,
  exportOverridesZipPath: null,
  exportError: null,
};

export const useWizard = create<WizardStore>((set, get) => ({
  sessionId: null,
  modpackPath: null,
  modpackName: "",
  probe: null,
  sourceLocale: "en_us",
  targetLocale: useSettings.getState().targetLocale,
  migrationEnabled: false,
  previousModpackPath: null,
  previousResourcepackPath: null,
  previousOverridesPath: null,
  ...initialJobState,

  startSession: (path, probe, targetLocale) => {
    closeScanEvents?.();
    closeTranslateEvents?.();
    closeExportEvents?.();
    set({
      sessionId: crypto.randomUUID(),
      modpackPath: path,
      modpackName: probe.name,
      probe,
      targetLocale: targetLocale ?? useSettings.getState().targetLocale,
      migrationEnabled: false,
      previousModpackPath: null,
      previousResourcepackPath: null,
      previousOverridesPath: null,
      ...initialJobState,
    });
    useSettings.getState().rememberFolder(path);
  },

  resumeSession: (sessionId) => {
    // Engine jobs are in-memory: resuming a session from a previous app run
    // restores its metadata so the wizard can re-run from W1/W3.
    const record = useSessions.getState().sessions.find((s) => s.id === sessionId);
    if (record === undefined) return false;
    if (get().sessionId === sessionId) return true; // live in this run
    set({
      sessionId: record.id,
      modpackPath: record.modpackPath,
      modpackName: record.modpackName,
      probe: null,
      sourceLocale: record.sourceLocale,
      targetLocale: record.targetLocale,
      migrationEnabled: false,
      previousModpackPath: null,
      previousResourcepackPath: null,
      previousOverridesPath: null,
      ...initialJobState,
    });
    return true;
  },

  reopenSession: async (sessionId) => {
    const state = get();
    const record = useSessions.getState().sessions.find((s) => s.id === sessionId);
    if (record === undefined || record.status === "failed") return "gone";
    if (
      state.scanState === "running" ||
      state.exportState === "running" ||
      (state.sessionId !== sessionId && state.runState === "running")
    ) {
      return "busy";
    }
    const candidates = [
      record.translateJobId,
      useSessionJobs.getState().jobs[sessionId],
      state.sessionId === sessionId ? state.translateJobId : null,
      sessionId,
    ].filter((value, index, values): value is string =>
      value !== null && value !== undefined && values.indexOf(value) === index,
    );

    const probe = async (jobId: string): Promise<JobSnapshot> => {
      const value = await api.jobSnapshot(jobId);
      if (value.job.type !== "translate") throw new Error("not a translate job");
      // Review/export require an actual PipelineResult, not only metadata.
      if (value.job.status === "done" || value.job.status === "cancelled") {
        await api.entries(jobId, "all", 1, 1);
      }
      return value;
    };

    let jobId: string | null = null;
    let snapshot: JobSnapshot | null = null;
    for (const candidate of candidates) {
      try {
        snapshot = await probe(candidate);
        jobId = candidate;
        break;
      } catch {
        // Try the next live/persisted identity before declaring the row gone.
      }
    }
    if (snapshot === null || jobId === null) {
      try {
        const restored = await api.restoreSession(sessionId);
        snapshot = await probe(restored.id);
        jobId = restored.id;
      } catch {
        snapshot = null;
        jobId = null;
      }
    }
    if (snapshot === null || jobId === null) {
      useSessionJobs.getState().unregister(sessionId);
      useSessions.getState().patch(sessionId, { translateJobId: null });
      if (record.status === "running") {
        const error = "engine no longer holds this translation job";
        useSessions.getState().patch(sessionId, {
          status: "failed",
          finishedAt: Date.now(),
          error,
        });
        if (get().sessionId === sessionId) {
          set({ runState: "failed", runError: error, finishedAt: Date.now() });
          moru.setBusy(false);
        }
      }
      return "gone";
    }

    let scanResult: ScanResult | null = null;
    try {
      scanResult = await api.scanResult(jobId);
    } catch {
      // Older sessions may not carry their W2 payload; compact totals remain.
    }

    closeScanEvents?.();
    closeTranslateEvents?.();
    closeExportEvents?.();

    const engineRunState =
      snapshot.job.status === "pending" ? "running" : snapshot.job.status;
    const startedAt = Date.parse(snapshot.job.created_at);
    let restored: WizardStore = {
      ...get(),
      sessionId: record.id,
      modpackPath: record.modpackPath,
      modpackName: record.modpackName,
      probe: null,
      sourceLocale: record.sourceLocale,
      targetLocale: record.targetLocale,
      migrationEnabled: false,
      previousModpackPath: null,
      previousResourcepackPath: null,
      previousOverridesPath: null,
      ...initialJobState,
      scanState: scanResult !== null || record.scanTotals !== null ? "done" : "idle",
      scanResult,
      scanTotals:
        scanResult !== null
          ? selectedScanTotals({ scanResult, excludedCategories: [] })
          : record.scanTotals,
      translateJobId: jobId,
      model: record.model,
      runState: engineRunState,
      runError: snapshot.job.error,
      startedAt: Number.isFinite(startedAt) ? startedAt : record.createdAt,
      translationStartedAt: snapshot.translation_started_at,
      finishedAt: engineRunState === "running" ? null : record.finishedAt,
      stats: engineRunState === "running" ? null : record.stats,
      exportState: record.exportZipPath !== null ? "done" : "idle",
      exportZipPath: record.exportZipPath,
      exportOverridesZipPath: record.exportOverridesZipPath,
    };
    for (const frame of snapshot.events) {
      restored = { ...restored, ...translationFramePatch(restored, frame) };
    }
    restored.failedEntryCount = Math.max(restored.failedEntryCount, snapshot.failed_count);
    if (Object.keys(restored.fileProgress).length === 0) {
      restored.doneEntries = record.doneEntries;
    }
    set(restored);
    useSessionJobs.getState().register(sessionId, jobId);
    const current = get();
    useSessions.getState().patch(sessionId, {
      translateJobId: jobId,
      status: current.runState === "idle" ? record.status : current.runState,
      finishedAt: current.runState === "running" ? null : current.finishedAt,
      doneEntries: current.doneEntries,
      stats: current.stats,
      error: current.runError,
    });
    moru.setBusy(current.runState === "running");
    if (current.runState === "running") {
      attachTranslationEvents(jobId, sessionId, snapshot.cursor);
    }
    return "ok";
  },

  setTargetLocale: (locale) => {
    set({ targetLocale: locale });
    useSettings.getState().set({ targetLocale: locale });
  },

  setMigrationEnabled: (enabled) => set({ migrationEnabled: enabled }),

  setMigrationInput: (kind, path) =>
    set(
      kind === "modpack"
        ? { previousModpackPath: path }
        : kind === "resourcepack"
          ? { previousResourcepackPath: path }
          : { previousOverridesPath: path },
    ),

  toggleCategory: (name, included) =>
    set((state) => ({
      excludedCategories: included
        ? state.excludedCategories.filter((c) => c !== name)
        : [...new Set([...state.excludedCategories, name])],
    })),

  setCategories: (names, included) =>
    set((state) => ({
      excludedCategories: included
        ? state.excludedCategories.filter((c) => !names.includes(c))
        : [...new Set([...state.excludedCategories, ...names])],
    })),

  startScan: async () => {
    const {
      modpackPath,
      sourceLocale,
      targetLocale,
      migrationEnabled,
      previousModpackPath,
      previousResourcepackPath,
      previousOverridesPath,
    } = get();
    if (modpackPath === null || get().scanState === "running") return;
    closeScanEvents?.();
      set({
        scanState: "running",
        scanError: null,
        scanResult: null,
        scanTotals: null,
        scanProgress: { current: 0, total: 0, message: "" },
    });
    try {
      const job = await api.startScan({
        modpack_path: modpackPath,
        source_locale: sourceLocale,
        target_locale: targetLocale,
        ...(migrationEnabled && previousModpackPath !== null
          ? { previous_modpack_path: previousModpackPath }
          : {}),
        ...(migrationEnabled && previousResourcepackPath !== null
          ? { previous_resourcepack_path: previousResourcepackPath }
          : {}),
        ...(migrationEnabled && previousOverridesPath !== null
          ? { previous_overrides_path: previousOverridesPath }
          : {}),
      });
      set({ scanJobId: job.id });
      closeScanEvents = openJobEvents(job.id, (frame) => {
        if (frame.type === "progress") {
          set({
            scanProgress: {
              current: frame.current ?? frame.done ?? 0,
              total: frame.total ?? 0,
              message: frame.message ?? "",
            },
          });
        } else if (frame.type === "done") {
          void api.scanResult(job.id).then((result) => {
            // Confident launcher-metadata identity beats the folder-name guess.
            const identity = result.identity;
            set({
              scanState: "done",
              scanResult: result,
              scanTotals: selectedScanTotals({
                scanResult: result,
                excludedCategories: [],
              }),
              excludedCategories: [],
              ...(identity?.confident === true && identity.name !== null
                ? { modpackName: identity.name }
                : {}),
            });
          });
        } else if (frame.type === "failed") {
          set({ scanState: "failed", scanError: frame.error ?? "scan failed" });
        } else if (frame.type === "cancelled") {
          set({ scanState: "idle" });
        }
      });
    } catch (error) {
      set({ scanState: "failed", scanError: String(error) });
    }
  },

  startTranslate: async (settingsOverride) => {
    const state = get();
    if (state.modpackPath === null || state.runState === "running") return;
    const settings = settingsOverride ?? snapshotTranslationSettings();
    const scanTotals = selectedScanTotals(state);
    closeTranslateEvents?.();

    const providerId = providerIdOf(settings.model);
    const apiKey = (await moru.secrets.get(`apikey:${providerId}`)) ?? undefined;
    const sessionId = state.sessionId ?? crypto.randomUUID();
    const params: TranslateParams = {
      ...buildTranslateParams(
        { ...state, modpackPath: state.modpackPath },
        settings,
        apiKey,
      ),
      session_id: sessionId,
    };

    set({
      runState: "running",
      model: settings.model,
      runError: null,
      startedAt: Date.now(),
      translationStartedAt: null,
      finishedAt: null,
      doneEntries: 0,
      scanTotals,
      fileProgress: {},
      glossaryProgress: null,
      failedKeys: {},
      failedEntryCount: 0,
      promptTokens: 0,
      completionTokens: 0,
      cachedTokens: 0,
      ticker: [],
      activeBatches: {},
      log: [],
      stats: null,
    });
    get().appendLog("info", `translate start: ${state.modpackName} → ${state.targetLocale}`);
    moru.setBusy(true);

    // Best-effort community pull: fresh approved corrections/terms land in
    // the local TM + user glossary store before the glossary stage reads
    // them. Network failure only logs - the run itself never blocks on it.
    try {
      const sync = await api.syncCommunity(WEB_URL, state.targetLocale, state.sourceLocale);
      const parts: string[] = [];
      if (sync.tm !== null) {
        parts.push(`TM v${sync.tm.version} (${sync.tm.entries})${sync.tm.updated ? " *" : ""}`);
      }
      if (sync.glossary !== null) {
        parts.push(
          `glossary v${sync.glossary.version} (${sync.glossary.terms})${sync.glossary.updated ? " *" : ""}`,
        );
      }
      get().appendLog(
        "info",
        parts.length > 0 ? `community sync: ${parts.join(", ")}` : "community sync: nothing published",
      );
    } catch (error) {
      get().appendLog("warn", `community sync skipped: ${String(error)}`);
    }

    const sessions = useSessions.getState();
    sessions.upsert({
      id: sessionId,
      modpackPath: state.modpackPath,
      modpackName: state.modpackName,
      sourceLocale: state.sourceLocale,
      targetLocale: state.targetLocale,
      model: settings.model,
      status: "running",
      createdAt: Date.now(),
      finishedAt: null,
      doneEntries: 0,
      totalEntries: scanTotals.entries,
      translateJobId: null,
      scanTotals,
      stats: null,
      error: null,
      exportZipPath: null,
      exportOverridesZipPath: null,
      sharedUrl: null,
    });
    set({ sessionId });

    const finish = (
      runState: "done" | "failed" | "cancelled",
      patch: Partial<WizardStore> = {},
    ): void => {
      moru.setBusy(false);
      set({ runState, finishedAt: Date.now(), activeBatches: {}, ...patch });
      const current = get();
      useSessions.getState().patch(sessionId, {
        status: runState,
        finishedAt: Date.now(),
        doneEntries: current.doneEntries,
        stats: current.stats,
        error: current.runError,
      });
    };

    try {
      const job = await api.startTranslate(params);
      set({ translateJobId: job.id });
      useSessionJobs.getState().register(sessionId, job.id);
      useSessions.getState().patch(sessionId, { translateJobId: job.id });
      lastSessionProgressPersistedAt = 0;
      attachTranslationEvents(job.id, sessionId);
    } catch (error) {
      get().appendLog("error", String(error));
      finish("failed", { runError: String(error) });
    }
  },

  handleTranslationFrame: (frame, sessionId) => {
    if (get().sessionId !== sessionId) return;
    set((prev) => translationFramePatch(prev, frame));
    const current = get();
    const terminal =
      frame.type === "done" || frame.type === "failed" || frame.type === "cancelled";
    if (terminal) moru.setBusy(false);

    const now = Date.now();
    if (terminal || (frame.type === "progress" && now - lastSessionProgressPersistedAt >= 1000)) {
      lastSessionProgressPersistedAt = now;
      useSessions.getState().patch(sessionId, {
        ...(current.runState === "idle" ? {} : { status: current.runState }),
        finishedAt: current.finishedAt,
        doneEntries: current.doneEntries,
        stats: current.stats,
        error: current.runError,
      });
    }
  },

  cancelTranslate: async () => {
    const { translateJobId } = get();
    if (translateJobId === null) return;
    await api.cancelJob(translateJobId);
  },

  updateReviewStats: (stats) => {
    set({ stats });
    const sessionId = get().sessionId;
    if (sessionId !== null) {
      useSessions.getState().patch(sessionId, { stats });
    }
  },

  startExport: async () => {
    const { translateJobId, exportState } = get();
    if (translateJobId === null || exportState === "running") return;
    closeExportEvents?.();
    set({ exportState: "running", exportError: null });
    try {
      const job = await api.startExport({ translate_job_id: translateJobId });
      set({ exportJobId: job.id });
      closeExportEvents = openJobEvents(job.id, (frame) => {
        if (frame.type === "done") {
          const zipPath = frame.zip_path ?? null;
          const overridesZipPath = frame.overrides_zip_path ?? null;
          set({
            exportState: "done",
            exportZipPath: zipPath,
            exportOverridesZipPath: overridesZipPath,
          });
          const sessionId = get().sessionId;
          if (sessionId !== null) {
            useSessions.getState().patch(sessionId, {
              exportZipPath: zipPath,
              exportOverridesZipPath: overridesZipPath,
            });
          }
        } else if (frame.type === "failed") {
          set({ exportState: "failed", exportError: frame.error ?? "export failed" });
        }
      });
    } catch (error) {
      set({ exportState: "failed", exportError: String(error) });
    }
  },

  appendLog: (level, text) =>
    set((prev) => ({
      log: [...prev.log.slice(-MAX_LOG_LINES + 1), { ts: Date.now(), level, text }],
    })),

  reset: () => {
    closeScanEvents?.();
    closeTranslateEvents?.();
    closeExportEvents?.();
    moru.setBusy(false);
    set({
      sessionId: null,
      modpackPath: null,
      modpackName: "",
      probe: null,
      migrationEnabled: false,
      previousModpackPath: null,
      previousResourcepackPath: null,
      previousOverridesPath: null,
      ...initialJobState,
    });
  },
}));
