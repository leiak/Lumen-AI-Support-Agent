import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';

import { AdminUsersPage } from '@/pages/admin-users';
import { apiClient, JWT_STORAGE_KEY } from '@/lib/api-client';
import { useCurrentUser } from '@/lib/use-current-user';

// Tier 2 Task 2.2 — Tenant Users admin page tests.

vi.mock('@/lib/api-client', async () => {
  // eslint-disable-next-line @typescript-eslint/consistent-type-imports
  const actual = await vi.importActual<typeof import('@/lib/api-client')>(
    '@/lib/api-client',
  );
  return {
    ...actual,
    apiClient: { get: vi.fn(), post: vi.fn(), patch: vi.fn(), delete: vi.fn() },
  };
});

vi.mock('@/lib/use-current-user', () => ({
  useCurrentUser: vi.fn(),
}));

const mockedGet = vi.mocked(apiClient.get);
const mockedPost = vi.mocked(apiClient.post);
const mockedPatch = vi.mocked(apiClient.patch);
const mockedDelete = vi.mocked(apiClient.delete);
const mockedUseCurrentUser = vi.mocked(useCurrentUser);

const CURRENT_ADMIN_ID = 'admin-self';
const CURRENT_TENANT_ID = '01HZDEMO00000000000000000';

beforeEach(() => {
  mockedGet.mockReset();
  mockedPost.mockReset();
  mockedPatch.mockReset();
  mockedDelete.mockReset();
  window.localStorage.setItem(JWT_STORAGE_KEY, 'test-token');
  mockedUseCurrentUser.mockReturnValue({
    user: {
      user_id: CURRENT_ADMIN_ID,
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
        <AdminUsersPage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

const SEED_USERS = [
  {
    id: 'agent-1',
    tenant_id: CURRENT_TENANT_ID,
    email: 'alice@example.com',
    full_name: 'Alice Operator',
    role: 'agent',
    is_active: true,
    created_at: '2026-09-01T00:00:00Z',
    updated_at: '2026-10-01T00:00:00Z',
  },
  {
    id: 'agent-2',
    tenant_id: CURRENT_TENANT_ID,
    email: 'bob@example.com',
    full_name: null,
    role: 'viewer',
    is_active: false,
    created_at: '2026-09-15T00:00:00Z',
    updated_at: '2026-09-15T00:00:00Z',
  },
];

describe('AdminUsersPage (Tier 2 Task 2.2)', () => {
  it('renders the empty state when no users exist', async () => {
    mockedGet.mockResolvedValue({ data: [] });
    renderPage();
    expect(await screen.findByTestId('users-empty')).toBeInTheDocument();
    expect(screen.getByTestId('user-create-card')).toBeInTheDocument();
  });

  it('renders one row per user with email, full_name, role, status', async () => {
    mockedGet.mockResolvedValue({ data: SEED_USERS });
    renderPage();
    expect(await screen.findByTestId('user-row-agent-1')).toBeInTheDocument();
    expect(screen.getByTestId('user-row-agent-2')).toBeInTheDocument();
    expect(screen.getByTestId('user-email-agent-1')).toHaveTextContent(
      'alice@example.com',
    );
    expect(screen.getByTestId('user-full-name-agent-1')).toHaveTextContent(
      'Alice Operator',
    );
    expect(screen.getByTestId('user-full-name-agent-2')).toHaveTextContent('—');
    expect(screen.getByTestId('user-status-active')).toBeInTheDocument();
    expect(screen.getByTestId('user-status-inactive')).toBeInTheDocument();
  });

  it('disables the create submit until email + password are filled', async () => {
    mockedGet.mockResolvedValue({ data: [] });
    renderPage();
    const user = userEvent.setup();
    // Initially disabled — no email + no password.
    expect(screen.getByTestId('user-create-submit')).toBeDisabled();
    await user.type(
      await screen.findByTestId('user-create-email'),
      'newuser@example.com',
    );
    // Still disabled — no password.
    expect(screen.getByTestId('user-create-submit')).toBeDisabled();
    await user.type(screen.getByTestId('user-create-password'), 'shortpw');
    // 7 chars — still disabled (server requires ≥ 8).
    expect(screen.getByTestId('user-create-submit')).toBeDisabled();
    await user.type(screen.getByTestId('user-create-password'), 'X');
    // 8 chars — enabled.
    expect(screen.getByTestId('user-create-submit')).not.toBeDisabled();
  });

  it('submits create via POST with email + password + role', async () => {
    mockedGet.mockResolvedValue({ data: [] });
    mockedPost.mockResolvedValue({
      data: {
        id: 'new-id',
        tenant_id: CURRENT_TENANT_ID,
        email: 'newuser@example.com',
        full_name: 'New User',
        role: 'agent',
        is_active: true,
        created_at: '2026-10-11T00:00:00Z',
        updated_at: '2026-10-11T00:00:00Z',
      },
    });
    renderPage();
    const user = userEvent.setup();
    await user.type(
      await screen.findByTestId('user-create-email'),
      'newuser@example.com',
    );
    await user.type(
      screen.getByTestId('user-create-password'),
      'longenough123',
    );
    await user.click(screen.getByTestId('user-create-submit'));
    await waitFor(() =>
      expect(mockedPost).toHaveBeenCalledWith(
        `/api/v1/admin/tenants/${CURRENT_TENANT_ID}/users`,
        {
          email: 'newuser@example.com',
          password: 'longenough123',
          role: 'agent',
        },
      ),
    );
  });

  it('disables save / delete controls on the self row', async () => {
    const selfUser = {
      id: CURRENT_ADMIN_ID,
      tenant_id: CURRENT_TENANT_ID,
      email: 'self@example.com',
      full_name: 'Self Admin',
      role: 'admin',
      is_active: true,
      created_at: '2026-09-01T00:00:00Z',
      updated_at: '2026-10-01T00:00:00Z',
    };
    mockedGet.mockResolvedValue({ data: [selfUser] });
    renderPage();
    expect(await screen.findByTestId('user-self-badge-admin-self')).toBeInTheDocument();
    expect(screen.getByTestId('user-save-admin-self')).toBeDisabled();
    expect(screen.getByTestId('user-delete-admin-self')).toBeDisabled();
    expect(screen.getByTestId('user-edit-role-admin-self')).toBeDisabled();
    expect(screen.getByTestId('user-edit-active-admin-self')).toBeDisabled();
  });

  it('calls PATCH when the save button is clicked after role change', async () => {
    mockedGet.mockResolvedValue({ data: SEED_USERS });
    mockedPatch.mockResolvedValue({
      data: { ...SEED_USERS[0], role: 'supervisor' },
    });
    renderPage();
    const user = userEvent.setup();
    await user.selectOptions(
      await screen.findByTestId('user-edit-role-agent-1'),
      'supervisor',
    );
    await waitFor(() =>
      expect(screen.getByTestId('user-save-agent-1')).not.toBeDisabled(),
    );
    await user.click(screen.getByTestId('user-save-agent-1'));
    await waitFor(() =>
      expect(mockedPatch).toHaveBeenCalledWith(
        `/api/v1/admin/tenants/${CURRENT_TENANT_ID}/users/agent-1`,
        { role: 'supervisor', is_active: true },
      ),
    );
  });

  it('opens the password reset form and POSTs to the reset endpoint', async () => {
    mockedGet.mockResolvedValue({ data: SEED_USERS });
    mockedPost.mockResolvedValue({ data: null });
    renderPage();
    const user = userEvent.setup();
    await user.click(
      await screen.findByTestId('user-reset-pw-agent-2'),
    );
    expect(screen.getByTestId('user-pw-form-agent-2')).toBeInTheDocument();
    await user.type(
      screen.getByTestId('user-pw-input-agent-2'),
      'brand-new-password',
    );
    await user.click(screen.getByTestId('user-pw-submit-agent-2'));
    await waitFor(() =>
      expect(mockedPost).toHaveBeenCalledWith(
        `/api/v1/admin/tenants/${CURRENT_TENANT_ID}/users/agent-2/reset-password`,
        { new_password: 'brand-new-password' },
      ),
    );
  });

  it('calls DELETE after window.confirm() returns true', async () => {
    mockedGet.mockResolvedValue({ data: SEED_USERS });
    mockedDelete.mockResolvedValue({ data: null });
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(true);
    renderPage();
    const user = userEvent.setup();
    await user.click(await screen.findByTestId('user-delete-agent-2'));
    await waitFor(() =>
      expect(mockedDelete).toHaveBeenCalledWith(
        `/api/v1/admin/tenants/${CURRENT_TENANT_ID}/users/agent-2`,
      ),
    );
    expect(confirmSpy).toHaveBeenCalledOnce();
  });

  it('does NOT call DELETE when the user cancels the confirm dialog', async () => {
    mockedGet.mockResolvedValue({ data: SEED_USERS });
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(false);
    renderPage();
    const user = userEvent.setup();
    await user.click(await screen.findByTestId('user-delete-agent-2'));
    await waitFor(() => expect(confirmSpy).toHaveBeenCalledOnce());
    expect(mockedDelete).not.toHaveBeenCalled();
  });
});