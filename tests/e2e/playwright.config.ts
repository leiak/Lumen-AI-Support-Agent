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
    // Windows runner: chromium sandbox fails to bind (0x5 Access Denied
    // via crashpad), so disable it. CI/Linux already run without one.
    launchOptions: { args: ['--no-sandbox'] },
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
  //
  // We override to reuseExistingServer:true for all three because the
  // docker stack already provides them (api :8000, web :8080, host :5173).
  // Spawning pnpm dev via cross-spawn under Git Bash currently fails
  // (spawn C:\Windows\system32\cmd.exe ENOENT) — once that's fixed
  // upstream, revert these back to reuseExistingServer:false.
  webServer: [
    {
      name: 'api',
      url: 'http://localhost:8000/health',
      timeout: 60_000,
      reuseExistingServer: true,
    },
    {
      name: 'web',
      url: 'http://localhost:5173',
      timeout: 60_000,
      reuseExistingServer: true,
    },
    {
      name: 'host',
      // The widget host serves tests/e2e/static/ via a tiny Python
      // http.server. It MUST run on a port that the docker stack
      // doesn't already bind (the web nginx container occupies :8080
      // and would otherwise serve its SPA index.html in place of our
      // widget-host.html, with /lumen-widget.js 404'ing via SPA
      // fallback). Port :8181 is unused across the compose network.
      //
      // The ``command`` is documented here but not executed by
      // Playwright — ``reuseExistingServer: true`` (see above) means
      // the operator (or a wrapper script) must start the python
      // server before invoking the suite. Start it with:
      //
      //   cd tests/e2e/static && python -m http.server 8181
      command: 'python -m http.server 8181 --directory tests/e2e/static',
      url: 'http://localhost:8181/widget-host.html',
      timeout: 15_000,
      reuseExistingServer: true,
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
