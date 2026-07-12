/**
 * Model pricing from OpenRouter plus the static direct-provider table.
 * Direct-provider models use the lower known rate for each token class so
 * displayed costs may be low but are not inflated by OpenRouter markup.
 * OpenRouter-selected models use their live OpenRouter row. Ollama is free.
 *
 * All rates are normalized to USD per 1M tokens.
 */

import { useQuery } from "@tanstack/react-query";

import { moru } from "./bridge";
import { MODEL_PRICES, providerIdOf, type ModelPrice } from "./models";

const OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models";
const CACHE_KEY = "moru:openrouter-pricing";
const CACHE_TTL_MS = 6 * 60 * 60 * 1000; // 6h

export interface LivePrice extends ModelPrice {
  /** where the numbers came from */
  source: "openrouter" | "static" | "conservative" | "free";
}

export type PricingTable = ReadonlyMap<string, ModelPrice>;

interface OpenRouterModel {
  id: string;
  pricing?: {
    prompt?: string;
    completion?: string;
    input_cache_read?: string;
    input_cache_write?: string;
  };
}

function perMillion(perToken: string | undefined): number | undefined {
  if (perToken === undefined) return undefined;
  const n = Number(perToken);
  return Number.isFinite(n) && n >= 0 ? n * 1_000_000 : undefined;
}

function parseTable(models: OpenRouterModel[]): Map<string, ModelPrice> {
  const table = new Map<string, ModelPrice>();
  for (const m of models) {
    const input = perMillion(m.pricing?.prompt);
    const output = perMillion(m.pricing?.completion);
    if (input === undefined || output === undefined) continue;
    table.set(m.id, { input, output, cacheRead: perMillion(m.pricing?.input_cache_read) });
  }
  return table;
}

/* -------------------------------------------------------------------- */
/* LiteLLM model id -> OpenRouter model id                               */
/* -------------------------------------------------------------------- */

/** LiteLLM ids whose OpenRouter counterpart is not mechanical. */
const OR_ID_OVERRIDES: Record<string, string> = {
  "deepseek/deepseek-reasoner": "deepseek/deepseek-r1",
};

/**
 * Map a LiteLLM model string to its OpenRouter id; null when the model is
 * local (ollama) or unmappable.
 */
export function openRouterId(model: string): string | null {
  if (model.startsWith("ollama")) return null;
  if (model.startsWith("openrouter/")) return model.slice("openrouter/".length);
  const override = OR_ID_OVERRIDES[model];
  if (override !== undefined) return override;

  const [provider, ...rest] = model.split("/");
  let name = rest.join("/");
  if (name === "") return null;

  // LiteLLM writes versions with dashes ("claude-sonnet-4-5"), OpenRouter
  // with a dot ("claude-sonnet-4.5").
  if (provider === "anthropic") name = name.replace(/-(\d+)-(\d+)$/, "-$1.$2");

  switch (provider) {
    case "gemini":
      return `google/${name}`;
    case "xai":
      return `x-ai/${name}`;
    default:
      return `${provider}/${name}`;
  }
}

/* -------------------------------------------------------------------- */
/* Fetch + cache                                                        */
/* -------------------------------------------------------------------- */

let memoryTable: Map<string, ModelPrice> | null = null;

function readLocalCache(): Map<string, ModelPrice> | null {
  try {
    const raw = window.localStorage.getItem(CACHE_KEY);
    if (raw === null) return null;
    const { at, entries } = JSON.parse(raw) as {
      at: number;
      entries: [string, ModelPrice][];
    };
    if (Date.now() - at > CACHE_TTL_MS) return null;
    return new Map(entries);
  } catch {
    return null;
  }
}

function writeLocalCache(table: Map<string, ModelPrice>): void {
  try {
    window.localStorage.setItem(
      CACHE_KEY,
      JSON.stringify({ at: Date.now(), entries: [...table.entries()] }),
    );
  } catch {
    // quota/serialization failures are non-fatal
  }
}

