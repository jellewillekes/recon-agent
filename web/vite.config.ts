import { defineConfig } from "vitest/config";

// The API serves the build from src/recon/api/static (gitignored; ADR 0033).
// In `make dev` the API runs on :8000 and Vite proxies these paths to it.
const API_PATHS = ["/investigate", "/capabilities", "/runs", "/evals", "/healthz", "/readyz", "/docs", "/openapi.json"];

export default defineConfig({
  build: {
    outDir: "../src/recon/api/static",
    emptyOutDir: true,
  },
  server: {
    proxy: Object.fromEntries(API_PATHS.map((path) => [path, "http://127.0.0.1:8000"])),
  },
  test: {
    include: ["src/**/*.test.{ts,tsx}"],
    environment: "node",
  },
});
