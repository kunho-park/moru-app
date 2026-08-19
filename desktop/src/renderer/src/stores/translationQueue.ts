/**
 * Durable, renderer-side queue for unattended multi-pack translation.
 *
 * The queue deliberately orchestrates the existing wizard store instead of
 * introducing a second engine pipeline. One item owns the wizard at a time;
 * completed jobs remain ordinary History sessions and use the existing W5/W6
 * review/export flow.
 */

import { create } from "zustand";
import { persist } from "zustand/middleware";

import type { DesktopPlatform, ModpackProbe } from "../../../shared/bridge";
import { moru } from "../lib/bridge";
import { useSessions } from "./sessions";
import {
  snapshotTranslationSettings,
  type TranslationRunSettings,
} from "./settings";
import { useWizard } from "./wizard";

export type TranslationQueueItemStatus =
  | "pending"
  | "scanning"
  | "translating"
  | "done"
  | "failed"
  | "cancelled";

export type TranslationQueuePhase =
  | "idle"
  | "running"
  | "pausing"
  | "paused"
  | "complete";

export interface TranslationQueueItem {
  id: string;
  path: string;
  name: string;
  status: TranslationQueueItemStatus;
  sessionId: string | null;
  error: string | null;
  addedAt: number;
  startedAt: number | null;
  finishedAt: number | null;
}

export interface TranslationQueueInput {
  path: string;
  name: string;
}

interface TranslationQueueStore {
  items: TranslationQueueItem[];
  phase: TranslationQueuePhase;
  settingsSnapshot: TranslationRunSettings | null;
  lastError: string | null;
  /** False only while an interrupted persisted item is being reconciled. */
  ready: boolean;
  add: (inputs: TranslationQueueInput[]) => void;
  remove: (id: string) => void;
  move: (id: string, direction: -1 | 1) => void;
  retry: (id: string) => void;
  clear: () => void;
  patchItem: (id: string, patch: Partial<TranslationQueueItem>) => void;
}

interface PersistedTranslationQueueState {
  items: TranslationQueueItem[];
  phase: TranslationQueuePhase;
  settingsSnapshot: TranslationRunSettings | null;
  lastError: string | null;
}

const INTERRUPTED_SCAN_ERROR = "앱이 종료되어 모드팩 스캔이 중단되었습니다.";
const LOST_TRANSLATION_ERROR = "앱 재시작 후 번역 작업을 복원할 수 없습니다.";

export function queuePathKey(path: string, platform: DesktopPlatform): string {
  const normalized = path.trim().replace(/[\\/]+$/, "").replaceAll("\\", "/");
  return platform === "win32" ? normalized.toLocaleLowerCase("en-US") : normalized;
}

export function appendUniqueQueueItems(
  existing: TranslationQueueItem[],
  inputs: TranslationQueueInput[],
  platform: DesktopPlatform,
  makeId: () => string = () => crypto.randomUUID(),
  now: () => number = () => Date.now(),
): TranslationQueueItem[] {
  const keys = new Set(existing.map((item) => queuePathKey(item.path, platform)));
  const added: TranslationQueueItem[] = [];
  for (const input of inputs) {
    const key = queuePathKey(input.path, platform);
    if (key.length === 0 || keys.has(key)) continue;
    keys.add(key);
    added.push({
      id: makeId(),
      path: input.path,
      name: input.name,
      status: "pending",
      sessionId: null,
      error: null,
      addedAt: now(),
      startedAt: null,
      finishedAt: null,
    });
  }
  return [...existing, ...added];
}

export function movePendingQueueItem(
  items: TranslationQueueItem[],
  id: string,
  direction: -1 | 1,
): TranslationQueueItem[] {
  const index = items.findIndex((item) => item.id === id && item.status === "pending");
  if (index < 0) return items;
  let swapIndex = index + direction;
  while (swapIndex >= 0 && swapIndex < items.length && items[swapIndex].status !== "pending") {
    swapIndex += direction;
  }
  if (swapIndex < 0 || swapIndex >= items.length) return items;
  const next = [...items];
  [next[index], next[swapIndex]] = [next[swapIndex], next[index]];
  return next;
}

export function retryQueueItem(
  item: TranslationQueueItem,
): TranslationQueueItem {
  if (item.status !== "failed" && item.status !== "cancelled") return item;
  return {
    ...item,
    status: "pending",
    sessionId: null,
    error: null,
    startedAt: null,
    finishedAt: null,
  };
}

