/** Persisted UI/translation preferences (renderer-local). */

import { create } from "zustand";
import { persist } from "zustand/middleware";

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
  useTm: boolean;
  useVanillaGlossary: boolean;
  extractGlossary: boolean;
  /** Maximum mined candidates sent to glossary curation; null means unlimited. */
  glossaryMaxTerms: number | null;
  ollamaBaseUrl: string;
  targetLocale: string;
  recentFolders: string[];

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
      useTm: true,
      useVanillaGlossary: true,
      extractGlossary: true,
      glossaryMaxTerms: 3000,
      ollamaBaseUrl: "http://localhost:11434",
      targetLocale: "ko_kr",
      recentFolders: [],

      set: (patch) => set(patch),
      rememberFolder: (path) =>
        set((state) => ({
          recentFolders: [path, ...state.recentFolders.filter((p) => p !== path)].slice(0, 8),
        })),
    }),
    { name: "moru-settings" },
  ),
);
