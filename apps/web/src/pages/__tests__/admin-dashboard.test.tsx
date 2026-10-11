import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';

import { AdminDashboardPage } from '@/pages/admin-dashboard';
import { apiClient, JWT_STORAGE_KEY } from '@/lib/api-client';
import { useCurrentUser } from '@/lib/use-current-user';

// Tier 2 Task 2.5 — Admin Dashboard page tests.

vi.mock('@/lib/api-client', async () => {
  // eslint-disable-next-line @typescript-eslint/consistent-type-imports
  const actual = await vi.importActual<typeof import('@/lib/api-client')>(
    '@/lib/api-client',
  );
  return {
    ...actual,
    apiClient: { get: vi.fn() },
  };
});

vi.mock('@/lib/use-current-user', () => ({
  useCurrentUser: vi.fn(),
}));

const mockedGet = vi.mocked(apiClient.get);
const mockedUseCurrentUser = vi.mocked(useCurrentUser);

const CURRENT_TENANT_ID = '01HZDEMO00000000000000000';

const SEED = {
  today_utc_start: '2026-10-11T00:00:00Z',
  tickets_today: 7,
  tickets_by_status: [
    { key: 'new', count: 4 },
    { key: 'in_progress', count: 2 },
    { key: 'resolved', count: 1 },
  ],
  tickets_by_priority: [
    { key: 'P0', count: 1 },
    { key: 'P2', count: 5 },
    { key: 'P3', count: 1 },
  ],
  conversations_open: 3,
  period: '2026-10',
  period_starts_at: '2026-10-01T00:00:00Z',
  llm_tokens_this_period: 12345,
  active_users: 4,
  total_users: 6,
};

beforeEach(() => {
  mockedGet.mockReset();
  window.localStorage.setItem(JWT_STORAGE_KEY, 'test-token');
  mockedUseCurrentUser.mockReturnValue({
    user: {
      user_id: 'admin-1',
      email: 'admin@example.com',
      tenant_id: CURRENT_TENANT_ID,
      tenant_name: 'Demo',
      role: 'admin',
    },
    isLoading: false,
    isError: false,
  });
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  window.localStorage.clear();
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
        <AdminDashboardPage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe('AdminDashboardPage (Tier 2 Task 2.5)', () => {
  it('renders all four KPI tiles with formatted counts', async () => {
    mockedGet.mockResolvedValue({ data: SEED });
    renderPage();

    expect(
      await screen.findByTestId('admin-dashboard-body'),
    ).toBeInTheDocument();
    expect(
      screen.getByTestId('dashboard-tile-tickets-today-value'),
    ).toHaveTextContent('7');
    expect(
      screen.getByTestId('dashboard-tile-conversations-open-value'),
    ).toHaveTextContent('3');
    expect(
      screen.getByTestId('dashboard-tile-llm-tokens-value'),
    ).toHaveTextContent(/\D*12,345\D*/);
    expect(
      screen.getByTestId('dashboard-tile-users-value'),
    ).toHaveTextContent('4');
    // Subtitle surfaces active vs total users + period + date anchor.
    expect(
      screen.getByTestId('dashboard-tile-users-subtitle'),
    ).toHaveTextContent(/共 6 人/);
    expect(
      screen.getByTestId('dashboard-tile-llm-tokens-subtitle'),
    ).toHaveTextContent('2026-10');
  });

  it('shows status + priority breakdown buckets, marking the largest as 最高', async () => {
    mockedGet.mockResolvedValue({ data: SEED });
    renderPage();

    const statusCard = await screen.findByTestId(
      'dashboard-breakdown-status',
    );
    expect(statusCard).toBeInTheDocument();
    // Status buckets are rendered.
    expect(
      screen.getByTestId('dashboard-breakdown-status-row-new'),
    ).toHaveTextContent('new');
    expect(
      screen.getByTestId('dashboard-breakdown-status-row-in_progress'),
    ).toHaveTextContent('in_progress');
    // "new" is the largest → badge 最高 appears next to it.
    const newRow = screen.getByTestId(
      'dashboard-breakdown-status-row-new',
    );
    expect(newRow.textContent).toContain('最高');

    // Priority card total reflects sum across buckets (1+5+1 = 7).
    expect(
      screen.getByTestId('dashboard-breakdown-priority-total'),
    ).toHaveTextContent(/总工单 · 7/);
  });

  it('shows empty-state copy if the tenant has no tickets', async () => {
    mockedGet.mockResolvedValue({
      data: { ...SEED, tickets_by_status: [], tickets_by_priority: [] },
    });
    renderPage();
    expect(
      await screen.findByTestId('dashboard-breakdown-status-empty'),
    ).toHaveTextContent('暂无工单');
    expect(
      screen.getByTestId('dashboard-breakdown-priority-empty'),
    ).toHaveTextContent('暂无工单');
  });

  it('calls the dashboard endpoint with the current tenant id', async () => {
    mockedGet.mockResolvedValue({ data: SEED });
    renderPage();
    await screen.findByTestId('admin-dashboard-body');
    expect(mockedGet).toHaveBeenCalledWith(
      `/api/v1/admin/tenants/${CURRENT_TENANT_ID}/dashboard`,
    );
  });

  it('shows the loading state while the GET is pending', async () => {
    mockedGet.mockReturnValue(new Promise(() => {}));
    renderPage();
    expect(
      await screen.findByTestId('dashboard-loading'),
    ).toHaveTextContent('加载中');
  });

  it('shows an error state when the GET fails', async () => {
    const err = new Error('Network Error');
    (err as unknown as { isAxiosError?: boolean }).isAxiosError = true;
    Object.defineProperty(err, 'message', { value: 'Network Error' });
    mockedGet.mockRejectedValue(err);
    renderPage();
    expect(
      await screen.findByTestId('dashboard-error'),
    ).toHaveTextContent(/加载失败/);
  });

  it('refetches when the user clicks the refresh button', async () => {
    mockedGet
      .mockResolvedValueOnce({ data: SEED })
      .mockResolvedValueOnce({ data: { ...SEED, tickets_today: 99 } });
    renderPage();
    await screen.findByTestId('admin-dashboard-body');
    expect(
      screen.getByTestId('dashboard-tile-tickets-today-value'),
    ).toHaveTextContent('7');
    mockedGet.mockClear();
    const user = (await import('@testing-library/user-event')).default.setup();
    await user.click(screen.getByTestId('dashboard-refresh'));
    await waitFor(() => expect(mockedGet).toHaveBeenCalledTimes(1));
    await waitFor(() =>
      expect(
        screen.getByTestId('dashboard-tile-tickets-today-value'),
      ).toHaveTextContent('99'),
    );
  });
});