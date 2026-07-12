/**
 * Resolve one provider key from the focused query and the all-provider refresh.
 * Either query may briefly hold stale null data while the other has the key.
 */
export function resolveProviderSecret(
  providerId: string,
  direct: string | null | undefined,
  all: Record<string, string | null> | undefined,
): string | null {
  const candidate = direct || all?.[providerId];
  return typeof candidate === "string" && candidate.length > 0
    ? candidate
    : null;
}
