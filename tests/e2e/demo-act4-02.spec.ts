/**
 * demo-act4-02.spec.ts — Stage 13 / Task 6 demo screenshot
 *
 * Captures the ticket detail panel for a single ticket:
 *   1. Customer asks a question via the widget (auto-creates a Ticket
 *      via the Stage 13 / Task 6 wiring — first customer message
 *      triggers ``TicketService.create``)
 *   2. Agent opens the conversation in the agent workspace
 *   3. The ticket detail panel renders (status, priority, SLA
 *      deadline, audit-event timeline)
 *
 * This is the headline screenshot for the M2.A "tickets live"
 * story: a real ticket, persisted via the customer-inbound hot
 * path, rendered in the agent workspace.
 *
 * Capture target: ``images/demo-act4-02.png`` (artifacts path —
 * see ``tests/e2e/artifacts/`` for the run-local copy).
 *
 * Capture script
 * -----------
 *
 *   # Prereqs (same as the M1 demo recorder):
 *   #   Postgres + Redis + Qdrant up
 *   #   alembic upgrade head (includes the ticket tables migration)
 *   #   apps/web + apps/web-sdk built and served
 *   cd tests/e2e
 *   npx playwright test demo-act4-02.spec.ts
 *
 * TODO(capture): The PNG was not captured in the Stage 13 / Task 6
 * commit because:
 *   (a) the dev servers were not running when this spec landed;
 *   (b) the M2.A frontend ticket page does not exist yet — the
 *       agent workspace currently has no ``/tickets/{id}`` or
 *       ``/inbox/{id}/ticket`` route. The spec below uses a
 *       ``test.skip`` guard so the spec is reviewable as the
 *       contract without breaking CI.
 *
 * When the M2.A frontend ships the ticket page, remove the
 * ``test.skip`` and update the ``TICKET_DETAIL_URL`` / selector
 * below to match the new route.
 *
 * The spec is the contract — the PNG is the cherry on top.
 */
import { expect, test } from '@playwright/test';

import { captureFlow } from './fixtures/screenshots';

// TODO(frontend): replace with the real M2.A ticket-detail route
// once the frontend ships. For now we point at the conversation
// detail page so the spec is structurally complete.
const TICKET_DETAIL_URL =
  process.env['DEMO_TICKET_DETAIL_URL'] ?? '/inbox';

test.describe('demo-act4-02: ticket detail panel', () => {
  test.skip(
    true,
    'M2.A frontend ticket page does not exist yet — see TODO(capture) ' +
      'in tests/e2e/demo-act4-02.spec.ts. Remove this skip when the ' +
      'frontend ships the ticket route.',
  );

  test('captures the ticket detail with status + SLA + events', async ({
    page,
  }, testInfo) => {
    // ---- 1. Open the ticket detail page -----------------------
    // The URL is currently a placeholder — see TODO above. Once
    // the frontend ships a real ticket route, this should be e.g.
    // `/tickets/01HXXXXXX`.
    await page.goto(TICKET_DETAIL_URL);
    // The ticket detail panel must render. Selector is provisional
    // — the M2.A frontend will define its own data-attribute.
    const ticketPanel = page.locator('[data-lumen-panel="ticket-detail"]');
    await expect(ticketPanel).toBeVisible({ timeout: 5_000 });

    // ---- 2. Status + priority + SLA deadline visible --------
    await expect(ticketPanel).toContainText(/status/i);
    await expect(ticketPanel).toContainText(/priority/i);
    await expect(ticketPanel).toContainText(/sla/i);

    // ---- 3. Audit-event timeline is non-empty ---------------
    // At minimum, the "created" event from TicketService.create.
    const events = ticketPanel.locator('[data-lumen-ticket-event]');
    await expect(events.first()).toBeVisible({ timeout: 5_000 });

    // ---- 4. Capture the screenshot ----------------------------
    await page.waitForTimeout(1_000); // settle
    // ``captureFlow`` writes to tests/e2e/artifacts/ (run-local copy)
    // and attaches the buffer to the HTML report. The committed
    // asset at ``images/demo-act4-02.png`` is produced from this
    // same buffer post-run.
    await captureFlow(page, testInfo, 'ticket-detail-complete');
  });
});