/** Deterministic JSON text; structured records retain their original values. */
function ordered(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(ordered);
  if (value !== null && typeof value === "object") {
    return Object.fromEntries(
      Object.keys(value)
        .sort()
        .map((key) => [key, ordered((value as Record<string, unknown>)[key])])
    );
  }
  return value;
}
export function prettyJson(value: unknown): string {
  return JSON.stringify(ordered(value), null, 2);
}
