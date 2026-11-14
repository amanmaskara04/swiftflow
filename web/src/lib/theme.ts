// Theme: explicit light/dark stored per browser; otherwise follow the system.
export type Theme = "light" | "dark";
const KEY = "swiftflow.theme";

export function storedTheme(): Theme | null {
  try {
    const v = localStorage.getItem(KEY);
    return v === "light" || v === "dark" ? v : null;
  } catch {
    return null;
  }
}

export function effectiveTheme(): Theme {
  const s = storedTheme() ?? (document.documentElement.dataset.theme as Theme | undefined);
  if (s === "light" || s === "dark") return s;
  return window.matchMedia?.("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}

export function applyTheme(t: Theme | null): void {
  const root = document.documentElement;
  if (t) root.dataset.theme = t;
  else delete root.dataset.theme;
  try {
    if (t) localStorage.setItem(KEY, t);
    else localStorage.removeItem(KEY);
  } catch {
    /* storage unavailable */
  }
}

export function initTheme(): void {
  const t = storedTheme();
  if (t) document.documentElement.dataset.theme = t;
}

export function reducedMotion(): boolean {
  return window.matchMedia?.("(prefers-reduced-motion: reduce)").matches ?? false;
}
