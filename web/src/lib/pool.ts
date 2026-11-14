// Run async jobs with bounded concurrency.
export async function runPool<T>(
  count: number,
  concurrency: number,
  job: (index: number) => Promise<T>,
  signal?: AbortSignal,
): Promise<void> {
  let next = 0;
  const workers = Array.from({ length: Math.max(1, Math.min(concurrency, count)) }, async () => {
    while (next < count) {
      if (signal?.aborted) return;
      const i = next++;
      await job(i);
    }
  });
  await Promise.all(workers);
}

export function sleep(ms: number, signal?: AbortSignal): Promise<void> {
  return new Promise((resolve) => {
    const t = setTimeout(resolve, ms);
    signal?.addEventListener("abort", () => {
      clearTimeout(t);
      resolve();
    });
  });
}

export function percentile(values: number[], p: number): number | null {
  if (!values.length) return null;
  const s = [...values].sort((a, b) => a - b);
  const idx = Math.min(s.length - 1, Math.max(0, Math.ceil((p / 100) * s.length) - 1));
  return s[idx] ?? null;
}
