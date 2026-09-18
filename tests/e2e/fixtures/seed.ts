/**
 * Demo seed constants — mirror tests/e2e/scripts/seed.py so the
 * Playwright side can assert against the same identifiers.
 *
 * The Python script is the source of truth (it owns the actual DB
 * rows). This TS file only declares the contract; mismatches will
 * surface as 404s in the tests, not silent falls-through.
 */
export const SEED = {
  tenantId: '01HZDEMO00000000000000000',
  agentUserId: '01HZDEMO00000000000000001',
  adminUserId: '01HZDEMO00000000000000002',
  webChannelId: '01HZDEMO00000000000000003',
  conversationId: '01HZDEMO00000000000000004',

  agentEmail: 'agent@example.com',
  adminEmail: 'admin@example.com',
  password: 'Demo123!',

  customerExternalId: 'e2e-visitor-001',
} as const;

export const API_BASE_DIRECT = 'http://localhost:8000';
export const API_BASE_VIA_PROXY = 'http://localhost:5173';
export const WIDGET_HOST_URL = 'http://localhost:8080/widget-host.html';