/**
 * OpenRouter pricing table, memoized in-module and mirrored to
 * localStorage. Returns null when offline and no fresh cache exists —
 * callers fall back to MODEL_PRICES via priceForModel.
 */
export async function fetchPricingTable(): Promise<PricingTable | null> {
  if (memoryTable !== null) return memoryTable;
  const cached = readLocalCache();
  if (cached !== null) {
    memoryTable = cached;
    return cached;
  }
  try {
    // main-process proxy: the file:// renderer origin is CORS-restricted
    const res = await moru.webRequest({ url: OPENROUTER_MODELS_URL });
    if (res.status !== 200) return null;
    const body = JSON.parse(res.body) as { data?: OpenRouterModel[] };
    const table = parseTable(body.data ?? []);
    if (table.size === 0) return null;
    memoryTable = table;
    writeLocalCache(table);
    return table;
  } catch {
    return null;
  }
}

/** react-query wrapper; data is null (not undefined) when unavailable. */
export function usePricingTable(): PricingTable | null {
  const query = useQuery({
    queryKey: ["openrouter-pricing"],
    queryFn: fetchPricingTable,
    staleTime: CACHE_TTL_MS,
    gcTime: CACHE_TTL_MS,
    retry: 1,
  });
  return query.data ?? null;
}

/* -------------------------------------------------------------------- */
/* Price resolution + cost math                                         */
/* -------------------------------------------------------------------- */

/**
 * Price for a LiteLLM model string. OpenRouter models use their live route
 * price. Direct-provider models use the lower of the static direct price and
 * the OpenRouter row for every token class; unknown paid models return null.
 */
export function priceForModel(table: PricingTable | null, model: string): LivePrice | null {
  if (providerIdOf(model) === "ollama") {
    return { input: 0, output: 0, cacheRead: 0, source: "free" };
  }
  const orId = openRouterId(model);
  const live = table !== null && orId !== null ? table.get(orId) : undefined;
  const staticPrice =
    MODEL_PRICES[model] ??
    (model.startsWith("openrouter/")
      ? MODEL_PRICES[model.slice("openrouter/".length).replace(/\./g, "-")]
      : undefined);

  if (model.startsWith("openrouter/")) {
    if (live !== undefined) return { ...live, source: "openrouter" };
    return staticPrice !== undefined ? { ...staticPrice, source: "static" } : null;
  }
  if (live !== undefined && staticPrice !== undefined) {
    const liveCache = live.cacheRead ?? live.input;
    const staticCache = staticPrice.cacheRead ?? staticPrice.input;
    return {
      input: Math.min(live.input, staticPrice.input),
      output: Math.min(live.output, staticPrice.output),
      cacheRead: Math.min(liveCache, staticCache),
      source: "conservative",
    };
  }
  if (staticPrice !== undefined) return { ...staticPrice, source: "static" };
  return live !== undefined ? { ...live, source: "openrouter" } : null;
}

export interface TokenUsageLike {
  promptTokens: number;
  completionTokens: number;
  /** prompt tokens served from the provider's cache (subset of promptTokens) */
  cachedTokens?: number;
}

/** USD cost of a usage snapshot; cached prompt tokens billed at cacheRead. */
export function costUsd(usage: TokenUsageLike, price: ModelPrice): number {
  const cached = Math.min(usage.cachedTokens ?? 0, usage.promptTokens);
  const fresh = usage.promptTokens - cached;
  const cacheRate = price.cacheRead ?? price.input;
  return (
    (fresh * price.input + cached * cacheRate + usage.completionTokens * price.output) /
    1_000_000
  );
}

/** cachedTokens / promptTokens as 0-100, null when nothing measured. */
export function cacheRatioPercent(usage: TokenUsageLike): number | null {
  if (usage.promptTokens <= 0) return null;
  const cached = Math.min(usage.cachedTokens ?? 0, usage.promptTokens);
  return Math.round((cached / usage.promptTokens) * 100);
}
