import { describe, expect, test } from "bun:test";

import { resolveProviderSecret } from "./providerSecrets.ts";

describe("resolveProviderSecret", () => {
  test("keeps a direct key while the all-provider query is stale", () => {
    expect(resolveProviderSecret("openai", "sk-direct", undefined)).toBe("sk-direct");
  });

  test("uses the refreshed all-provider key when the direct query is stale", () => {
    expect(
      resolveProviderSecret("openai", null, {
        openai: "sk-refreshed",
        anthropic: "sk-other",
      }),
    ).toBe("sk-refreshed");
  });

  test("never borrows another provider key", () => {
    expect(
      resolveProviderSecret("openai", null, { anthropic: "sk-other" }),
    ).toBeNull();
  });
});
