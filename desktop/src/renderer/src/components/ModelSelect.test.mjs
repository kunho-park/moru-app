import { expect, test } from "bun:test";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { createInstance } from "i18next";
import { I18nextProvider } from "react-i18next";
import { ModelSelect, modelSearchResults } from "./ModelSelect.tsx";

const CATALOG = [
  "openrouter/anthropic/claude-haiku-4-5",
  "openrouter/deepseek/deepseek-chat-v3-0324",
  "openrouter/google/gemini-2.5-flash",
  "gpt-5-mini",
];

test("empty or whitespace query returns the whole catalog", () => {
  expect(modelSearchResults(CATALOG, CATALOG[0], "")).toEqual(CATALOG);
  expect(modelSearchResults(CATALOG, CATALOG[0], "   ")).toEqual(CATALOG);
});

test("a persisted model missing from the catalog stays pickable, once", () => {
  const withGhost = modelSearchResults(CATALOG, "openrouter/retired/old-model", "");
  expect(withGhost[0]).toBe("openrouter/retired/old-model");
  expect(withGhost.length).toBe(CATALOG.length + 1);

  // present values are not duplicated
  expect(modelSearchResults(CATALOG, CATALOG[2], "").length).toBe(CATALOG.length);
});

test("matches the raw model id case-insensitively", () => {
  expect(modelSearchResults(CATALOG, CATALOG[0], "DEEPSEEK")).toEqual([
    "openrouter/deepseek/deepseek-chat-v3-0324",
  ]);
  expect(modelSearchResults(CATALOG, CATALOG[0], "openrouter/google")).toEqual([
    "openrouter/google/gemini-2.5-flash",
  ]);
});

test("matches the display name where the raw id would miss", () => {
  // display name "Claude Haiku 4.5" - the id only contains "4-5"
  expect(modelSearchResults(CATALOG, CATALOG[0], "haiku 4.5")).toEqual([
    "openrouter/anthropic/claude-haiku-4-5",
  ]);
  // "GPT" via the display-name prettifier of "gpt-5-mini"
  expect(modelSearchResults(CATALOG, CATALOG[0], "GPT 5")).toEqual(["gpt-5-mini"]);
});

test("returns nothing when nothing matches", () => {
  expect(modelSearchResults(CATALOG, CATALOG[0], "zzz-no-such-model")).toEqual([]);
});

const i18n = createInstance();
await i18n.init({
  lng: "en",
  interpolation: { escapeValue: false },
  resources: {
    en: {
      translation: {
        w3: { advanced: { modelSearchPlaceholder: "Search models… (name or ID)" } },
      },
    },
  },
});

test("closed combobox shows the current model's display name, no dropdown", () => {
  const html = renderToStaticMarkup(
    createElement(
      I18nextProvider,
      { i18n },
      createElement(ModelSelect, {
        value: "openrouter/anthropic/claude-haiku-4-5",
        options: CATALOG,
        onSelect: () => {},
        labelId: "model-label",
      }),
    ),
  );
  expect(html).toContain("Claude Haiku 4.5");
  expect(html).toContain('aria-haspopup="listbox"');
  expect(html).toContain('aria-labelledby="model-label"');
  expect(html).not.toContain("Search models");
});
