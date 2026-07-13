/**
 * Provider/model catalog and token estimation.
 *
 * Static MODEL_PRICES is the OFFLINE FALLBACK only — live per-model pricing
 * (incl. cache-read rates) comes from OpenRouter via lib/pricing.ts.
 * Estimates are labeled "예상" in the UI; the constants below are calibrated
 * against real engine runs (DSPy JSONAdapter batches).
 */

import type { PresetId } from "../stores/settings";

export interface ModelPrice {
  /** USD per 1M prompt tokens */
  input: number;
  /** USD per 1M completion tokens */
  output: number;
  /** USD per 1M cached prompt tokens (provider cache read) */
  cacheRead?: number;
}

export const MODEL_PRICES: Record<string, ModelPrice> = {
  "openai/gpt-5.6-sol": { input: 5.0, output: 30.0, cacheRead: 0.5 },
  "openai/gpt-5.6-terra": { input: 2.5, output: 15.0, cacheRead: 0.25 },
  "openai/gpt-5.6-luna": { input: 1.0, output: 6.0, cacheRead: 0.1 },
  "openai/gpt-4.1": { input: 2.0, output: 8.0, cacheRead: 0.5 },
  "openai/gpt-4.1-mini": { input: 0.4, output: 1.6, cacheRead: 0.1 },
  "anthropic/claude-opus-4-8": { input: 5.0, output: 25.0, cacheRead: 0.5 },
  "anthropic/claude-sonnet-4-6": { input: 3.0, output: 15.0, cacheRead: 0.3 },
  "anthropic/claude-haiku-4-5": { input: 1.0, output: 5.0, cacheRead: 0.1 },
  "gemini/gemini-3.1-pro-preview": { input: 2.0, output: 12.0, cacheRead: 0.2 },
  "gemini/gemini-3.5-flash": { input: 1.5, output: 9.0, cacheRead: 0.15 },
  "gemini/gemini-3.1-flash-lite": { input: 0.25, output: 1.5, cacheRead: 0.025 },
  "deepseek/deepseek-chat": { input: 0.27, output: 1.1, cacheRead: 0.07 },
  "deepseek/deepseek-reasoner": { input: 0.55, output: 2.19, cacheRead: 0.14 },
  "xai/grok-4": { input: 3.0, output: 15.0, cacheRead: 0.75 },
  "xai/grok-3": { input: 3.0, output: 15.0, cacheRead: 0.75 },
  "xai/grok-3-mini": { input: 0.3, output: 0.5, cacheRead: 0.075 },
  // Retired/legacy models: no longer offered, kept so History still
  // prices old sessions when the OpenRouter live table is unavailable.
  "openai/gpt-4o": { input: 2.5, output: 10.0, cacheRead: 1.25 },
  "openai/gpt-4o-mini": { input: 0.15, output: 0.6, cacheRead: 0.075 },
  "openai/o4-mini": { input: 1.1, output: 4.4, cacheRead: 0.275 },
  "anthropic/claude-sonnet-4-5": { input: 3.0, output: 15.0, cacheRead: 0.3 },
  "anthropic/claude-opus-4-1": { input: 15.0, output: 75.0, cacheRead: 1.5 },
  "gemini/gemini-2.5-pro": { input: 1.25, output: 10.0, cacheRead: 0.31 },
  "gemini/gemini-2.5-flash": { input: 0.3, output: 2.5, cacheRead: 0.075 },
  "gemini/gemini-2.5-flash-lite": { input: 0.1, output: 0.4, cacheRead: 0.025 },
};

/** Provider display order for the W3 provider selector. */
export const PROVIDER_ORDER: readonly string[] = [
  "openai",
  "anthropic",
  "gemini",
  "deepseek",
  "xai",
  "openrouter",
  "ollama",
  "openai-compatible",
];

/**
 * Providers served from the user's machine: no API key requirement, a
 * base-URL setting, live model lists instead of preset tiers, and zero
 * per-token cost.
 */
export const LOCAL_PROVIDERS: ReadonlySet<string> = new Set(["ollama", "openai-compatible"]);

/** LiteLLM model-string prefixes that differ from our provider ids. */
const PREFIX_TO_PROVIDER: Record<string, string> = {
  ollama_chat: "ollama",
  hosted_vllm: "openai-compatible",
};

/**
 * 제공자별 프리셋 3종 (빠른 / 균형 / 최고 품질). The user picks the provider
 * first (W3 top band); tier cards then offer that provider's models.
 * Providers with a thin lineup may repeat a model across tiers.
 * Ollama has no tiers — the UI shows the live local model list instead.
 */
export const PROVIDER_TIERS: Record<string, Record<PresetId, string>> = {
  openai: {
    fast: "openai/gpt-5.6-luna",
    balanced: "openai/gpt-5.6-terra",
    best: "openai/gpt-5.6-sol",
  },
  anthropic: {
    fast: "anthropic/claude-haiku-4-5",
    balanced: "anthropic/claude-sonnet-4-6",
    best: "anthropic/claude-opus-4-8",
  },
  gemini: {
    fast: "gemini/gemini-3.1-flash-lite",
    balanced: "gemini/gemini-3.5-flash",
    best: "gemini/gemini-3.1-pro-preview",
  },
  deepseek: {
    fast: "deepseek/deepseek-chat",
    balanced: "deepseek/deepseek-chat",
    best: "deepseek/deepseek-reasoner",
  },
  xai: {
    fast: "xai/grok-3-mini",
    balanced: "xai/grok-3",
    best: "xai/grok-4",
  },
  openrouter: {
    fast: "openrouter/openai/gpt-5.6-luna",
    balanced: "openrouter/anthropic/claude-haiku-4.5",
    best: "openrouter/anthropic/claude-sonnet-4.6",
  },
};

