import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';

import { AdminBudgetPage } from '@/pages/admin-budget';
import { apiClient, JWT_STORAGE_KEY } from '@/lib/api-client';
import { useCurrentUser } from '@/lib/use-current-user';
import type { BudgetUsage } from '@/lib/budget';

vi.mock('@/lib/api-client', async () => {
  // eslint-disable-next-line @typescript-eslint/consistent-type-imports
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
const mockedPost = vi.mocked(apiClient.post);
const mockedUseCurrentUser = vi.mocked(useCurrentUser);

beforeEach(() => {
  mockedGet.mockReset();
  mockedPost.mockReset();
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

const sampleUsage: BudgetUsage = {
  period: '2026-10',
  tokens_used: 13150,
  soft_warn_tokens: 10000,
  hard_cap_tokens: 20000,
  period_starts_at: '2026-10-01T00:00:00Z',
  effective_cap: 30500,
  credits_total: 10500,
  breakdown: null,
};

const sampleCredits = { credits: [], total_tokens: 10500 };

function setupHappyReads(): void {
  mockedGet.mockImplementation((url) => {
    if (typeof url === 'string' && url.endsWith('/budget')) {
      return Promise.resolve({ data: sampleBudget });
    }
    if (typeof url === 'string' && url.includes('/budget/usage')) {
      return Promise.resolve({ data: sampleUsage });
    }
    if (typeof url === 'string' && url.includes('/credits')) {
      return Promise.resolve({ data: sampleCredits });
    }
    return Promise.reject(new Error(`unexpected GET ${String(url)}`));
  });
}

describe('AdminBudgetPage — mutation UI (Tier 1 Task 1.4)', () => {
  it('renders 编辑预算 and 发放 Credit buttons next to their cards', async () => {
    setupHappyReads();
    renderPage();
    expect(await screen.findByTestId('budget-edit-open')).toBeInTheDocument();
    expect(screen.getByTestId('credit-grant-open')).toBeInTheDocument();
  });

  it('opens the edit dialog and pre-fills from the loaded budget', async () => {
    setupHappyReads();
    renderPage();
    const user = userEvent.setup();
    // The button is `disabled={!budgetQuery.data}` so wait for it to
    // become enabled before clicking — otherwise the click is a no-op
    // and the dialog never opens.
    const openBtn = await screen.findByTestId('budget-edit-open');
    await waitFor(() => expect(openBtn).not.toBeDisabled());
    await user.click(openBtn);

    await screen.findByTestId('budget-edit-dialog');
    // Inputs are pre-filled with the loaded budget values.
    expect(screen.getByTestId('budget-edit-soft-warn')).toHaveValue(10000);
    expect(screen.getByTestId('budget-edit-hard-cap')).toHaveValue(20000);
    expect(screen.getByTestId('budget-edit-tz')).toHaveValue('UTC');
  });

  it('disables the submit button when hard_cap < soft_warn and shows an error', async () => {
    setupHappyReads();
    renderPage();
    const user = userEvent.setup();
    const openBtn = await screen.findByTestId('budget-edit-open');
    await waitFor(() => expect(openBtn).not.toBeDisabled());
    await user.click(openBtn);
    await screen.findByTestId('budget-edit-dialog');

    // soft_warn > hard_cap should disable the submit button.
    const softWarn = screen.getByTestId('budget-edit-soft-warn');
    const hardCap = screen.getByTestId('budget-edit-hard-cap');
    await user.clear(softWarn);
    await user.type(softWarn, '15000');
    await user.clear(hardCap);
    await user.type(hardCap, '10000');

    expect(screen.getByTestId('budget-edit-submit')).toBeDisabled();
    expect(screen.getByTestId('budget-edit-error')).toBeInTheDocument();
    expect(mockedPost).not.toHaveBeenCalled();
  });

  it('opens the credit-grant dialog with a default note + period', async () => {
    setupHappyReads();
    renderPage();
    const user = userEvent.setup();
    await user.click(screen.getByTestId('credit-grant-open'));
    await screen.findByTestId('credit-grant-dialog');

    // Period is shown but read-only; only tokens + note are editable.
    expect(screen.getByTestId('credit-grant-tokens')).toHaveValue(5000);
    expect(screen.getByTestId('credit-grant-note')).toHaveValue('');
  });
});
