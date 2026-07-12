import { afterAll, expect, test } from "bun:test";

const originalWindow = globalThis.window;
Object.defineProperty(globalThis, "window", {
  configurable: true,
  value: {
    localStorage: { getItem: () => null, setItem: () => undefined },
    moru: {},
  },
});

const { estimateUsage } = await import("./models.ts?usage-estimate-test");
const { costUsd, priceForModel } = await import("./pricing.ts?usage-estimate-test");

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

const BASE_INPUT = {
  chars: 350_000,
  entries: 3_000,
  batchSize: 30,
  glossary: true,
  glossaryMaxTerms: 3_000,
};

test("adds every baseline glossary-curation chunk to the usage estimate", () => {
  const translationOnly = estimateUsage({ ...BASE_INPUT, extractGlossary: false });
  const withExtraction = estimateUsage({ ...BASE_INPUT, extractGlossary: true });

  // 3,000 candidates / 50 per chunk: 60 schema-bearing prompts.
  expect(withExtraction.promptTokens - translationOnly.promptTokens).toBe(270_000);
  expect(withExtraction.completionTokens - translationOnly.completionTokens).toBe(225_000);
  expect(withExtraction.totalTokens - translationOnly.totalTokens).toBe(495_000);
});

test("caps glossary cost at the configured candidate budget", () => {
  const estimate = estimateUsage({
    ...BASE_INPUT,
    extractGlossary: true,
    glossaryMaxTerms: 100,
  });
  const baseline = estimateUsage({ ...BASE_INPUT, extractGlossary: false });

  expect(estimate.promptTokens - baseline.promptTokens).toBe(9_000);
  expect(estimate.completionTokens - baseline.completionTokens).toBe(7_500);
});

test("uses lower known direct-provider rates but preserves OpenRouter pricing", () => {
  const live = { input: 0.2, output: 1.3, cacheRead: 0.1 };
  const table = new Map([["deepseek/deepseek-chat", live]]);

  const direct = priceForModel(table, "deepseek/deepseek-chat");
  expect(direct).toEqual({
    input: 0.2,
    output: 1.1,
    cacheRead: 0.07,
    source: "conservative",
  });
  expect(costUsd({ promptTokens: 1_000_000, completionTokens: 1_000_000 }, direct)).toBe(
    1.3,
  );

  expect(priceForModel(table, "openrouter/deepseek/deepseek-chat")).toEqual({
    ...live,
    source: "openrouter",
  });
});
