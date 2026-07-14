import { afterAll, expect, test } from "bun:test";

const originalWindow = globalThis.window;
Object.defineProperty(globalThis, "window", {
  configurable: true,
  value: {
    localStorage: { getItem: () => null, setItem: () => undefined },
    moru: {},
  },
});

const { RECOMMENDED_MODEL, estimateUsage, healedModel, PROVIDER_TIERS } = await import(
  "./models.ts?usage-estimate-test"
);
const { costUsd, estimatePriceForModel, priceForModel } = await import(
  "./pricing.ts?usage-estimate-test"
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

const BASE_INPUT = {
  chars: 350_000,
  entries: 3_000,
  batchSize: 30,
  glossary: true,
  glossaryMaxTerms: 3_000,
};

/*
 * Conservative factors (defaults): translation retry 1 + 0.1*maxRefine(2)
 * = 1.2, glossary retry 1 + 0.1*ENGINE_GLOSSARY_MAX_RETRIES(2) = 1.2,
 * safety margin 1.4 — every glossary token therefore lands at x1.68.
 */

test("adds every baseline glossary-curation chunk to the usage estimate", () => {
  const translationOnly = estimateUsage({ ...BASE_INPUT, extractGlossary: false });
  const withExtraction = estimateUsage({ ...BASE_INPUT, extractGlossary: true });

  // 3,000 candidates / 50 per chunk: 60 schema-bearing prompts.
  // (60*3000 + 3000*30) * 1.2 * 1.4 = 453,600
  expect(withExtraction.promptTokens - translationOnly.promptTokens).toBe(453_600);
  // 3,000 * 75 * 1.2 * 1.4 = 378,000
  expect(withExtraction.completionTokens - translationOnly.completionTokens).toBe(378_000);
  expect(withExtraction.totalTokens - translationOnly.totalTokens).toBe(831_600);
});

test("caps glossary cost at the configured candidate budget", () => {
  const estimate = estimateUsage({
    ...BASE_INPUT,
    extractGlossary: true,
    glossaryMaxTerms: 100,
  });
  const baseline = estimateUsage({ ...BASE_INPUT, extractGlossary: false });

  // (2*3000 + 100*30) * 1.68 / 100 * 75 * 1.68
  expect(estimate.promptTokens - baseline.promptTokens).toBe(15_120);
  expect(estimate.completionTokens - baseline.completionTokens).toBe(12_600);
});

test("uncapped glossary extraction prices one candidate per entry", () => {
  const uncapped = estimateUsage({
    ...BASE_INPUT,
    extractGlossary: true,
    glossaryMaxTerms: null,
  });
  const cappedAtEntries = estimateUsage({
    ...BASE_INPUT,
    extractGlossary: true,
    glossaryMaxTerms: BASE_INPUT.entries,
  });
  expect(uncapped).toEqual(cappedAtEntries);

  // and strictly more than a tight cap — "no limit" must cost more
  const tight = estimateUsage({
    ...BASE_INPUT,
    extractGlossary: true,
    glossaryMaxTerms: 100,
  });
  expect(uncapped.totalTokens).toBeGreaterThan(tight.totalTokens);
});

test("retry pricing scales with the configured refine passes", () => {
  const noRefine = estimateUsage({ ...BASE_INPUT, extractGlossary: false, maxRefine: 0 });
  const defaults = estimateUsage({ ...BASE_INPUT, extractGlossary: false });

  // maxRefine=0: raw 241,000 prompt / 141,000 completion * 1.4 margin only
  expect(noRefine.promptTokens).toBe(337_400);
  expect(noRefine.completionTokens).toBe(197_400);
  // default maxRefine=2 adds the 1.2 retry factor
  expect(defaults.promptTokens).toBe(404_880);
  expect(defaults.completionTokens).toBe(236_880);
});

test("thinking bills reasoning as extra completion tokens", () => {
  const plain = estimateUsage({ ...BASE_INPUT, extractGlossary: false });
  const thinking = estimateUsage({ ...BASE_INPUT, extractGlossary: false, thinking: true });

  // (100,000 * 1.2 * 2 + 21,000) * 1.2 * 1.4 = 438,480
  expect(thinking.completionTokens).toBe(438_480);
  expect(thinking.promptTokens).toBe(plain.promptTokens);
});

test("actual costs bill the selected route: direct=list price, openrouter=live row", () => {
  const live = { input: 0.2, output: 1.3, cacheRead: 0.1 };
  const table = new Map([["deepseek/deepseek-chat", live]]);

  // Direct DeepSeek is billed by DeepSeek, not by OpenRouter's route price.
  const direct = priceForModel(table, "deepseek/deepseek-chat");
  expect(direct).toEqual({ input: 0.27, output: 1.1, cacheRead: 0.07, source: "static" });
  expect(costUsd({ promptTokens: 1_000_000, completionTokens: 1_000_000 }, direct)).toBe(
    1.37,
  );

  expect(priceForModel(table, "openrouter/deepseek/deepseek-chat")).toEqual({
    ...live,
    source: "openrouter",
  });
});

test("estimates price every token class at the highest known rate", () => {
  const live = { input: 0.2, output: 1.3, cacheRead: 0.1 };
  const table = new Map([["deepseek/deepseek-chat", live]]);

  // direct route: max(live, static) per class — never under the real bill
  expect(estimatePriceForModel(table, "deepseek/deepseek-chat")).toEqual({
    input: 0.27,
    output: 1.3,
    cacheRead: 0.1,
    source: "conservative",
  });
  // openrouter route bills exactly the live row
  expect(estimatePriceForModel(table, "openrouter/deepseek/deepseek-chat")).toEqual({
    ...live,
    source: "openrouter",
  });
  // models OpenRouter no longer lists (xAI grok) fall back to list prices
  expect(estimatePriceForModel(table, "xai/grok-4")).toEqual({
    input: 3.0,
    output: 15.0,
    cacheRead: 0.75,
    source: "static",
  });
});

test("recommended combo resolves offline via the static fallback", () => {
  expect(RECOMMENDED_MODEL).toBe(PROVIDER_TIERS.openrouter.fast);
  expect(estimatePriceForModel(null, RECOMMENDED_MODEL)).toEqual({
    input: 0.09,
    output: 0.18,
    cacheRead: 0.018,
    source: "static",
  });
});

test("healedModel repairs stale tier models after catalog refreshes", () => {
  const tiers = PROVIDER_TIERS.gemini;
  // retired recommendation persisted from an older build
  expect(
    healedModel({ preset: "balanced", model: "gemini/gemini-2.5-flash" }, "gemini", tiers),
  ).toBe(tiers.balanced);
  // model from another provider falls back to the tier (balanced for custom)
  expect(
    healedModel({ preset: "custom", model: "openai/gpt-5.6-luna" }, "gemini", tiers),
  ).toBe(tiers.balanced);
  // aligned tier selection stays untouched
  expect(healedModel({ preset: "fast", model: tiers.fast }, "gemini", tiers)).toBe(null);
  // custom model of the right provider is user intent - never overridden
  expect(
    healedModel({ preset: "custom", model: "gemini/gemini-2.5-pro" }, "gemini", tiers),
  ).toBe(null);
  // providers without tiers (local) never heal
  expect(
    healedModel({ preset: "balanced", model: "ollama_chat/qwen3:8b" }, "ollama", undefined),
  ).toBe(null);
});
