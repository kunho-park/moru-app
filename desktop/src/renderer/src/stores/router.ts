/** Screen router - global navigation state for hub screens and the wizard. */

import { create } from "zustand";

export type Screen =
  | "onboarding"
  | "home"
  | "w1"
  | "w2"
  | "w3"
  | "w4"
  | "w5"
  | "w6"
  | "history"
  | "glossary"
  | "settings";

export const WIZARD_SCREENS: readonly Screen[] = ["w1", "w2", "w3", "w4", "w5", "w6"];

/** localStorage flag: present once the first-run onboarding was finished or skipped. */
const ONBOARDED_KEY = "moru:onboarded";

interface RouterStore {
  screen: Screen;
  /** First-run onboarding done; App renders the fullscreen wizard while false. */
  onboarded: boolean;
  go: (screen: Screen) => void;
  /** Sets the persistent flag and lands on home (finish and skip both call this). */
  completeOnboarding: () => void;
  /** Clears the flag and re-enters the wizard (Settings > General > replay). */
  replayOnboarding: () => void;
}

export const useRouter = create<RouterStore>((set) => ({
  screen: "home",
  onboarded: window.localStorage.getItem(ONBOARDED_KEY) !== null,
  go: (screen) => set({ screen }),
  completeOnboarding: () => {
    window.localStorage.setItem(ONBOARDED_KEY, "1");
    set({ onboarded: true, screen: "home" });
  },
  replayOnboarding: () => {
    window.localStorage.removeItem(ONBOARDED_KEY);
    set({ onboarded: false, screen: "onboarding" });
  },
}));
