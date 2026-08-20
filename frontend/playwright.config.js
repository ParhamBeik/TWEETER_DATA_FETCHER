import { existsSync } from "node:fs";
import { defineConfig, devices } from "@playwright/test";

// Prefer the repo's virtualenv when it exists (local runs); CI installs into the
// runner's own interpreter and has no .venv.
const VENV_PYTHON = new URL("../../.venv/bin/python", import.meta.url).pathname;
const PYTHON = process.env.E2E_PYTHON || (existsSync(VENV_PYTHON) ? VENV_PYTHON : "python");

const BACKEND_DIR = new URL("../backend", import.meta.url).pathname;

// Deliberately not 8000/5173. Those are this stack's normal dev ports and are
// routinely occupied by another project's containers, and `reuseExistingServer`
// would then silently drive the E2E suite against the wrong application.
const API_PORT = process.env.E2E_API_PORT || "8021";
const WEB_PORT = process.env.E2E_WEB_PORT || "5183";

// One command so the server never starts before the fixture is in place. The
// database is a file (see settings_e2e) precisely so these can be separate
// processes.
const BACKEND_CMD = [
  `${PYTHON} manage.py migrate --noinput`,
  `${PYTHON} manage.py seed_e2e`,
  `${PYTHON} manage.py runserver 127.0.0.1:${API_PORT} --noreload`,
].join(" && ");

export default defineConfig({
  testDir: "./e2e",
  // Serial: every spec shares one seeded SQLite fixture, and some specs mutate it.
  workers: 1,
  fullyParallel: false,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 2 : 0,
  reporter: process.env.CI ? [["html", { open: "never" }], ["list"]] : "list",
  timeout: 30_000,
  expect: { timeout: 10_000 },

  use: {
    baseURL: `http://127.0.0.1:${WEB_PORT}`,
    trace: "on-first-retry",
    screenshot: "only-on-failure",
    video: "retain-on-failure",
  },

  projects: [
    { name: "chromium", use: { ...devices["Desktop Chrome"] } },
  ],

  webServer: [
    {
      command: BACKEND_CMD,
      cwd: BACKEND_DIR,
      // Must be a 200 without a token: Playwright treats a 401 as "not ready",
      // and every /api/ route is authenticated.
      url: `http://127.0.0.1:${API_PORT}/admin/login/`,
      // Never reuse: a foreign service on this port would be driven silently.
      reuseExistingServer: false,
      timeout: 120_000,
      stdout: "pipe",
      stderr: "pipe",
      env: { DJANGO_SETTINGS_MODULE: "config.settings_e2e" },
    },
    {
      command: `npm run dev -- --host 127.0.0.1 --port ${WEB_PORT}`,
      url: `http://127.0.0.1:${WEB_PORT}`,
      reuseExistingServer: false,
      timeout: 120_000,
      // Overrides the proxy target in vite.config.js for this run only.
      env: { E2E_API_PORT: API_PORT },
    },
  ],
});