function validPhase(value: unknown): value is TranslationQueuePhase {
  return ["idle", "running", "pausing", "paused", "complete"].includes(String(value));
}

function validStatus(value: unknown): value is TranslationQueueItemStatus {
  return ["pending", "scanning", "translating", "done", "failed", "cancelled"].includes(
    String(value),
  );
}

/** Normalize localStorage data and never resume paid work automatically. */
export function normalizePersistedQueueState(
  persisted: unknown,
): PersistedTranslationQueueState {
  const value = (persisted ?? {}) as Partial<PersistedTranslationQueueState>;
  const items = Array.isArray(value.items)
    ? value.items
        .filter(
          (item): item is TranslationQueueItem =>
            item !== null &&
            typeof item === "object" &&
            typeof item.id === "string" &&
            typeof item.path === "string" &&
            validStatus(item.status),
        )
        .map((item) => ({
          ...item,
          name: typeof item.name === "string" && item.name.length > 0 ? item.name : item.path,
          sessionId: typeof item.sessionId === "string" ? item.sessionId : null,
          error: typeof item.error === "string" ? item.error : null,
          addedAt: Number.isFinite(item.addedAt) ? item.addedAt : Date.now(),
          startedAt: Number.isFinite(item.startedAt) ? item.startedAt : null,
          finishedAt: Number.isFinite(item.finishedAt) ? item.finishedAt : null,
        }))
    : [];
  const storedPhase = validPhase(value.phase) ? value.phase : "idle";
  return {
    items,
    phase: storedPhase === "running" || storedPhase === "pausing" ? "paused" : storedPhase,
    settingsSnapshot: value.settingsSnapshot ?? null,
    lastError: typeof value.lastError === "string" ? value.lastError : null,
  };
}

export const useTranslationQueue = create<TranslationQueueStore>()(
  persist(
    (set, get) => ({
      items: [],
      phase: "idle",
      settingsSnapshot: null,
      lastError: null,
      ready: false,
      add: (inputs) =>
        set((state) => ({
          items: appendUniqueQueueItems(state.items, inputs, moru.platform),
          phase: state.phase === "complete" ? "idle" : state.phase,
          lastError: null,
        })),
      remove: (id) =>
        set((state) => ({
          items: state.items.filter(
            (item) => item.id !== id || item.status !== "pending",
          ),
        })),
      move: (id, direction) =>
        set((state) => ({ items: movePendingQueueItem(state.items, id, direction) })),
      retry: (id) =>
        set((state) => ({
          items: state.items.map((item) => (item.id === id ? retryQueueItem(item) : item)),
          phase: state.phase === "complete" ? "idle" : state.phase,
          lastError: null,
        })),
      clear: () => {
        if (get().phase === "running" || get().phase === "pausing") return;
        set({ items: [], phase: "idle", settingsSnapshot: null, lastError: null });
      },
      patchItem: (id, patch) =>
        set((state) => ({
          items: state.items.map((item) => (item.id === id ? { ...item, ...patch } : item)),
        })),
    }),
    {
      name: "moru-translation-queue",
      version: 1,
      partialize: (state) => ({
        items: state.items,
        phase: state.phase,
        settingsSnapshot: state.settingsSnapshot,
        lastError: state.lastError,
      }),
      merge: (persisted, current) => ({
        ...current,
        ...normalizePersistedQueueState(persisted),
      }),
    },
  ),
);

export interface QueueItemOutcome {
  status: "done" | "failed" | "cancelled";
  sessionId?: string | null;
  name?: string;
  error?: string | null;
}

export interface TranslationQueueDriver {
  getPhase: () => TranslationQueuePhase;
  getNext: () => TranslationQueueItem | undefined;
  run: (item: TranslationQueueItem) => Promise<QueueItemOutcome>;
  settle: (item: TranslationQueueItem, outcome: QueueItemOutcome) => void;
  setPhase: (phase: TranslationQueuePhase) => void;
}

/** Generic drain loop kept dependency-injectable for deterministic tests. */
export async function drainTranslationQueue(driver: TranslationQueueDriver): Promise<void> {
  while (true) {
    const phase = driver.getPhase();
    if (phase === "pausing") {
      driver.setPhase("paused");
      return;
    }
    if (phase !== "running") return;
    const item = driver.getNext();
    if (item === undefined) {
      driver.setPhase("complete");
      return;
    }
    let outcome: QueueItemOutcome;
    try {
      outcome = await driver.run(item);
    } catch (error) {
      outcome = { status: "failed", error: String(error) };
    }
    driver.settle(item, outcome);
    if (driver.getPhase() === "pausing") {
      driver.setPhase("paused");
      return;
    }
  }
}

