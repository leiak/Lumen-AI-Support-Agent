/**
 * Minimal Playwright config for running against the docker-compose
 * stack (api :8000, web :8080, postgres :25432, redis :26379,
 * qdrant :26333).
 *
 * The default ``playwright.config.ts`` boots its own Vite dev server
 * on :5173 + a static widget host on :8080 — both of which collide
 * with the docker-compose web (:8080) and widget-host ports. This
 * config drops those webServers so a single spec can run against an
 * already-running stack.
 *
 * Usage:
 *   pnpm exec playwright test \
 *     --config=tests/e2e/playwright.docker.config.ts \
 *     demo-act4-02.spec.ts
 */
import { defineConfig, devices } from '@playwright/test';

export default defineConfig({
  testDir: '.',
  testIgnore: ['**/fixtures/**', '**/scripts/**', '**/static/**'],
  timeout: 30_000,
  expect: { timeout: 10_000 },
  fullyParallel: false,
  workers: 1,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 1 : 0,
  use: {
    baseURL: 'http://localhost:8080',
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
    video: 'retain-on-failure',
  },
  projects: [
    {
      name: 'chromium',
      use: { ...devices['Desktop Chrome'] },
    },
  ],
  // No webServer block — we assume the stack is already running.
  // Run ``docker compose -f deploy/docker-compose.yml up -d`` first.
  reporter: [['list'], ['html', { outputFolder: 'report-docker', open: 'never' }]],
});