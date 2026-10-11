/**
 * demo-act4-02.spec.ts — M2.A Stage 15 ticket-detail demo screenshot.
 *
 * Captures the agent ticket detail panel for a single ticket:
 *   1. Open the admin SPA at ``/admin/tickets/{id}`` (the ticket id
 *      is provided via ``DEMO_TICKET_ID`` env var, defaulting to the
 *      ``T_P1_BILLING`` ticket seeded by ``seed_demo_rich.py``).
 *   2. The detail panel renders with status / priority / SLA deadline
 *      and the audit-event timeline (which has at least one row
 *      because the rich seed inserts ``new → triaged → ...`` events).
 *   3. Capture the screenshot to ``images/demo-act4-02.png`` (the
 *      artefact path mirrors other demo-act4 specs).
 *
 * This is the headline screenshot for the M2.A "tickets live" story:
 * a real ticket, persisted via the customer-inbound hot path, rendered
 * in the agent workspace.
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
 *   #   tests/e2e/scripts/seed_demo_rich.py executed (rich seed with tickets)
 *   #   apps/web + apps/web-sdk built and served
 *   cd tests/e2e
 *   npx playwright test demo-act4-02.spec.ts
 *
 * Migration history: this spec shipped as a stub with ``test.skip`` in
 * Stage 13 because the frontend ticket page did not exist yet. Once
 * Stage 20 (#20 admin SPA UI) shipped ``admin-ticket-detail.tsx`` we
 * can drop the skip and exercise the real route. The selectors below
 * mirror ``data-testid`` attributes that admin-ticket-detail.tsx ships.
 */
import { expect, test } from '@playwright/test';

import { captureFlow } from './fixtures/screenshots';
import { JWT_STORAGE_KEY } from './fixtures/auth-shared';
import { API_BASE_DIRECT, SEED } from './fixtures/seed';

// The rich seed inserts a deterministic P1 billing ticket
// (``01HZDEMOT0P1BILLING0TICKET``) — see
// ``tests/e2e/scripts/seed_demo_rich.py``. Allow override via env so
// the same spec can be reused against a fresh ticket id if the rich
// seed evolves.
const TICKET_ID =
  process.env['DEMO_TICKET_ID'] ?? '01HZDEMOT0P1BILLING0TICKET';

// The web SPA origin. Resolved via env so the spec can target either
// the Vite dev server (default :5173 per playwright.config.ts) or the
// docker-compose stack (:8080). The Playwright ``baseURL`` setting
// is only applied to ``page.goto`` when the test resolves against the
// configured per-project use — fixtures like :func:`loginViaApi` use
// absolute URLs for the ``/`` landing visit, so we hard-code here.
const WEB_URL = process.env['DEMO_WEB_URL'] ?? 'http://localhost:5173';
const TICKET_DETAIL_URL = `${WEB_URL}/admin/tickets/${TICKET_ID}`;

test.describe('demo-act4-02: ticket detail panel', () => {
  test('captures the ticket detail with status + SLA + events', async ({
    page,
    request,
  }, testInfo) => {
    // ---- 0. Log in via the API so the SPA boots authenticated ------
    // The admin ticket detail route is gated behind AdminGuard →
    // AgentGuard → auth, so we need a valid JWT in localStorage before
    // the SPA router does its first redirect. We inline the login
    // (rather than calling :func:`loginViaApi`) because the fixture's
    // ``page.goto('/')`` is relative to playwright's configured
    // baseURL; against the docker stack we want to bind localStorage
    // to the docker SPA origin (:8080) instead of the configured :5173.
    const lookup = await request.get(
      `${API_BASE_DIRECT}/api/v1/auth/lookup-tenant`,
      {
        params: { email: SEED.adminEmail },
        headers: { 'X-Tenant-Id': SEED.tenantId },
      },
    );
    if (lookup.status() !== 200) {
      throw new Error(
        `auth fixture: tenant lookup returned ${lookup.status()} — did the seed script run?`,
      );
    }
    const lookupBody = (await lookup.json()) as { tenant_id: string | null };
    if (!lookupBody.tenant_id) {
      throw new Error(
        `auth fixture: tenant lookup returned null for ${SEED.adminEmail}`,
      );
    }
    const login = await request.post(
      `${API_BASE_DIRECT}/api/v1/auth/login`,
      {
        headers: { 'X-Tenant-Id': lookupBody.tenant_id },
        data: { email: SEED.adminEmail, password: SEED.password },
      },
    );
    if (login.status() !== 200) {
      throw new Error(
        `auth fixture: /login returned ${login.status()} — ${await login.text()}`,
      );
    }
    const loginBody = (await login.json()) as { access_token: string };

    // Bind JWT to the WEB_URL origin before any navigation so the SPA's
    // axios interceptor picks it up on the first request.
    await page.goto(WEB_URL);
    await page.evaluate(
      ([key, token]) => window.localStorage.setItem(key, token),
      [JWT_STORAGE_KEY, loginBody.access_token],
    );

    // ---- 1. Open the ticket detail page ---------------------------
    await page.goto(TICKET_DETAIL_URL);

    // The ticket detail page root carries the canonical testid.
    const ticketPanel = page.locator(
      '[data-testid="admin-ticket-detail-page"]',
    );
    await expect(ticketPanel).toBeVisible({ timeout: 5_000 });

    // ---- 2. Status + priority + SLA deadline visible --------
    // The page renders priority / status / category / subject as
    // individual ``ticket-detail-*`` testids, plus SLA inside the
    // metadata column. We assert on the high-level text contents the
    // demo narrative calls out.
    await expect(
      page.locator('[data-testid="ticket-detail-priority"]'),
    ).toBeVisible();
    await expect(
      page.locator('[data-testid="ticket-detail-status"]'),
    ).toBeVisible();
    await expect(
      page.locator('[data-testid="ticket-detail-subject"]'),
    ).toBeVisible();
    // SLA deadline lives in the right-hand metadata column. The
    // component renders the formatted timestamp in a small tile; we
    // just assert the tile shows up — the SLA label lives in the
    // visible text near ``ticket-detail-sla-deadline``.
    await expect(ticketPanel).toContainText(/sla/i);

    // ---- 3. Audit-event timeline is non-empty ---------------
    // ``seed_demo_rich.py`` writes a 4-row event chain (new → triaged
    // → in_progress → resolved), so the events list always has at
    // least one row.
    const eventsList = page.locator(
      '[data-testid="ticket-events-list"]',
    );
    await expect(eventsList).toBeVisible({ timeout: 5_000 });
    const firstEvent = page.locator(
      '[data-testid^="ticket-event-row-"]',
    );
    await expect(firstEvent.first()).toBeVisible({ timeout: 5_000 });

    // ---- 4. Capture the screenshot ----------------------------
    await page.waitForTimeout(1_000); // settle
    // ``captureFlow`` writes to tests/e2e/artifacts/ (run-local copy)
    // and attaches the buffer to the HTML report. The committed asset
    // at ``images/demo-act4-02.png`` is produced from this same
    // buffer post-run (see ``tests/e2e/scripts/record-demo.sh``).
    await captureFlow(page, testInfo, 'ticket-detail-complete');
  });
});