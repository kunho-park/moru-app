/** Persisted UI/translation preferences (renderer-local). */

import { create } from "zustand";
import { persist } from "zustand/middleware";

/**
 * Mods excluded from translation out of the box: libraries, optimization
 * mods and author tooling, whose strings only the modpack author ever
 * reads. Entries are **declared mod ids** (`fabric.mod.json:id`,
 * `mods.toml:modId`), the identifier that namespaces a mod's assets and
 * therefore every translation key it owns — jar file names and display
 * names both vary per build and per launcher. Every id below was read out
 * of that mod's real published jar.
 *
 * The user owns this list: it is persisted, editable on W2, and matching
 * ignores case and punctuation, so typing a display name ("Entity
 * Culling") still hits the id ("entityculling").
 */
export const DEFAULT_MOD_BLACKLIST: readonly string[] = [
  "jade", // Jade — in-world tooltip HUD
  "modernfix", // ModernFix — startup/memory optimization
  "craftpresence", // CraftPresence — Discord rich presence
  "kiwi", // Kiwi (Library) — Snownee's shared code
  "clumps", // Clumps — XP orb merging
  "sodium", // Sodium — rendering engine
  "entityculling", // Entity Culling — render culling
  "fancymenu", // FancyMenu — the author's own menu editor
  "chunky", // Chunky — chunk pregeneration command
  "bookshelf", // Bookshelf — Darkhax's shared code
];

export type PresetId = "fast" | "balanced" | "best";

interface SettingsStore {
  uiLanguage: "ko" | "en";
  theme: "dark" | "light";
  outputDir: string | null;
  /** last-used translate settings, restored as W3 defaults */
  preset: PresetId | "custom";
  /** selected LLM provider id (W3 provider band); model belongs to it */
  provider: string;
  model: string;
  temperature: number;
  batchSize: number;
  maxConcurrent: number;
  maxRefine: number;
  /** Send litellm reasoning_effort so reasoning-capable models think. */
  thinkingEnabled: boolean;
  /** Effort level sent when thinking is enabled. */
  thinkingEffort: "low" | "medium" | "high";
  useTm: boolean;
  useVanillaGlossary: boolean;
  extractGlossary: boolean;
  /** Maximum mined candidates sent to glossary curation; null means unlimited. */
  glossaryMaxTerms: number | null;
  ollamaBaseUrl: string;
  /** OpenAI-compatible server (LM Studio, llama.cpp, vLLM) base URL incl. /v1 */
  openaiCompatBaseUrl: string;
  targetLocale: string;
  recentFolders: string[];
  /** Mod ids never translated; seeded from DEFAULT_MOD_BLACKLIST. */
  modBlacklist: string[];

  set: (patch: Partial<SettingsStore>) => void;
  rememberFolder: (path: string) => void;
}

export const useSettings = create<SettingsStore>()(
  persist(
    (set) => ({
      uiLanguage: "ko",
      theme: "dark",
      outputDir: null,
      preset: "balanced",
      provider: "anthropic",
      model: "anthropic/claude-haiku-4-5",
      temperature: 0.3,
      batchSize: 30,
      maxConcurrent: 15,
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
      recentFolders: [],
      modBlacklist: [...DEFAULT_MOD_BLACKLIST],

      set: (patch) => set(patch),
      rememberFolder: (path) =>
        set((state) => ({
          recentFolders: [path, ...state.recentFolders.filter((p) => p !== path)].slice(0, 8),
        })),
    }),
    { name: "moru-settings" },
  ),
);
