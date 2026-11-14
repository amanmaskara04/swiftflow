import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react";

// The console talks only to the gateway, through relative URLs.
// In development Vite proxies /v1 and /.well-known to the gateway;
// override the target with SWIFTFLOW_GATEWAY (e.g. http://localhost:8080).
const CSP = [
  "default-src 'self'",
  "script-src 'self'",
  "style-src 'self'",
  "img-src 'self' data:",
  "font-src 'self'",
  "connect-src 'self'",
  "object-src 'none'",
  "base-uri 'self'",
  "form-action 'self'",
  "frame-ancestors 'none'",
].join("; ");

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, ".", "");
  const target = env.SWIFTFLOW_GATEWAY || env.VITE_GATEWAY_TARGET || "http://localhost:8080";
  const mock = env.VITE_MOCK === "1";
  const proxy = {
    "/v1": { target, changeOrigin: true },
    "/.well-known": { target, changeOrigin: true },
  };
  return {
    plugins: [react()],
    define: {
      // Compile-time constant: when false, the mock folder is dropped from the bundle.
      __MOCK__: JSON.stringify(mock),
    },
    server: { port: 5173, strictPort: true, proxy: mock ? undefined : proxy },
    // `vite preview` mimics the nginx headers so the CSP can be checked locally.
    preview: {
      port: 3000,
      proxy,
      headers: {
        "Content-Security-Policy": CSP,
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "no-referrer",
        "X-Frame-Options": "DENY",
      },
    },
    build: { target: "es2022", sourcemap: false, chunkSizeWarningLimit: 900 },
  };
});
