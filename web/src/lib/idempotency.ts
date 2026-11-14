// Idempotency keys for writes. Each key is unique per logical request; a retry of the
// same request (after a 401 refresh or a 429) reuses its key.

export function newKey(prefix = "console"): string {
  const id =
    typeof crypto !== "undefined" && "randomUUID" in crypto
      ? crypto.randomUUID()
      : `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
  return `${prefix}-${id}`;
}

export function randomHex(bytes = 4): string {
  const a = new Uint8Array(bytes);
  crypto.getRandomValues(a);
  return Array.from(a, (b) => b.toString(16).padStart(2, "0")).join("");
}
