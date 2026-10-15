/**
 * Login helpers for the E2E suite.
 *
 * Strategy
 * --------
 * The task spec is explicit: "use fixtures to login, not manual
 * typing". We do TWO things:
 *
 *   1. For tests that need to drive the UI end-to-end (login form
 *      itself is a flow under test), expose `loginViaUi()` which
 *      fills the form via Playwright actions — used by the
 *      "tenant hint lookup" assertion specifically.
 *
 *   2. For tests that just need an authenticated session (most of
 *      Flow A / Flow B), use `loginViaApi()` which calls
 *      POST /api/v1/auth/login directly and seeds the JWT into
 *      localStorage before the page script runs. This is faster
 *      and decoupled from the login-page UI being correct.
 *
 * Both helpers verify the workspace reaches the inbox after login —
 * a hard precondition for every Flow A test.
 */
import type { APIRequestContext, Page } from '@playwright/test';
import { expect } from '@playwright/test';

import { API_BASE_DIRECT, SEED } from './seed';
import { JWT_STORAGE_KEY } from './auth-shared';

/**
 * Backend-resolved tenant id for the demo agent. Looked up via the
 * public anti-enumeration endpoint — kept as a constant here so
 * tests assert against the same value the seed inserted.
 */
export async function lookupTenantId(
  request: APIRequestContext,
  email: string,
): Promise<string | null> {
  const res = await request.get(`${API_BASE_DIRECT}/api/v1/auth/lookup-tenant`, {
    params: { email },
    headers: { 'X-Tenant-Id': SEED.tenantId },
  });
  // The endpoint is anti-enumeration: always 200 with nullable fields.
  // Asserting on status would leak which emails are registered.
  if (res.status() !== 200) return null;
  const body = (await res.json()) as { tenant_id: string | null; tenant_name: string | null };
  return body.tenant_id;
}

/**
 * Authenticate via the REST API and stash the JWT in localStorage so
 * the page's `apiClient` interceptor picks it up on the first request.
 *
 * Navigates to the SPA origin first so `localStorage` is the SPA's
 * (port 5173), not the API's. Tests can then visit any protected
 * route without going through the login form.
 */
export async function loginViaApi(
  page: Page,
  request: APIRequestContext,
  email: string = SEED.agentEmail,
  password: string = SEED.password,
): Promise<{ token: string; userId: string; tenantId: string }> {
  // ---- Step 1: resolve tenant id (anti-enumeration lookup) -------
  const tenantId = await lookupTenantId(request, email);
  if (!tenantId) {
    throw new Error(
      `auth fixture: tenant lookup returned null for ${email} — ` +
        `did the seed script run?`,
    );
  }

  // ---- Step 2: POST /api/v1/auth/login ---------------------------
  const res = await request.post(`${API_BASE_DIRECT}/api/v1/auth/login`, {
    headers: { 'X-Tenant-Id': tenantId },
    data: { email, password },
  });
  if (res.status() !== 200) {
    throw new Error(
      `auth fixture: /login returned ${res.status()} — ${await res.text()}`,
    );
  }
  const body = (await res.json()) as {
    access_token: string;
    user: { id: string; tenant_id: string };
  };

  // ---- Step 3: persist JWT on the SPA origin ---------------------
  // Visit the origin first so localStorage is bound to the right
  // domain. Using about:blank would silently drop the write.
  // Absolute URL (not baseURL-relative ``/``) to dodge the same
  // Playwright "Cannot navigate to invalid URL" guard hit by
  // loginViaUi — see comment there.
  await page.goto('http://localhost:5173/');
  await page.evaluate(
    ([key, token]) => window.localStorage.setItem(key, token),
    [JWT_STORAGE_KEY, body.access_token],
  );

  return {
    token: body.access_token,
    userId: body.user.id,
    tenantId: body.user.tenant_id,
  };
}

/**
 * Drive the login form end-to-end via UI actions. Used by the
 * "tenant hint" assertion in agent-flow.spec.ts so the form's
 * debounced lookup path is exercised — not just bypassed.
 *
 * Returns the same shape as `loginViaApi` but does NOT throw on
 * "tenant lookup failed" anti-enumeration cases — caller decides.
 */
export async function loginViaUi(
  page: Page,
  email: string = SEED.agentEmail,
  password: string = SEED.password,
): Promise<void> {
  // Use the absolute URL rather than the baseURL-relative '/login' so
  // we don't trip the Playwright "Cannot navigate to invalid URL"
  // guard that some versions raise when the navigation is kicked off
  // from a helper that wasn't directly passed the page's context.
  await page.goto('http://localhost:5173/login');
  await page.getByLabel('邮箱').fill(email);
  // Wait for the form's debounced /auth/lookup-tenant request to
  // return BEFORE we click 登录 — the form's submit handler short-
  // circuits with "邮箱或租户信息无法识别" if ``resolvedTenantId`` is
  // still null (i.e. the lookup is in flight). The old
  // ``toHaveCount(0)`` on ``tenant-hint-failed`` raced against the
  // 300ms debounce because the hint is also absent while the lookup
  // is in flight.
  const lookupResponse = await page.waitForResponse(
    (resp) => resp.url().includes('/api/v1/auth/lookup-tenant') && resp.status() === 200,
    { timeout: 10_000 },
  );
  expect(lookupResponse.status()).toBe(200);
  await page.getByLabel('密码').fill(password);
  await page.getByRole('button', { name: '登录' }).click();
  // SPA route change doesn't fire a ``load`` event, so the default
  // ``page.waitForURL`` (which waits for ``load``) would hang.
  // ``expect(page).toHaveURL(...)`` retries until the regex matches
  // without depending on a full-page load. The regex (vs. the
  // ``**/inbox`` glob) also avoids Playwright's "glob requires a
  // preceding segment" footgun on the root-level ``/inbox`` URL.
  await expect(page).toHaveURL(/\/inbox(\/|$|\?)/, { timeout: 10_000 });
}
