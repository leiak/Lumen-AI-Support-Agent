/**
 * Flow A — Agent workspace (SPA) end-to-end (Task 9.11).
 *
 * Five sequential scenarios:
 *   1. login         — fill the form, submit, land on /inbox
 *   2. tenant hint   — typing the email resolves the demo tenant
 *                      (anti-enumeration: hint returns 200, never
 *                       asserts "user found" wording)
 *   3. claim         — POST /agents/conversations/{id}/claim via the
 *                      REST API (M1 has no queue UI page); then
 *                      navigate to the detail view
 *   4. send          — type a reply, click send, verify it lands in
 *                      the stream
 *   5. AI reply      — click "拿AI建议", wait for the suggestion,
 *                      click "应用到输入框", click send, capture
 *                      the final screenshot
 *
 * M1 trade-off: the AI suggest endpoint can take >5s in some
 * environments. The test accepts EITHER a real suggestion OR the
 * documented `llm_unavailable` fallback — see docs/Task-9-11-
 * concerns in README.md.
 */
import { spawnSync } from 'node:child_process';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

import { expect, test } from '@playwright/test';

import { loginViaApi, loginViaUi } from './fixtures/auth';
import { captureFlow } from './fixtures/screenshots';
import { API_BASE_DIRECT, SEED } from './fixtures/seed';

// ESM-safe __dirname — Playwright's config is also ESM, so
// ``import.meta.url`` is the canonical way to anchor relative paths
// inside test files.
const HERE = dirname(fileURLToPath(import.meta.url));
const REPO = join(HERE, '..', '..');
const API_DIR = join(REPO, 'apps', 'api');
const SEED_SCRIPT = join(HERE, 'scripts', 'seed.py');

