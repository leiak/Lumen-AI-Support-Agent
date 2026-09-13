/**
 * Shared local-storage keys — kept in a separate file so auth.ts
 * can be imported from places (seed.ts) that themselves don't need
 * to pull Playwright.
 *
 * MUST stay in sync with `apps/web/src/lib/api-client.ts`:
 *   export const JWT_STORAGE_KEY = 'lumen.jwt';
 */
export const JWT_STORAGE_KEY = 'lumen.jwt';
