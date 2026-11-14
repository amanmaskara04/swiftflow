/** @type {import('tailwindcss').Config} */
const v = (name) => `var(--${name})`;
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  darkMode: ["selector", '[data-theme="dark"]'],
  theme: {
    extend: {
      colors: {
        paper: v("paper"), sheet: v("sheet"), sunk: v("sunk"), ink: v("ink"), "ink-2": v("ink-2"), rule: v("rule"),
        signal: v("signal"), "signal-ink": v("signal-ink"), go: v("go"), stop: v("stop"), route: v("route"),
        "go-wash": v("go-wash"), "stop-wash": v("stop-wash"), "route-wash": v("route-wash"), "signal-wash": v("signal-wash"),
      },
      fontFamily: { display: v("display"), body: v("body"), mono: v("mono") },
      borderRadius: { ctl: "6px", card: "10px", badge: "4px" },
    },
  },
  plugins: [],
};
