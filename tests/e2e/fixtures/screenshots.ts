/**
 * Screenshot capture helpers — used by the final assertion in each
 * flow to drop a PNG into tests/e2e/artifacts/ for manual review.
 *
 * Playwright's built-in `page.screenshot()` is fine for most cases
 * but our final assertions want a deterministic filename regardless
 * of which test ran first. ``captureFlow`` writes to a stable path
 * AND attaches the buffer to the test report.
 */
import { mkdir, writeFile } from 'node:fs/promises';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

import type { Page, TestInfo } from '@playwright/test';

// ES-module-safe __dirname equivalent.
const HERE = dirname(fileURLToPath(import.meta.url));
const ARTIFACTS = join(HERE, '..', 'artifacts');

/**
 * Capture a full-page screenshot and persist it under
 * tests/e2e/artifacts/<flow>.png. Also attaches a copy to the
 * current test report so it shows up in the HTML report.
 *
 * ``name`` MUST be unique per flow (Flow A / Flow B each have one
 * final screenshot — see the task spec).
 *
 * Persistence: the disk write happens BEFORE the testInfo.attach so
 * that even if the runner's output cleanup deletes the test-results
 * directory after a passing run, the run-local artefact under
 * ``tests/e2e/artifacts/`` survives (post-run shell scripts pick it
 * up to commit into ``images/<name>.png``).
 */
export async function captureFlow(
  page: Page,
  testInfo: TestInfo,
  name:
    | 'agent-flow-complete'
    | 'widget-flow-complete'
    // Stage 13 / Task 6 (M2.A): ticket detail flow.
    | 'ticket-detail-complete',
): Promise<void> {
  await mkdir(ARTIFACTS, { recursive: true });
  const filePath = join(ARTIFACTS, `${name}.png`);
  const buffer = await page.screenshot({ fullPage: true });
  await writeFile(filePath, buffer);
  await testInfo.attach(name, { body: buffer, contentType: 'image/png' });
  // eslint-disable-next-line no-console -- intentional operator log
  console.log(`[e2e] screenshot saved → ${filePath}`);
}