type WizardState = ReturnType<typeof useWizard.getState>;

function waitForWizard(
  terminal: (state: WizardState) => boolean,
): Promise<WizardState> {
  const current = useWizard.getState();
  if (terminal(current)) return Promise.resolve(current);
  return new Promise((resolve) => {
    const unsubscribe = useWizard.subscribe((state) => {
      if (!terminal(state)) return;
      unsubscribe();
      resolve(state);
    });
  });
}

function probeError(path: string, probe: ModpackProbe): string | null {
  if (!probe.exists) return `모드팩 폴더를 찾을 수 없습니다: ${path}`;
  if (!probe.isDirectory) return `폴더가 아닙니다: ${path}`;
  if (!probe.hasMods) return `mods 폴더가 없는 모드팩입니다: ${path}`;
  return null;
}

function ensureFailedSession(
  item: TranslationQueueItem,
  sessionId: string | null,
  settings: TranslationRunSettings,
  error: string,
): string {
  const wizard = useWizard.getState();
  const id = sessionId ?? wizard.sessionId ?? crypto.randomUUID();
  const sessions = useSessions.getState();
  const existing = sessions.sessions.find((session) => session.id === id);
  if (existing !== undefined) {
    sessions.patch(id, {
      status: "failed",
      finishedAt: Date.now(),
      error,
    });
    return id;
  }
  const totals = wizard.scanTotals;
  sessions.upsert({
    id,
    modpackPath: item.path,
    modpackName: wizard.modpackName || item.name,
    sourceLocale: wizard.sourceLocale || "en_us",
    targetLocale: wizard.targetLocale || settings.targetLocale,
    model: settings.model,
    status: "failed",
    createdAt: item.startedAt ?? Date.now(),
    finishedAt: Date.now(),
    doneEntries: 0,
    totalEntries: totals?.entries ?? 0,
    translateJobId: null,
    scanTotals: totals,
    stats: null,
    error,
    exportZipPath: null,
    exportOverridesZipPath: null,
    sharedUrl: null,
  });
  return id;
}

async function runQueueItem(
  item: TranslationQueueItem,
  settings: TranslationRunSettings,
): Promise<QueueItemOutcome> {
  const queue = useTranslationQueue.getState();
  queue.patchItem(item.id, {
    status: "scanning",
    error: null,
    startedAt: Date.now(),
    finishedAt: null,
  });

  let sessionId: string | null = null;
  try {
    const probe = await moru.probeModpack(item.path);
    const invalid = probeError(item.path, probe);
    if (invalid !== null) {
      sessionId = ensureFailedSession(item, null, settings, invalid);
      return { status: "failed", sessionId, error: invalid };
    }

    useWizard.getState().reset();
    useWizard.getState().startSession(item.path, probe, settings.targetLocale);
    sessionId = useWizard.getState().sessionId;
    queue.patchItem(item.id, { sessionId, name: probe.name });

    await useWizard.getState().startScan();
    const scanned = await waitForWizard((state) => state.scanState !== "running");
    if (scanned.scanState !== "done") {
      const error = scanned.scanError ?? "모드팩 스캔에 실패했습니다.";
      sessionId = ensureFailedSession(item, sessionId, settings, error);
      return { status: "failed", sessionId, name: scanned.modpackName, error };
    }

    queue.patchItem(item.id, { status: "translating", name: scanned.modpackName });
    await useWizard.getState().startTranslate(settings);
    const translated = await waitForWizard(
      (state) =>
        state.runState === "done" ||
        state.runState === "failed" ||
        state.runState === "cancelled",
    );
    const status =
      translated.runState === "done" ||
      translated.runState === "failed" ||
      translated.runState === "cancelled"
        ? translated.runState
        : "failed";
    return {
      status,
      sessionId: translated.sessionId,
      name: translated.modpackName,
      error: translated.runError,
    };
  } catch (error) {
    const message = String(error);
    sessionId = ensureFailedSession(item, sessionId, settings, message);
    return { status: "failed", sessionId, error: message };
  }
}

function wizardBusy(): boolean {
  const state = useWizard.getState();
  return (
    state.scanState === "running" ||
    state.runState === "running" ||
    state.exportState === "running"
  );
}

let runnerPromise: Promise<void> | null = null;

