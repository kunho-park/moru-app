import { expect, test } from "bun:test";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { createInstance } from "i18next";
import { I18nextProvider } from "react-i18next";

import { ActiveBatchPanel } from "./ActiveBatchPanel.tsx";

const i18n = createInstance();
await i18n.init({
  lng: "en",
  interpolation: { escapeValue: false },
  resources: {
    en: {
      translation: {
        w4: {
          concurrent: {
            title: "Concurrent translation requests",
            slots: "{{active}} / {{limit}} slots",
            entries: "{{count}} entries",
            waiting: "Waiting",
            glossary: "Glossary",
          },
        },
      },
    },
  },
});

test("renders every active provider request with slot usage", () => {
  const html = renderToStaticMarkup(
    createElement(
      I18nextProvider,
      { i18n },
      createElement(ActiveBatchPanel, {
        batches: [
          {
            requestId: 11,
            file: "/pack/kubejs/assets/example/lang/en_us.json",
            key: "block.example",
            entries: 40,
            startedAt: 9_000,
          },
          {
            requestId: 12,
            file: "/pack/config/quests.snbt",
            key: "chapter.quests",
            entries: 18,
            startedAt: 9_500,
          },
        ],
        limit: 15,
        now: 12_000,
        glossaryActive: false,
      }),
    ),
  );

  expect(html).toContain("2 / 15 slots");
  expect(html).toContain("REQ 11");
  expect(html).toContain("en_us.json");
  expect(html).toContain("block.example");
  expect(html).toContain("40 entries");
  expect(html).toContain("REQ 12");
  expect(html).toContain("quests.snbt");
});
