import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

// In development the API runs on :8000. Its Origin check requires
// JENDOCK_PUBLIC_ORIGIN to equal the dev server origin (see README).
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: { "/api": { target: "http://127.0.0.1:8000" } },
  },
  build: { sourcemap: false, target: "es2022" },
  test: {
    environment: "jsdom",
    setupFiles: ["./vitest.setup.ts"],
    css: false,
    restoreMocks: true,
    // Primer renders far more DOM than hand-written markup did; the editor tests are heavy.
    testTimeout: 30_000,
    // Primer ships ESM that imports its own .css files; they must go through Vite, not Node.
    server: { deps: { inline: [/@primer\//] } },
  },
});
