import { fileURLToPath } from "node:url";
import { defineConfig } from "@playwright/test";

const port = 18741;
const baseURL = `http://127.0.0.1:${port}`;

export default defineConfig({
  testDir: "./tests",
  workers: 1,
  use: {
    baseURL,
    channel: process.env.PLAYWRIGHT_CHROMIUM_CHANNEL,
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
  },
  webServer: {
    command: `.venv/bin/python tests/serve_e2e.py ${port}`,
    cwd: fileURLToPath(new URL("../..", import.meta.url)),
    url: `${baseURL}/api/health`,
    gracefulShutdown: { signal: "SIGTERM", timeout: 5_000 },
  },
});
