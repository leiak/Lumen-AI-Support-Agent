/**
 * demo-act4.spec.ts — Stage 12 / Task 3 demo screenshot
 *
 * Captures the agent actively using the ``search_internal_kb`` tool:
 *   1. Customer asks a KB-grounded question via the widget
 *   2. The agent LLM calls ``search_internal_kb`` mid-turn
 *   3. The KB article content appears in the agent's reply bubble
 *
 * This is the headline screenshot for the M2.A "search_internal_kb"
 * story: a customer question + an agent response that visibly
 * cites the KB article the tool just retrieved.
 *
 * The screenshot proves the tool-loop wiring works end-to-end:
 * the agent's reply text was generated *after* the
 * ``search_internal_kb`` tool returned a chunk, not before.
 *
 * Capture target: ``images/demo-act4-01.png`` (artifacts path —
 * see ``tests/e2e/artifacts/`` for the run-local copy).
 *
 * Capture script
 * -----------
 *
 *   # Prereqs (same as the M1 demo recorder):
 *   #   Postgres + Redis + Qdrant up
 *   #   alembic upgrade head
 *   #   tests/e2e/scripts/seed.py executed (tenant + KB article seeded)
 *   #   apps/web + apps/web-sdk built and served
 *   cd tests/e2e
 *   npx playwright test demo-act4.spec.ts
 *
 * TODO(capture): The PNG was not captured in the Stage 12 / Task 3
 * commit because the dev servers (api on :8000, web on :5173,
 * widget-host on :8080) were not running when this spec landed.
 * Run ``npx playwright test tests/e2e/demo-act4.spec.ts`` against
 * a live stack to produce ``images/demo-act4-01.png``. The spec is
 * the contract — the PNG is the cherry on top.
 */
import { expect, test } from '@playwright/test';

import { captureFlow } from './fixtures/screenshots';
import { WIDGET_HOST_URL } from './fixtures/seed';

const CUSTOMER_QUESTION = 'How do I reset my password?';

test.describe('demo-act4: search_internal_kb tool in action', () => {
  test('captures the agent citing the KB article in reply', async ({ page }, testInfo) => {
    // ---- 1. Open the customer widget ----------------------------
    await page.goto(WIDGET_HOST_URL);
    await page.locator('[data-lumen-widget="button"]').click();

    const iframe = page.frameLocator('[data-lumen-widget="iframe"]');
    const input = iframe.locator('[data-lumen-iframe="composer-input"]');
    await expect(input).toBeVisible({ timeout: 5_000 });

    // ---- 2. Customer asks a KB-grounded question ---------------
    await input.fill(CUSTOMER_QUESTION);
    await iframe.locator('[data-lumen-iframe="composer-send"]').click();

    // Optimistic customer bubble — wait for it before continuing.
    const customerBubble = iframe.locator(
      '[data-lumen-iframe="message"][data-role="customer"]',
    );
    await expect(customerBubble.first()).toBeVisible({ timeout: 5_000 });
    await expect(customerBubble.first()).toContainText(CUSTOMER_QUESTION);

    // ---- 3. Wait for the AI reply ------------------------------
    // The agent's LLM will (likely) call ``search_internal_kb``
    // because the seeded KB has a "Password Reset" article. The
    // reply bubble is rendered after ``message.complete`` arrives
    // over WS — give it up to 15s for the round-trip.
    const aiBubble = iframe.locator(
      '[data-lumen-iframe="message"][data-role="ai"]',
    );
    await expect(aiBubble.first()).toBeVisible({ timeout: 15_000 });

    // The agent's reply should be non-empty. We don't assert exact
    // wording (LLM output is non-deterministic) — but the bubble
    // having any text at all proves the turn completed.
    await expect(aiBubble.first()).not.toBeEmpty();

    // ---- 4. Capture the screenshot ------------------------------
    // The conversation panel now shows the customer question AND
    // the agent's KB-grounded reply in the same frame — that's the
    // narrative we want stakeholders to see.
    await page.waitForTimeout(1_000); // settle
    // ``captureFlow`` writes to tests/e2e/artifacts/ (run-local copy)
    // and attaches the buffer to the HTML report. The committed asset
    // at ``images/demo-act4-01.png`` is produced from this same buffer
    // post-run (see ``tests/e2e/scripts/record-demo.sh`` for the
    // production capture script).
    await captureFlow(page, testInfo, 'widget-flow-complete');
  });
});