import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';

import { AdminTenantSettingsPage } from '@/pages/admin-tenant-settings';
import { apiClient, JWT_STORAGE_KEY } from '@/lib/api-client';
import { useCurrentUser } from '@/lib/use-current-user';

// Tier 2 Task 2.3 — Tenant self-settings page tests.

vi.mock('@/lib/api-client', async () => {
  // eslint-disable-next-line @typescript-eslint/consistent-type-imports
  const actual = await vi.importActual<typeof import('@/lib/api-client')>(
    '@/lib/api-client',
  );
  return {
    ...actual,
    apiClient: { get: vi.fn(), patch: vi.fn() },
  };
});

vi.mock('@/lib/use-current-user', () => ({
  useCurrentUser: vi.fn(),
}));

const mockedGet = vi.mocked(apiClient.get);
const mockedPatch = vi.mocked(apiClient.patch);
const mockedUseCurrentUser = vi.mocked(useCurrentUser);

const CURRENT_TENANT_ID = '01HZDEMO00000000000000000';

const SEED = {
  id: CURRENT_TENANT_ID,
  name: 'Acme Corp',
  plan: 'pro' as const,
  status: 'active' as const,
  created_at: '2026-09-01T00:00:00Z',
  updated_at: '2026-10-01T00:00:00Z',
};

