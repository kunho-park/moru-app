/**
 * i18n: ko (default) / en. Every user-facing string goes through t().
 * Resources live in per-screen JSON files under ko/ and
 * en/ - the filename becomes the key prefix ("home.json" -> t("home.*")),
 * so screens can be added without touching shared files.
 */

import i18next from "i18next";
import { initReactI18next } from "react-i18next";

import { useSettings } from "../stores/settings";

type ResourceModule = Record<string, unknown>;

function collect(modules: Record<string, ResourceModule>): Record<string, unknown> {
  const merged: Record<string, unknown> = {};
  for (const [path, module] of Object.entries(modules)) {
    const name = path.split("/").at(-1)?.replace(".json", "") ?? path;
    merged[name] = module.default ?? module;
  }
  return merged;
}

const koModules = import.meta.glob<ResourceModule>("./ko/*.json", { eager: true });
const enModules = import.meta.glob<ResourceModule>("./en/*.json", { eager: true });

export function initI18n(): void {
  void i18next.use(initReactI18next).init({
    lng: useSettings.getState().uiLanguage,
    fallbackLng: "ko",
    interpolation: { escapeValue: false },
    resources: {
      ko: { translation: collect(koModules) },
      en: { translation: collect(enModules) },
    },
  });

  useSettings.subscribe((state, prev) => {
    if (state.uiLanguage !== prev.uiLanguage) {
      void i18next.changeLanguage(state.uiLanguage);
    }
  });
}
