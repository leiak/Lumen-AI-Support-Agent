import { describe, expect, it, vi, beforeEach } from 'vitest';
import { cleanup, render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';

import { AppShell } from '@/components/layout/app-shell';

// The badge used to be a hardcoded `<span>demo</span>` regardless of
// which tenant was logged in. After the fix, the badge text comes from
// ``useCurrentUser().tenant_name`` (the /me response includes it), with
// a "加载中…" fallback while the query is in-flight and "demo" only as
// the last-resort no-token fallback.

const useCurrentUserMock = vi.fn();
const useIsAdminMock = vi.fn();

vi.mock('@/lib/use-current-user', () => ({
  useCurrentUser: () => useCurrentUserMock(),
  CURRENT_USER_QUERY_KEY: ['agents', 'me'],
}));

vi.mock('@/lib/use-is-admin', () => ({
  useIsAdmin: () => useIsAdminMock(),
}));

const mockNavigate = vi.fn();
vi.mock('react-router-dom', async () => {
  const actual = await vi.importActual<typeof import('react-router-dom')>(
    'react-router-dom',
  );
  return {
    ...actual,
    useNavigate: () => mockNavigate,
  };
});

function renderShell(): void {
  render(
    <MemoryRouter>
      <AppShell />
    </MemoryRouter>,
  );
}

describe('AppShell — tenant-name badge', () => {
  beforeEach(() => {
    cleanup();
    vi.clearAllMocks();
    // Default: agent (not admin) so the admin nav items don't appear in
    // these tests; the badge is independent of role.
    useIsAdminMock.mockReturnValue({ isAdmin: false });
  });

  it('shows the actual tenant_name from /me when the user is loaded', () => {
    useCurrentUserMock.mockReturnValue({
      user: {
        user_id: '01HZDEMO00000000000000001',
        email: 'agent@example.com',
        tenant_id: '01HZDEMO00000000000000000',
        tenant_name: 'Acme Demo',
        role: 'agent',
      },
      isLoading: false,
      isError: false,
    });

    renderShell();

    const badge = screen.getByTestId('tenant-name-badge');
    expect(badge.textContent).toBe('Acme Demo');
    expect(badge.getAttribute('data-tenant')).toBe('01HZDEMO00000000000000000');
    expect(badge.getAttribute('data-loading')).toBe('false');
  });

  it('shows "加载中…" while the /me query is in-flight', () => {
    useCurrentUserMock.mockReturnValue({
      user: null,
      isLoading: true,
      isError: false,
    });

    renderShell();

    const badge = screen.getByTestId('tenant-name-badge');
    expect(badge.textContent).toBe('加载中…');
    expect(badge.getAttribute('data-loading')).toBe('true');
  });

  it('falls back to "demo" when there is no user and no loading state', () => {
    // No token in localStorage → useCurrentUser short-circuits to
    // { user: null, isLoading: false }. The badge should NOT show the
    // hardcoded "demo" forever — but until /me resolves it's the only
    // honest fallback we have.
    useCurrentUserMock.mockReturnValue({
      user: null,
      isLoading: false,
      isError: false,
    });

    renderShell();

    const badge = screen.getByTestId('tenant-name-badge');
    expect(badge.textContent).toBe('demo');
  });

  it('reflects the per-tenant tenant_name (not a hardcoded literal)', () => {
    // Regression: a different tenant must show its own name, not
    // "Acme Demo" or "demo". This is the core bug the test guards
    // against — before the fix the badge was hardcoded.
    useCurrentUserMock.mockReturnValue({
      user: {
        user_id: '01M4SBR3EMV4F5KQDZJ2A2903C',
        email: 'budget@example.com',
        tenant_id: '01M4SBR3EMV4F5KQDZJ2A2903C',
        tenant_name: 'Budget Test 1',
        role: 'admin',
      },
      isLoading: false,
      isError: false,
    });

    renderShell();

    const badge = screen.getByTestId('tenant-name-badge');
    expect(badge.textContent).toBe('Budget Test 1');
    expect(badge.textContent).not.toBe('demo');
  });
});