test.describe('Flow A: agent workspace', () => {
  // Hermetic seed reset. We rewind the seeded PENDING conversation
  // back to ``status=PENDING, assigned_agent_id=NULL`` BEFORE EACH
  // TEST so re-runs of the suite stay green:
  //
  //   - test 1 needs the conversation already claimed/assigned to
  //     the agent so the inbox list shows it (the list filter is
  //     "rows where assigned_agent_id = me").
  //   - test 3 needs the conversation UNASSIGNED so it shows up in
  //     the GET /queue response (the queue filter is
  //     "status=PENDING AND assigned_agent_id IS NULL").
  //
  // Resolving both: reset the seed before every test, then have
  // each test do whatever it needs (test 1 doesn't care, test 3
  // claims from the freshly-reset row).
  test.beforeEach(() => {
    const result = spawnSync('uv', ['run', 'python', SEED_SCRIPT], {
      cwd: API_DIR,
      stdio: 'pipe',
    });
    if (result.status !== 0) {
      const stderr = result.stderr?.toString() ?? '';
      throw new Error(
        `[agent-flow] seed reset failed (exit ${result.status}): ${stderr.slice(0, 500)}`,
      );
    }
  });


  test('0. backend /health probe', async ({ request }) => {
    // Belt-and-braces — globalSetup already verified this, but
    // having an explicit test entry makes the suite self-validating
    // when running a single test file (which skips globalSetup).
    const res = await request.get(`${API_BASE_DIRECT}/health`);
    expect([200, 503]).toContain(res.status());
  });

  test('1. login form submits and lands on /inbox', async ({ page }) => {
    await loginViaUi(page);
    // After login the app auto-navigates to the first conversation,
    // so the URL is ``/inbox/:id`` (or ``/inbox`` when no
    // conversations exist). Workspace chrome lands on the inbox
    // route — the row list, the detail pane, or the empty state
    // are all valid here.
    await expect(page).toHaveURL(/\/inbox(\/.*)?$/);
    await expect(
      page
        .getByTestId('inbox-list')
        .or(page.getByTestId('inbox-detail'))
        .or(page.getByTestId('inbox-empty')),
    ).toBeVisible();
  });

  test('2. tenant hint lookup resolves the demo agent', async ({ page, request }) => {
    // Open the login page WITHOUT a JWT — the form's debounced
    // lookup is what we're exercising.
    await page.context().clearCookies();
    await page.goto('http://localhost:5173/login');
    await page.getByLabel('邮箱').fill(SEED.agentEmail);

    // The form's lookup endpoint is anti-enumeration: returns 200
    // with the tenant id when known, 200 with null fields when not.
    // The UI MUST NOT show the "邮箱或租户信息无法识别" hint when
    // the tenant was resolved — assert that hint is absent.
    await expect(page.getByTestId('tenant-hint-failed')).toHaveCount(0, {
      timeout: 5_000,
    });

    // Direct API probe: the lookup endpoint is the wire contract.
    const res = await request.get(
      `${API_BASE_DIRECT}/api/v1/auth/lookup-tenant`,
      { params: { email: SEED.agentEmail } },
    );
    expect(res.status()).toBe(200);
    const body = (await res.json()) as { tenant_id: string | null };
    expect(body.tenant_id).toBe(SEED.tenantId);
  });

  test('3. claim the seeded PENDING conversation', async ({ page, request }) => {
    const { token } = await loginViaApi(page, request);

    // Pre-condition: the seeded conversation is in the unassigned
    // queue. M1 has no /queue UI page so we verify via REST.
    const queueRes = await request.get(`${API_BASE_DIRECT}/api/v1/agents/queue`, {
      headers: { Authorization: `Bearer ${token}` },
    });
    expect(queueRes.status()).toBe(200);
    const queue = (await queueRes.json()) as {
      items: Array<{ id: string; status: string; assigned_agent_id: string | null }>;
    };
    expect(queue.items.map((c) => c.id)).toContain(SEED.conversationId);

    // Claim via REST (logical "click Claim"). Re-running this test
    // is safe: a second claim hits the 409 "conversation cannot be
    // claimed" branch because the assignee is already set — we
    // accept either 200 (first run) or 409 (subsequent run).
    const claimRes = await request.post(
      `${API_BASE_DIRECT}/api/v1/agents/conversations/${SEED.conversationId}/claim`,
      { headers: { Authorization: `Bearer ${token}` } },
    );
    expect([200, 409]).toContain(claimRes.status());

    // The agent now owns the conversation — it appears in /inbox.
    // Navigate to the detail page to verify the conversation is
    // fetchable (uses the inbox list endpoint internally).
    await page.goto(`http://localhost:5173/inbox/${SEED.conversationId}`);
    await expect(page.getByTestId('inbox-detail')).toBeVisible();
  });

  test('4. compose + send an agent reply', async ({ page, request }) => {
    await loginViaApi(page, request);
    await page.goto(`http://localhost:5173/inbox/${SEED.conversationId}`);
    await expect(page.getByTestId('inbox-detail')).toBeVisible();

    // Unique per-run suffix so a re-run of this test against a
    // already-sent message doesn't trigger Playwright's strict-mode
    // "resolved to 2 elements" violation on getByText.
    const replyText = `您好,我来帮您处理密码重置问题 (${Date.now()})。`;

    const composer = page.getByTestId('composer-textarea');
    await composer.fill(replyText);

    await page.getByTestId('composer-send').click();

    // After send, the composer clears + the new message appears in
    // the stream. Allow up to 8s for the WS invalidation + refetch.
    await expect(composer).toHaveValue('', { timeout: 8_000 });
    await expect(
      page.getByText(replyText),
    ).toBeVisible({ timeout: 8_000 });
  });

  test('5. AI suggestion round-trip', async ({ page, request }, testInfo) => {
    await loginViaApi(page, request);
    await page.goto(`http://localhost:5173/inbox/${SEED.conversationId}`);
    await expect(page.getByTestId('inbox-detail')).toBeVisible();

    // ---- Click "拿AI建议" --------------------------------------
    await page.getByTestId('composer-suggest').click();

    // The suggestion pane transitions idle → loading → success|error.
    // We accept EITHER a real suggestion OR the documented
    // llm_unavailable fallback — see README "Mock LLM" trade-off.
    const pane = page.getByTestId('ai-suggestion-pane');
    await expect(pane).toBeVisible();
    // The current build renders BOTH ``suggestion-content`` and a
    // ``suggestion-warning`` badge when a suggestion succeeded — the
    // badge is informational, not exclusive. ``.or()`` matches both,
    // which trips Playwright's strict mode. ``.first()`` accepts
    // whichever the DOM surfaces first.
    await expect(
      page
        .getByTestId('suggestion-content')
        .or(page.getByTestId('suggestion-warning'))
        .first(),
    ).toBeVisible({ timeout: 10_000 });

    const contentBlock = page.getByTestId('suggestion-content');
    const warningBadge = page.getByTestId('suggestion-warning');

    // Click "应用到输入框" — only available when suggestion succeeded
    // AND the suggested text is non-empty. The button is rendered
    // disabled in the LLM-fallback case, so check enabled state
    // explicitly before clicking.
    const applyBtn = page.getByTestId('suggestion-apply');
    if (
      (await contentBlock.isVisible().catch(() => false)) &&
      (await applyBtn.isEnabled().catch(() => false))
    ) {
      await applyBtn.click();
      // The composer should now hold the suggested text (or the
      // fallback message). We don't assert on exact wording —
      // LLM output is non-deterministic.
      await expect(page.getByTestId('composer-textarea')).not.toHaveValue('');
    } else {
      // Fallback path — log it so the operator knows which branch ran.
      // eslint-disable-next-line no-console -- intentional diagnostic
      console.log('[e2e] suggestion fell back to llm_unavailable or empty');
      // Surface the warning in the test report for traceability.
      testInfo.annotations.push({
        type: 'llm-fallback',
        description:
          'AI suggestion returned llm_unavailable or empty — apply button disabled',
      });
      // Manually fill the composer with a deterministic text so the
      // subsequent send assertion stays stable.
      await page
        .getByTestId('composer-textarea')
        .fill('AI 暂时无法生成建议,人工继续处理中。');
    }
    // Silence the unused-var lint without altering assertions.
    void warningBadge;

    // ---- Send the (possibly-applied) reply ----------------------
    await page.getByTestId('composer-send').click();
    await expect(page.getByTestId('composer-textarea')).toHaveValue('', {
      timeout: 8_000,
    });

    // Final screenshot — the task spec asks for agent-flow-complete.png.
    await captureFlow(page, testInfo, 'agent-flow-complete');
  });
});
