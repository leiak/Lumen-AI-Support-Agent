/**
 * Playwright config for Lumen AI Support Agent E2E (Task 9.11).
 *
 * Boots three web servers in parallel before the suite runs:
 *   - api   (FastAPI on :8000)   — backend under test
 *   - web   (Vite dev on :5173)  — agent workspace SPA
 *   - host  (python http.server :8080) — static page that embeds the
 *                                          SDK bundle (dist/lumen-widget.js)
 *
 * Prerequisites (run BEFORE `pnpm exec playwright test`):
 *   1. Postgres + Redis + Qdrant are already running locally on their
 *      default ports. If not, the globalSetup health check will fail
 *      loudly with a clear message — the suite does NOT spin up
 *      docker-compose.
 *   2. `cd apps/api && uv run alembic upgrade head` is up to date.
 *      globalSetup also runs this defensively so a fresh checkout
 *      still boots.
 *   3. The Python seed at `tests/e2e/scripts/seed.py` populates the
 *      demo tenant / agent / channel / conversation. globalSetup runs
 *      it after alembic.
 *   4. The widget SDK bundle is built at
 *      `apps/web-sdk/dist/lumen-widget.js` so the static host can
 *      serve it. globalSetup builds it if missing.
 *
 * Why three web servers, not two: the embedded widget MUST be served
 * from a different origin than the agent SPA so the cross-origin CORS
 * + WS-origin checks actually fire (the whole point of 9.10). Vite's
 * proxy only forwards /api*, so we serve the widget host from a tiny
 * static server on :8080.
 */
import { defineConfig, devices } from '@playwright/test';

const TESTS_DIR = new URL('.', import.meta.url).pathname; // .../tests/e2e/
const REPO = new URL('../..', import.meta.url).pathname; // repo root

export default defineConfig({
  // Test files live alongside the config so the suite is portable.
  testDir: '.',

  // Avoid auto-matching helpers and seed fixtures as tests.
  testIgnore: ['**/fixtures/**', '**/scripts/**', '**/static/**'],

  // Cap each test individually so a slow LLM call doesn't pin the
  // whole suite. ``expect.timeout`` keeps individual assertions snappy.
  timeout: 15_000,
  expect: { timeout: 8_000 },

  // Run serially: both flows mutate shared backend state (the seeded
  // conversation + channel). Parallel runs would race.
  fullyParallel: false,
  workers: 1,

  // Fail the build on `pnpm type-check` errors. We don't run it here
  // because the suite is JS-only — TS files are compiled on-the-fly
  // by Playwright via its built-in transformer.
  forbidOnly: !!process.env.CI,

  // Single retry on CI so flakes from LLM latency / WS reconnects
  // don't fail the suite; zero retries locally so a regression shows
  // up immediately.
  retries: process.env.CI ? 1 : 0,

  // Capture trace + screenshot on every failure so post-mortem doesn't
  // require re-running.
  use: {
    baseURL: 'http://localhost:5173',
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
    video: 'retain-on-failure',
    // The widget-host static origin needs to be allowed by the API's
    // CORS allowlist (see apps/api/src/core/config.py). We bake the
    // base URL into the static HTML.
  },

  // One project, one browser (M1 only ships Chromium). The workspace
  // doesn't pin a different browser yet — adding a Firefox project is
  // a Stage 10+ concern.
  projects: [
    {
      name: 'chromium',
      use: { ...devices['Desktop Chrome'] },
    },
  ],

  // Launch all three dev servers in parallel BEFORE the suite. Each
  // ``command`` runs from ``cwd`` and Playwright captures stdout so
  // failures show up in the report.
  webServer: [
    {
      name: 'api',
      command: 'uv run uvicorn main:app --port 8000 --host 127.0.0.1',
      cwd: `${REPO}apps/api`,
      url: 'http://localhost:8000/health',
      timeout: 60_000,
      reuseExistingServer: false,
      stdout: 'pipe',
      stderr: 'pipe',
    },
    {
      name: 'web',
      command: 'pnpm dev --host 127.0.0.1',
      cwd: `${REPO}apps/web`,
      url: 'http://localhost:5173',
      timeout: 60_000,
      reuseExistingServer: false,
      stdout: 'pipe',
      stderr: 'pipe',
    },
    {
      name: 'host',
      command: 'python -m http.server 8080 --bind 127.0.0.1',
      cwd: `${REPO}tests/e2e/static`,
      url: 'http://localhost:8080/widget-host.html',
      timeout: 15_000,
      reuseExistingServer: false,
      stdout: 'pipe',
      stderr: 'pipe',
    },
  ],

  // Pre-flight: alembic upgrade + seed + health check. Runs ONCE
  // before any project starts. Output is captured into the report.
  globalSetup: './global-setup.ts',

  // Reporters: keep the default list-style for CI but also emit JSON
  // + an HTML report under tests/e2e/report/ for post-mortem.
  reporter: [
    ['list'],
    ['html', { outputFolder: 'report', open: 'never' }],
  ],
});
