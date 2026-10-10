import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';

import { AdminSlaPoliciesPage } from '@/pages/admin-sla-policies';
import { apiClient } from '@/lib/api-client';

// useCurrentUser reads the JWT from localStorage + calls /api/v1/agents/me.
// Stub it so the page renders with a known tenant_id regardless of the
// test environment's auth state.
vi.mock('@/lib/use-current-user', () => ({
  useCurrentUser: () => ({
    user: {
      user_id: 'admin-1',
      email: 'admin@example.com',
      tenant_id: 'tenant-abc',
      tenant_name: 'Test Tenant',
      role: 'admin',
    },
    isLoading: false,
    isError: false,
  }),
}));

vi.mock('@/lib/api-client', async () => {
  const actual = await vi.importActual<typeof import('@/lib/api-client')>(
    '@/lib/api-client',
  );
  return {
    ...actual,
    apiClient: { get: vi.fn(), post: vi.fn() },
  };
});

const mockedGet = vi.mocked(apiClient.get);

beforeEach(() => {
  mockedGet.mockReset();
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

function makeQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: { retry: false, staleTime: 0, refetchOnWindowFocus: false },
    },
  });
}

function renderPage(): void {
  render(
    <QueryClientProvider client={makeQueryClient()}>
      <MemoryRouter>
        <AdminSlaPoliciesPage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

const SEED_POLICIES = [
  {
    id: 'p0-id',
    name: 'P0 Critical',
    priority: 'P0',
    first_response_minutes: 15,
    resolution_minutes: 240,
    business_hours_only: false,
    created_at: '2026-10-10T00:00:00Z',
  },
  {
    id: 'p1-id',
    name: 'P1 High',
    priority: 'P1',
    first_response_minutes: 60,
    resolution_minutes: 480,
    business_hours_only: false,
    created_at: '2026-10-10T00:00:00Z',
  },
  {
    id: 'p2-id',
    name: 'P2 Normal',
    priority: 'P2',
    first_response_minutes: 240,
    resolution_minutes: 1440,
    business_hours_only: false,
    created_at: '2026-10-10T00:00:00Z',
  },
  {
    id: 'p3-id',
    name: 'P3 Low',
    priority: 'P3',
    first_response_minutes: 1440,
    resolution_minutes: 4320,
    business_hours_only: true,
    created_at: '2026-10-10T00:00:00Z',
  },
];

describe('AdminSlaPoliciesPage', () => {
  it('renders all 4 policies with priority, name, and SLA minute values', async () => {
    mockedGet.mockResolvedValue({ data: SEED_POLICIES });

    renderPage();

    await waitFor(() => {
      expect(screen.getByTestId('sla-policy-row-P0')).toBeInTheDocument();
    });
    expect(mockedGet).toHaveBeenCalledWith(
      '/api/v1/admin/tenants/tenant-abc/sla-policies',
    );

    // All 4 priority rows render.
    for (const p of ['P0', 'P1', 'P2', 'P3'] as const) {
      expect(screen.getByTestId(`sla-policy-row-${p}`)).toBeInTheDocument();
      expect(screen.getByTestId(`sla-priority-badge-${p}`)).toHaveTextContent(p);
      expect(screen.getByTestId(`sla-policy-response-${p}`)).toHaveTextContent(
        /首响 \d+ 分钟/,
      );
      expect(screen.getByTestId(`sla-policy-resolution-${p}`)).toHaveTextContent(
        /解决 \d+ 分钟/,
      );
    }

    // P0 — values + 24/7 indicator.
    expect(screen.getByTestId('sla-policy-response-P0')).toHaveTextContent('首响 15 分钟');
    expect(screen.getByTestId('sla-policy-resolution-P0')).toHaveTextContent('解决 240 分钟');
    expect(screen.getByTestId('sla-policy-hours-P0')).toHaveTextContent('全天');

    // P3 — values + business-hours indicator.
    expect(screen.getByTestId('sla-policy-response-P3')).toHaveTextContent('首响 1440 分钟');
    expect(screen.getByTestId('sla-policy-resolution-P3')).toHaveTextContent('解决 4320 分钟');
    expect(screen.getByTestId('sla-policy-hours-P3')).toHaveTextContent('仅工作时间');

    // Names are wired through.
    expect(screen.getByTestId('sla-policy-name-P2')).toHaveTextContent('P2 Normal');
  });

  it('shows loading state while fetching', async () => {
    // Promise we never resolve so the loading state stays visible.
    mockedGet.mockReturnValue(new Promise(() => {}));

    renderPage();

    expect(await screen.findByTestId('sla-policies-loading')).toHaveTextContent(
      '加载中',
    );
    // Loading state must NOT be paired with table rows.
    expect(screen.queryByTestId('sla-policy-row-P0')).not.toBeInTheDocument();
  });

  it('shows error state with API failure message', async () => {
    const err = new Error('Network Error');
    (err as unknown as { isAxiosError?: boolean }).isAxiosError = true;
    // axios-style shape so Error.message reaches the renderer.
    Object.defineProperty(err, 'message', { value: 'Network Error' });
    mockedGet.mockRejectedValue(err);

    renderPage();

    const alert = await screen.findByTestId('sla-policies-error');
    expect(alert).toHaveTextContent(/加载失败/);
    expect(alert).toHaveTextContent('Network Error');
    expect(screen.queryByTestId('sla-policy-row-P0')).not.toBeInTheDocument();
  });

  it('shows empty message when API returns no policies', async () => {
    mockedGet.mockResolvedValue({ data: [] });

    renderPage();

    await waitFor(() => {
      expect(screen.getByTestId('sla-policies-empty')).toBeInTheDocument();
    });
    expect(screen.queryByTestId('sla-policy-row-P0')).not.toBeInTheDocument();
  });
});