beforeEach(() => {
  mockedGet.mockReset();
  mockedPatch.mockReset();
  window.localStorage.setItem(JWT_STORAGE_KEY, 'test-token');
  mockedUseCurrentUser.mockReturnValue({
    user: {
      user_id: 'admin-1',
      email: 'admin@example.com',
      tenant_id: CURRENT_TENANT_ID,
      tenant_name: 'Acme Corp',
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
        <AdminTenantSettingsPage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

/**
 * Wire `apiClient.get` so the tenant-settings and the new
 * assignment-policy endpoints both resolve. The legacy tests
 * never read the policy card so we just return the default
 * opt-out shape; tests that care about the policy card override
 * this.
 */
const DEFAULT_POLICY = { enabled: false, strategy: 'manual' } as const;

function installGetRouter(): void {
  mockedGet.mockImplementation((url: unknown) => {
    const u = typeof url === 'string' ? url : '';
    if (u.endsWith('/assignment-policy')) {
      return Promise.resolve({ data: DEFAULT_POLICY });
    }
    return Promise.resolve({ data: SEED });
  });
}

describe('AdminTenantSettingsPage (Tier 2 Task 2.3)', () => {
  it('renders the summary card with name + plan + status badges', async () => {
    installGetRouter();
    renderPage();
    expect(await screen.findByTestId('tenant-settings-summary')).toBeInTheDocument();
    expect(screen.getByTestId('tenant-plan-badge-pro')).toBeInTheDocument();
    expect(screen.getByTestId('tenant-status-badge-active')).toBeInTheDocument();
    expect(screen.getByTestId('tenant-settings-id')).toHaveTextContent(
      CURRENT_TENANT_ID,
    );
  });

  it('pre-fills the editor with the server-side values', async () => {
    installGetRouter();
    renderPage();
    const nameInput = (await screen.findByTestId(
      'tenant-settings-name',
    )) as HTMLInputElement;
    expect(nameInput.value).toBe('Acme Corp');
    expect(screen.getByTestId('tenant-settings-plan')).toHaveValue('pro');
    expect(screen.getByTestId('tenant-settings-status')).toHaveValue('active');
  });

  it('disables the save button until something changes', async () => {
    installGetRouter();
    renderPage();
    expect(
      await screen.findByTestId('tenant-settings-save'),
    ).toBeDisabled();
  });

  it('PATCHes all three mutable fields when the save button is clicked', async () => {
    installGetRouter();
    mockedPatch.mockResolvedValue({
      data: { ...SEED, name: 'Acme Corp v2', plan: 'enterprise', status: 'suspended' },
    });
    renderPage();
    const user = userEvent.setup();
    const nameInput = (await screen.findByTestId(
      'tenant-settings-name',
    )) as HTMLInputElement;
    nameInput.focus();
    nameInput.select();
    await user.keyboard('Acme Corp v2');
    await user.selectOptions(
      screen.getByTestId('tenant-settings-plan'),
      'enterprise',
    );
    await user.selectOptions(
      screen.getByTestId('tenant-settings-status'),
      'suspended',
    );
    await waitFor(() =>
      expect(screen.getByTestId('tenant-settings-save')).not.toBeDisabled(),
    );
    await user.click(screen.getByTestId('tenant-settings-save'));
    await waitFor(() =>
      expect(mockedPatch).toHaveBeenCalledWith(
        `/api/v1/admin/tenants/${CURRENT_TENANT_ID}/settings`,
        { name: 'Acme Corp v2', plan: 'enterprise', status: 'suspended' },
      ),
    );
  });

  it('shows an inline error when the name is cleared', async () => {
    installGetRouter();
    renderPage();
    const user = userEvent.setup();
    const nameInput = (await screen.findByTestId(
      'tenant-settings-name',
    )) as HTMLInputElement;
    await user.clear(nameInput);
    expect(
      screen.getByTestId('tenant-settings-name-error'),
    ).toHaveTextContent('名称不能为空');
    // Save button is disabled while the name is invalid.
    expect(screen.getByTestId('tenant-settings-save')).toBeDisabled();
  });

  it('shows an error message on a failed PATCH', async () => {
    installGetRouter();
    mockedPatch.mockRejectedValue(new Error('boom'));
    renderPage();
    const user = userEvent.setup();
    const nameInput = (await screen.findByTestId(
      'tenant-settings-name',
    )) as HTMLInputElement;
    nameInput.focus();
    nameInput.select();
    await user.keyboard('Renamed');
    await waitFor(() =>
      expect(screen.getByTestId('tenant-settings-save')).not.toBeDisabled(),
    );
    await user.click(screen.getByTestId('tenant-settings-save'));
    expect(
      await screen.findByTestId('tenant-settings-error'),
    ).toHaveTextContent('保存失败');
  });

  it('shows the loading state while the GET is pending', async () => {
    mockedGet.mockReturnValue(new Promise(() => {}));
    renderPage();
    expect(
      await screen.findByTestId('tenant-settings-loading'),
    ).toHaveTextContent('加载中');
  });

  it('shows an error state when the GET fails', async () => {
    const err = new Error('Network Error');
    (err as unknown as { isAxiosError?: boolean }).isAxiosError = true;
    Object.defineProperty(err, 'message', { value: 'Network Error' });
    mockedGet.mockRejectedValue(err);
    renderPage();
    expect(
      await screen.findByTestId('tenant-settings-error-loading'),
    ).toHaveTextContent(/加载失败/);
  });
});

describe('AdminTenantSettingsPage — assignment policy section', () => {
  it('renders the policy card pre-filled with the server-side values', async () => {
    mockedGet.mockImplementation((url: unknown) => {
      const u = typeof url === 'string' ? url : '';
      if (u.endsWith('/assignment-policy')) {
        return Promise.resolve({
          data: { enabled: true, strategy: 'least_busy' },
        });
      }
      return Promise.resolve({ data: SEED });
    });
    renderPage();

    const card = await screen.findByTestId('assignment-policy-card');
    expect(card).toBeInTheDocument();
    const select = screen.getByTestId(
      'assignment-policy-strategy',
    ) as HTMLSelectElement;
    expect(select.value).toBe('least_busy');
    const checkbox = screen.getByTestId(
      'assignment-policy-enabled',
    ) as HTMLInputElement;
    expect(checkbox.checked).toBe(true);
    expect(
      screen.getByTestId('assignment-policy-save'),
    ).toBeDisabled();
  });

  it('clicking save calls PATCH with the current form values', async () => {
    installGetRouter();
    mockedPatch.mockImplementation((url: unknown, body: unknown) => {
      const u = typeof url === 'string' ? url : '';
      if (u.endsWith('/assignment-policy')) {
        return Promise.resolve({ data: body });
      }
      return Promise.resolve({ data: SEED });
    });
    renderPage();
    const user = userEvent.setup();

    const card = await screen.findByTestId('assignment-policy-card');
    expect(card).toBeInTheDocument();

    await user.selectOptions(
      screen.getByTestId('assignment-policy-strategy'),
      'least_busy',
    );
    const checkbox = screen.getByTestId(
      'assignment-policy-enabled',
    ) as HTMLInputElement;
    await user.click(checkbox);

    await waitFor(() =>
      expect(
        screen.getByTestId('assignment-policy-save'),
      ).not.toBeDisabled(),
    );
    await user.click(screen.getByTestId('assignment-policy-save'));

    await waitFor(() =>
      expect(mockedPatch).toHaveBeenCalledWith(
        `/api/v1/admin/tenants/${CURRENT_TENANT_ID}/assignment-policy`,
        { enabled: true, strategy: 'least_busy' },
      ),
    );
  });
});