import { afterAll, expect, test } from "bun:test";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { createInstance } from "i18next";
import { I18nextProvider } from "react-i18next";

const originalWindow = globalThis.window;
Object.defineProperty(globalThis, "window", {
  configurable: true,
  value: {
    moru: {
      updates: {
        check: async () => undefined,
        install: () => undefined,
        getState: async () => ({ status: "none" }),
        onState: () => () => undefined,
      },
    },
  },
});

const { UpdateGate, isUpdateBlocking } = await import(
  "./UpdateGate.tsx?update-gate-test"
);

afterAll(() => {
  if (originalWindow === undefined) {
    delete globalThis.window;
  } else {
    Object.defineProperty(globalThis, "window", {
      configurable: true,
      value: originalWindow,
    });
  }
});

const i18n = createInstance();
await i18n.init({
  lng: "en",
  interpolation: { escapeValue: false },
  resources: {
    en: {
      translation: {
        common: {
          update: {
            forceTitle: "Update required",
            forceDesc: "Version v{{version}} is required.",
            preparing: "Preparing download…",
            downloading: "Downloading {{percent}}%",
            installRestart: "Restart & update",
          },
        },
      },
    },
  },
});

test("only known-newer updater states lock the app", () => {
  expect(isUpdateBlocking({ status: "available", version: "9.9.9" })).toBeTrue();
  expect(
    isUpdateBlocking({ status: "downloading", percent: 40, version: "9.9.9" }),
  ).toBeTrue();
  expect(isUpdateBlocking({ status: "ready", version: "9.9.9" })).toBeTrue();

  // Unknown states pass through: offline / dev builds must stay usable.
  expect(isUpdateBlocking({ status: "idle" })).toBeFalse();
  expect(isUpdateBlocking({ status: "checking" })).toBeFalse();
  expect(isUpdateBlocking({ status: "none" })).toBeFalse();
  expect(isUpdateBlocking({ status: "error", message: "offline" })).toBeFalse();
});

test("renders children until an update is known to exist", () => {
  const html = renderToStaticMarkup(
    createElement(
      I18nextProvider,
      { i18n },
      createElement(UpdateGate, null, createElement("div", null, "app-content")),
    ),
  );
  expect(html).toContain("app-content");
  expect(html).not.toContain("Update required");
});