export const PRESET_IDS: readonly PresetId[] = ["fast", "balanced", "best"];

/** Provider id a LiteLLM model string belongs to ("ollama_chat/x" -> "ollama"). */
export function providerIdOf(model: string): string {
  const prefix = model.split("/")[0];
  return PREFIX_TO_PROVIDER[prefix] ?? prefix;
}

/** Short display name: "anthropic/claude-haiku-4-5" -> "Claude Haiku 4.5". */
export function modelDisplayName(model: string): string {
  const bare = model.split("/").at(-1) ?? model;
  return bare
    .replace(/^gpt/, "GPT")
    .replace(/^claude/, "Claude")
    .replace(/^gemini/, "Gemini")
    .replace(/^deepseek/, "DeepSeek")
    .replace(/^grok/, "Grok")
    .replace(/^o4/, "o4")
    .replace(/^qwen/, "Qwen")
    .replace(/^llama/, "Llama")
    .replace(/^gemma/, "Gemma")
    .split("-")
    .map((part) => (/^\d+$/.test(part) ? part : part.charAt(0).toUpperCase() + part.slice(1)))
    .join(" ")
    .replace(/(\d) (\d)/g, "$1.$2");
}

/* -------------------------------------------------------------------- */
/* Token estimation                                                     */
/* -------------------------------------------------------------------- */

/**
 * Engine request shape (dspy JSONAdapter): every batch re-sends the
 * signature instructions + JSON schema + field scaffolding, plus a
 * glossary slice; entry keys are echoed in prompt AND completion.
 * Constants calibrated against real runs (observed ~2.4x the old
 * chars/4-only heuristic).
 */
const TOKENS_PER_CHAR = 1 / 3.5;
const BATCH_OVERHEAD_TOKENS = 950;
const GLOSSARY_TOKENS_PER_BATCH = 250;
const KEY_TOKENS_PER_ENTRY = 7;
/** ko/ja/zh completions tokenize to roughly the source token volume */
const COMPLETION_RATIO = 1.0;
/** Glossary curation: 50 candidates per engine request. */
const GLOSSARY_CHUNK_SIZE = 50;
/** DSPy signature/schema plus existing-glossary context on every chunk. */
const GLOSSARY_PROMPT_OVERHEAD_TOKENS = 3000;
/** Candidate line and structured TermRule output token volumes. */
const GLOSSARY_PROMPT_TOKENS_PER_CANDIDATE = 30;
const GLOSSARY_COMPLETION_TOKENS_PER_CANDIDATE = 75;
/** engine-side batch splitting also caps chars per batch */
const MAX_BATCH_CHARS = 8000;

export interface UsageEstimateInput {
  /** source character volume (scan char_count of the selection) */
  chars: number;
  /** entry count of the selection */
  entries: number;
  /** settings.batchSize */
  batchSize: number;
  /** a glossary is sent with every batch (vanilla/community/user) */
  glossary: boolean;
  /** term extraction pass enabled */
  extractGlossary: boolean;
  /** maximum candidates curated by the glossary LLM; null means uncapped */
  glossaryMaxTerms: number | null;
}

export interface UsageEstimate {
  promptTokens: number;
  completionTokens: number;
  totalTokens: number;
}

export function estimateUsage(input: UsageEstimateInput): UsageEstimate {
  const { chars, entries } = input;
  if (chars <= 0 || entries <= 0) {
    return { promptTokens: 0, completionTokens: 0, totalTokens: 0 };
  }
  const batchSize = Math.max(1, input.batchSize);
  // engine splits on entry count AND char volume, whichever is tighter
  const charsWithKeys = chars + entries * 24;
  const batches = Math.max(
    Math.ceil(entries / batchSize),
    Math.ceil(charsWithKeys / MAX_BATCH_CHARS),
  );

  const sourceTokens = chars * TOKENS_PER_CHAR;
  const keyTokens = entries * KEY_TOKENS_PER_ENTRY;

  let prompt =
    batches * BATCH_OVERHEAD_TOKENS +
    (input.glossary ? batches * GLOSSARY_TOKENS_PER_BATCH : 0) +
    sourceTokens +
    keyTokens;
  let completion = sourceTokens * COMPLETION_RATIO + keyTokens;

  // Baseline only: optional refine and schema-retry calls are deliberately
  // excluded so the estimate never prices work that may not run.

  if (input.extractGlossary) {
    const configuredLimit =
      input.glossaryMaxTerms === null
        ? entries
        : Math.max(0, Math.floor(input.glossaryMaxTerms));
    const candidates = Math.min(entries, configuredLimit);
    const chunks = Math.ceil(candidates / GLOSSARY_CHUNK_SIZE);
    prompt +=
      chunks * GLOSSARY_PROMPT_OVERHEAD_TOKENS +
      candidates * GLOSSARY_PROMPT_TOKENS_PER_CANDIDATE;
    completion += candidates * GLOSSARY_COMPLETION_TOKENS_PER_CANDIDATE;
  }

  const promptTokens = Math.round(prompt);
  const completionTokens = Math.round(completion);
  return {
    promptTokens,
    completionTokens,
    totalTokens: promptTokens + completionTokens,
  };
}
