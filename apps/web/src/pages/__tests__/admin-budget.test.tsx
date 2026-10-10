import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { AxiosError } from 'axios';

import { AdminBudgetPage } from '@/pages/admin-budget';
import { apiClient, JWT_STORAGE_KEY } from '@/lib/api-client';
import { useCurrentUser } from '@/lib/use-current-user';
import type { BudgetUsage } from '@/lib/budget';

vi.mock('@/lib/api-client', async () => {
  const actual = await vi.importActual<typeof import('@/lib/api-client')>(
    '@/lib/api-client',
  );
  return {
    ...actual,
    apiClient: { get: vi.fn(), post: vi.fn() },
  };
});

vi.mock('@/lib/use-current-user', () => ({
  useCurrentUser: vi.fn(),
}));

const mockedGet = vi.mocked(apiClient.get);
const mockedUseCurrentUser = vi.mocked(useCurrentUser);

beforeEach(() => {
  mockedGet.mockReset();
  window.localStorage.setItem(JWT_STORAGE_KEY, 'test-token');
  mockedUseCurrentUser.mockReturnValue({
    user: {
      user_id: 'admin-1',
      email: 'admin@example.com',
      tenant_id: '01HZDEMO00000000000000000',
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
        <AdminBudgetPage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

const sampleBudget = {
  soft_warn_tokens: 10000,
  hard_cap_tokens: 20000,
  period_anchor_tz: 'UTC',
  updated_at: '2026-10-07T03:51:20.546554Z',
};

// Mirrors the live seeded snapshot — 13150 tokens used, effective_cap
// 30500 (20000 hard + 10500 credits). 13150 / 30500 ≈ 43.1%, which is
// the threshold-band validation in the "renders all four stat tiles"
// test below.
const sampleUsage: BudgetUsage = {
  period: '2026-10',
  tokens_used: 13150,
  soft_warn_tokens: 10000,
  hard_cap_tokens: 20000,
  period_starts_at: '2026-10-01T00:00:00Z',
  effective_cap: 30500,
  credits_total: 10500,
  breakdown: [
    {
      provider: 'anthropic',
      model: 'claude-3-5-sonnet-20241022',
      prompt_tokens: 6100,
      completion_tokens: 1800,
      total_tokens: 7900,
      request_count: 6,
    },
    {
      provider: 'anthropic',
      model: 'claude-haiku-4-5',
      prompt_tokens: 1000,
      completion_tokens: 500,
      total_tokens: 1500,
      request_count: 2,
    },
    {
      provider: 'openai',
      model: 'gpt-4o-mini',
      prompt_tokens: 2500,
      completion_tokens: 1250,
      total_tokens: 3750,
      request_count: 3,
    },
  ],
};

const sampleCredits = {
  credits: [
    {
      id: 'c1',
      tenant_id: '01HZDEMO00000000000000000',
      period: '2026-10',
      tokens: 5000,
      note: 'Demo seed — initial top-up',
      granted_by: 'super-1',
      created_at: '2026-10-07T03:51:20.546554Z',
    },
    {
      id: 'c2',
      tenant_id: '01HZDEMO00000000000000000',
      period: '2026-10',
      tokens: 3000,
      note: 'Demo seed — follow-up top-up',
      granted_by: 'super-1',
      created_at: '2026-10-07T03:55:00.000000Z',
    },
  ],
  total_tokens: 8000,
};

function mockBudgetEndpoints(opts: {
  budget?: typeof sampleBudget;
  usage?: typeof sampleUsage | null;
  credits?: typeof sampleCredits | null;
  usageError?: boolean;
  creditsError?: boolean;
}): void {
  const usagePayload = opts.usageError
    ? null
    : (opts.usage ?? sampleUsage);
  const creditsPayload = opts.creditsError
    ? null
    : (opts.credits ?? sampleCredits);

  mockedGet.mockImplementation((url) => {
    const u = typeof url === 'string' ? url : (url as URL).toString();
    if (u.endsWith(`/budget`) && !u.includes('/usage') && !u.includes('/credits')) {
      return Promise.resolve({ data: opts.budget ?? sampleBudget });
    }
    if (u.includes('/budget/usage')) {
      if (opts.usageError) {
        return Promise.reject(
          new AxiosError('boom', undefined, undefined, undefined, {
            status: 500,
            data: { detail: 'usage blew up' },
            statusText: 'Internal Server Error',
            headers: {},
            config: {} as never,
          }),
        );
      }
      return Promise.resolve({ data: usagePayload });
    }
    if (u.includes('/credits')) {
      if (opts.creditsError) {
        return Promise.reject(
          new AxiosError('forbidden', undefined, undefined, undefined, {
            status: 404,
            data: { detail: 'not found' },
            statusText: 'Not Found',
            headers: {},
            config: {} as never,
          }),
        );
      }
      return Promise.resolve({ data: creditsPayload });
    }
    return Promise.reject(new Error(`unexpected GET ${u}`));
  });
}

describe('AdminBudgetPage', () => {
  it('renders all four stat tiles with the values from /usage', async () => {
    mockBudgetEndpoints({});
    renderPage();

    // The four tiles appear once usage resolves.
    const hardCap = await screen.findByTestId('budget-stat-hard-cap');
    expect(hardCap).toBeInTheDocument();
    expect(screen.getByTestId('budget-stat-hard-cap-value')).toHaveTextContent('20,000');

    const used = screen.getByTestId('budget-stat-tokens-used');
    expect(used).toBeInTheDocument();
    expect(screen.getByTestId('budget-stat-tokens-used-value')).toHaveTextContent('13,150');

    const effective = screen.getByTestId('budget-stat-effective-cap');
    expect(effective).toBeInTheDocument();
    expect(screen.getByTestId('budget-stat-effective-cap-value')).toHaveTextContent('30,500');
    expect(screen.getByTestId('budget-stat-effective-cap-subtitle')).toHaveTextContent(
      '+10,500 credits',
    );

    const percent = screen.getByTestId('budget-stat-percent');
    expect(percent).toBeInTheDocument();
    // 13150 / 30500 = 0.4311… → "43.1%"
    expect(screen.getByTestId('budget-stat-percent-value')).toHaveTextContent('43.1%');

    // The band picker: tokens_used=13150 ≥ soft_warn=10000, < hard_cap=20000.
    // Expected text class: warn (yellow).
    expect(
      screen.getByTestId('budget-stat-tokens-used-value').className,
    ).toMatch(/text-yellow-600/);
    expect(
      screen.getByTestId('budget-stat-percent-bar').className,
    ).toMatch(/bg-yellow-500/);

    // The /usage request explicitly carries ?breakdown=true so the page
    // can render the per-model table.
    await waitFor(() => {
      expect(mockedGet).toHaveBeenCalledWith(
        '/api/v1/admin/tenants/01HZDEMO00000000000000000/budget/usage',
        { params: { breakdown: true } },
      );
    });
  });

  it('renders the breakdown table when breakdown data is present', async () => {
    mockBudgetEndpoints({});
    renderPage();

    await screen.findByTestId('budget-breakdown-table');
    expect(
      screen.getByTestId('budget-breakdown-row-claude-3-5-sonnet-20241022'),
    ).toBeInTheDocument();
    expect(
      screen.getByTestId('budget-breakdown-row-claude-haiku-4-5'),
    ).toBeInTheDocument();
    expect(
      screen.getByTestId('budget-breakdown-row-gpt-4o-mini'),
    ).toBeInTheDocument();

    // claude-3-5-sonnet is 7900/13150 ≈ 60.1%
    expect(
      screen
        .getByTestId('budget-breakdown-row-claude-3-5-sonnet-20241022')
        .textContent ?? '',
    ).toMatch(/60\.[01]%/);
  });

  it('hides the breakdown table when breakdown is null', async () => {
    mockBudgetEndpoints({ usage: { ...sampleUsage, breakdown: null } });
    renderPage();

    // Wait for usage to settle so we're not racing the conditional.
    await screen.findByTestId('budget-stat-hard-cap');
    expect(screen.queryByTestId('budget-breakdown-table')).not.toBeInTheDocument();
  });

  it('renders the credits list with one row per grant', async () => {
    mockBudgetEndpoints({});
    renderPage();

    await screen.findByTestId('budget-credits-table');
    expect(screen.getByTestId('budget-credit-row-c1')).toBeInTheDocument();
    expect(screen.getByTestId('budget-credit-row-c2')).toBeInTheDocument();
    expect(screen.getByTestId('budget-credit-row-c1').textContent ?? '').toContain(
      'Demo seed — initial top-up',
    );

    // Credits request carries the current period as YYYY-MM.
    await waitFor(() => {
      expect(mockedGet).toHaveBeenCalledWith(
        '/api/v1/admin/tenants/01HZDEMO00000000000000000/credits',
        { params: { period: '2026-10' } },
      );
    });
  });

  it('shows the empty-state copy when credits list is empty', async () => {
    mockBudgetEndpoints({
      credits: { credits: [], total_tokens: 0 },
    });
    renderPage();

    await screen.findByTestId('budget-credits-empty');
    expect(screen.getByTestId('budget-credits-empty')).toHaveTextContent('暂无 credits');
    expect(screen.queryByTestId('budget-credits-table')).not.toBeInTheDocument();
  });

  it('shows the empty-state copy when the credits endpoint returns 404', async () => {
    // Per-tenant admins get a 404 from the super-admin-only /credits
    // endpoint. The page must degrade gracefully, not crash.
    mockBudgetEndpoints({ creditsError: true });
    renderPage();

    await screen.findByTestId('budget-credits-empty');
    expect(screen.getByTestId('budget-credits-empty')).toHaveTextContent('暂无 credits');
  });

  it('shows the error message when /usage fails', async () => {
    mockBudgetEndpoints({ usageError: true });
    renderPage();

    await screen.findByTestId('budget-usage-error');
    expect(screen.getByTestId('budget-usage-error').textContent ?? '').toContain(
      'usage blew up',
    );
    // Stat tiles must not appear when the source-of-truth failed.
    expect(screen.queryByTestId('budget-stat-hard-cap')).not.toBeInTheDocument();
  });
});