export function startTranslationQueue(): void {
  const queue = useTranslationQueue.getState();
  if (!queue.ready || runnerPromise !== null) return;
  if (wizardBusy()) {
    useTranslationQueue.setState({
      phase: "paused",
      lastError: "다른 번역 작업이 진행 중입니다. 완료하거나 중단한 뒤 대기열을 시작하세요.",
    });
    return;
  }
  if (!queue.items.some((item) => item.status === "pending")) return;

  const settings =
    queue.phase === "paused" && queue.settingsSnapshot !== null
      ? queue.settingsSnapshot
      : snapshotTranslationSettings();
  useTranslationQueue.setState({
    phase: "running",
    settingsSnapshot: settings,
    lastError: null,
  });

  runnerPromise = drainTranslationQueue({
    getPhase: () => useTranslationQueue.getState().phase,
    getNext: () =>
      useTranslationQueue.getState().items.find((item) => item.status === "pending"),
    run: (item) => runQueueItem(item, settings),
    settle: (item, outcome) => {
      useTranslationQueue.getState().patchItem(item.id, {
        status: outcome.status,
        sessionId: outcome.sessionId ?? item.sessionId,
        name: outcome.name ?? item.name,
        error: outcome.error ?? null,
        finishedAt: Date.now(),
      });
    },
    setPhase: (phase) => useTranslationQueue.setState({ phase }),
  }).finally(() => {
    runnerPromise = null;
    if (useTranslationQueue.getState().phase === "running") {
      useTranslationQueue.setState({ phase: "paused" });
    }
  });
}

export function pauseTranslationQueue(): void {
  const phase = useTranslationQueue.getState().phase;
  if (phase === "running") useTranslationQueue.setState({ phase: "pausing" });
}

let initializationPromise: Promise<void> | null = null;

/** Reattach a live translate job after renderer reload, but never advance it. */
export function initializeTranslationQueue(): Promise<void> {
  if (initializationPromise !== null) return initializationPromise;
  initializationPromise = (async () => {
    const queue = useTranslationQueue.getState();
    const settings = queue.settingsSnapshot ?? snapshotTranslationSettings();
    const activeItems = queue.items.filter(
      (item) => item.status === "scanning" || item.status === "translating",
    );

    for (const item of activeItems) {
      if (item.status === "scanning") {
        const sessionId = ensureFailedSession(item, item.sessionId, settings, INTERRUPTED_SCAN_ERROR);
        useTranslationQueue.getState().patchItem(item.id, {
          status: "failed",
          sessionId,
          error: INTERRUPTED_SCAN_ERROR,
          finishedAt: Date.now(),
        });
        continue;
      }

      if (item.sessionId === null) {
        const sessionId = ensureFailedSession(item, null, settings, LOST_TRANSLATION_ERROR);
        useTranslationQueue.getState().patchItem(item.id, {
          status: "failed",
          sessionId,
          error: LOST_TRANSLATION_ERROR,
          finishedAt: Date.now(),
        });
        continue;
      }

      const reopened = await useWizard.getState().reopenSession(item.sessionId);
      if (reopened !== "ok") {
        const session = useSessions
          .getState()
          .sessions.find((record) => record.id === item.sessionId);
        useTranslationQueue.getState().patchItem(item.id, {
          status: "failed",
          error: session?.error ?? LOST_TRANSLATION_ERROR,
          finishedAt: Date.now(),
        });
        continue;
      }

      const restored =
        useWizard.getState().runState === "running"
          ? await waitForWizard((state) => state.runState !== "running")
          : useWizard.getState();
      const status =
        restored.runState === "done" ||
        restored.runState === "failed" ||
        restored.runState === "cancelled"
          ? restored.runState
          : "failed";
      useTranslationQueue.getState().patchItem(item.id, {
        status,
        name: restored.modpackName || item.name,
        error: restored.runError,
        finishedAt: Date.now(),
      });
    }

    const remaining = useTranslationQueue
      .getState()
      .items.some((item) => item.status === "pending");
    useTranslationQueue.setState({
      phase: remaining ? "paused" : queue.items.length > 0 ? "complete" : "idle",
      ready: true,
    });
  })().catch((error) => {
    useTranslationQueue.setState({
      phase: "paused",
      ready: true,
      lastError: `대기열 복원 중 오류가 발생했습니다: ${String(error)}`,
    });
  });
  return initializationPromise;
}

export function translationQueueIsActive(): boolean {
  const state = useTranslationQueue.getState();
  return (
    state.phase === "running" ||
    state.phase === "pausing" ||
    state.items.some((item) => item.status === "scanning" || item.status === "translating")
  );
}
