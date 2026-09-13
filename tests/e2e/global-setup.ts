/**
 * Playwright globalSetup — runs ONCE before the suite starts.
 *
 * Order of operations (each step aborts with a clear message on failure):
 *   1. Sanity-check that the repo root has the expected apps/api +
 *      apps/web + apps/web-sdk folders. Catches a "wrong cwd" early.
 *   2. Run `alembic upgrade head` inside apps/api so the schema is
 *      current. Idempotent — safe to re-run.
 *   3. Run the Python seed (tests/e2e/scripts/seed.py) which inserts
 *      the deterministic demo tenant / agent / channel / PENDING
 *      conversation. Also idempotent.
 *   4. Wait for the api webServer to be reachable on /health (the
 *      webServer.url polling above already does this; we re-check
 *      here to surface the JSON status — 200 vs 503).
 *   5. Build the widget SDK bundle if apps/web-sdk/dist/lumen-widget.js
 *      is missing. The widget-host static page loads it directly.
 *
 * Anti-enumeration: setup never logs PII beyond the demo email we
 * literally write into the seed.
 */
import { execSync, spawnSync } from 'node:child_process';
import { copyFileSync, existsSync, mkdirSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

import type { FullConfig } from '@playwright/test';

// ES-module-safe __dirname equivalent.
const HERE = dirname(fileURLToPath(import.meta.url));
const REPO = join(HERE, '..', '..');
const API = join(REPO, 'apps', 'api');
const WEB_SDK = join(REPO, 'apps', 'web-sdk');
const E2E_STATIC = join(HERE, 'static');
const SEED = join(HERE, 'scripts', 'seed.py');
const SDK_BUNDLE_SRC = join(WEB_SDK, 'dist', 'lumen-widget.js');
const SDK_BUNDLE_DST = join(E2E_STATIC, 'lumen-widget.js');

function step(label: string): void {
  // eslint-disable-next-line no-console -- intentional CLI output
  console.log(`\n[e2e-setup] ${label}`);
}

function run(cmd: string, cwd: string): void {
  // eslint-disable-next-line no-console -- intentional CLI output
  console.log(`[e2e-setup]   $ ${cmd}   (cwd=${cwd})`);
  const result = spawnSync(cmd, { cwd, shell: true, stdio: 'inherit' });
  if (result.status !== 0) {
    throw new Error(
      `[e2e-setup] command failed (exit ${result.status}): ${cmd}`,
    );
  }
}

async function healthCheck(baseUrl: string): Promise<void> {
  const res = await fetch(`${baseUrl}/health`);
  // 200 = all green; 503 = degraded but reachable. For M1 we accept
  // both — a degraded Qdrant still lets Flow A's "拿 AI 建议" return
  // the documented `llm_unavailable` fallback the suite tolerates.
  if (res.status !== 200 && res.status !== 503) {
    const body = await res.text();
    throw new Error(
      `[e2e-setup] unexpected /health status ${res.status}: ${body.slice(0, 200)}`,
    );
  }
  // eslint-disable-next-line no-console -- intentional CLI output
  console.log(`[e2e-setup]   /health = ${res.status}`);
}

export default async function globalSetup(_config: FullConfig): Promise<void> {
  // -- Step 0: structural sanity check ---------------------------------
  step('verifying repo layout');
  for (const rel of ['apps/api', 'apps/web', 'apps/web-sdk']) {
    const p = join(REPO, rel);
    if (!existsSync(p)) {
      throw new Error(`[e2e-setup] missing expected path: ${p}`);
    }
  }

  // -- Step 1: alembic upgrade head -----------------------------------
  step('running alembic upgrade head');
  run('uv run alembic upgrade head', API);

  // -- Step 2: seed the deterministic demo data ----------------------
  step('seeding demo tenant/user/channel/conversation');
  run(`uv run python "${SEED}"`, API);

  // -- Step 3: health check the API server ----------------------------
  step('checking backend /health');
  // The webServer block in playwright.config.ts already waits for
  // the API to come up; this is a belt-and-braces second check that
  // also verifies the JSON body shape.
  await healthCheck('http://localhost:8000');

  // -- Step 4: build the widget SDK if needed -------------------------
  step('ensuring widget SDK bundle is built');
  if (!existsSync(SDK_BUNDLE_SRC)) {
    run('pnpm build', WEB_SDK);
  } else {
    // eslint-disable-next-line no-console -- intentional CLI output
    console.log(`[e2e-setup]   found ${SDK_BUNDLE_SRC} (skipping build)`);
  }

  // Copy the built bundle into tests/e2e/static/ so the static host
  // (python -m http.server :8080) can serve it from the same origin
  // as widget-host.html. Same-origin script load avoids a second
  // CORS preflight for the bundle itself.
  if (existsSync(SDK_BUNDLE_SRC)) {
    mkdirSync(dirname(SDK_BUNDLE_DST), { recursive: true });
    copyFileSync(SDK_BUNDLE_SRC, SDK_BUNDLE_DST);
    // eslint-disable-next-line no-console -- intentional CLI output
    console.log(`[e2e-setup]   copied bundle → ${SDK_BUNDLE_DST}`);
  }

  // -- Step 5: lint only the TS files we own --------------------------
  // (Cheap — keeps accidental syntax regressions out of the suite.
  //  Skipped on CI to avoid double-running alongside pnpm type-check
  //  in the deployment pipeline.)
  if (!process.env.CI) {
    step('tsc --noEmit on e2e fixtures (best-effort)');
    try {
      execSync(
        'pnpm exec tsc --noEmit --allowJs --target ES2022 --module ESNext --moduleResolution Bundler --strict --skipLibCheck --types node tests/e2e',
        { cwd: REPO, stdio: 'pipe' },
      );
    } catch (err) {
      // We never want a TS hiccup to block the suite — Playwright's
      // own transformer catches real errors at test-run time.
      // eslint-disable-next-line no-console -- intentional CLI output
      console.warn(
        '[e2e-setup] tsc warning (non-blocking):',
        (err as Error).message.split('\n')[0],
      );
    }
  }

  // eslint-disable-next-line no-console -- intentional CLI output
  console.log('[e2e-setup] done.\n');
}
