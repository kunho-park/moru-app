import { afterAll, describe, expect, test } from "bun:test";

const originalWindow = globalThis.window;
Object.defineProperty(globalThis, "window", {
  configurable: true,
  value: {
    localStorage: { getItem: () => null },
    moru: {},
  },
});

const { resolveWebUrl, WEB_URL } = await import("./web.ts?web-url-test");

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

describe("desktop web API origin", () => {
  test("uses the canonical www host by default", () => {
    expect(WEB_URL).toBe("https://www.moru.gg");
    expect(resolveWebUrl(null)).toBe("https://www.moru.gg");
  });

  test("migrates legacy apex overrides before bearer requests", () => {
    expect(resolveWebUrl("https://moru.gg")).toBe("https://www.moru.gg");
    expect(resolveWebUrl("https://moru.gg/")).toBe("https://www.moru.gg");
  });

  test("keeps development overrides and removes one trailing slash", () => {
    expect(resolveWebUrl("http://localhost:3199/")).toBe(
      "http://localhost:3199",
    );
  });
});
