import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';

import { AdminSlaPoliciesPage } from '@/pages/admin-sla-policies';
import { apiClient, JWT_STORAGE_KEY } from '@/lib/api-client';
import { useCurrentUser } from '@/lib/use-current-user';

// Tier 2 Task 2.1 — SLA Policies CRUD mutation suite.
// Mirrors the LLM-configs mutation test (the same useMutation + invalidateQueries
// pattern). Covers create, PATCH (with dirty detection), and DELETE.

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

beforeEach(() => {
  mockedGet.mockReset();
  mockedPost.mockReset();
  mockedPatch.mockReset();
  mockedDelete.mockReset();
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
        <AdminSlaPoliciesPage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

const SEED = [
  {
    id: 'p0-id',
    name: 'P0 Critical',
    priority: 'P0' as const,
    first_response_minutes: 15,
    resolution_minutes: 240,
    business_hours_only: false,
    created_at: '2026-10-10T00:00:00Z',
  },
  {
    id: 'p2-id',
    name: 'P2 Normal',
    priority: 'P2' as const,
    first_response_minutes: 240,
    resolution_minutes: 1440,
    business_hours_only: false,
    created_at: '2026-10-10T00:00:00Z',
  },
];

describe('AdminSlaPoliciesPage mutations (Tier 2 Task 2.1)', () => {
  it('renders the create card alongside the existing rows', async () => {
    mockedGet.mockResolvedValue({ data: SEED });
    renderPage();
    // Create card always visible (no dialog toggle).
    expect(
      await screen.findByTestId('sla-policy-create-card'),
    ).toBeInTheDocument();
    expect(
      screen.getByTestId('sla-policy-create-submit'),
    ).toBeInTheDocument();
    // Existing rows still render (wait for the query to settle).
    expect(
      await screen.findByTestId('sla-policy-row-P0'),
    ).toBeInTheDocument();
    expect(screen.getByTestId('sla-policy-row-P2')).toBeInTheDocument();
  });

  it('disables the create submit until a name is entered', async () => {
    mockedGet.mockResolvedValue({ data: SEED });
    renderPage();
    const user = userEvent.setup();
    await user.click(await screen.findByTestId('sla-policy-create-name'));
    // Submit starts disabled — name is empty.
    expect(screen.getByTestId('sla-policy-create-submit')).toBeDisabled();
    await user.type(
      screen.getByTestId('sla-policy-create-name'),
      'P1 High',
    );
    // Now enabled.
    expect(
      screen.getByTestId('sla-policy-create-submit'),
    ).not.toBeDisabled();
  });

  it('submits create via POST when all fields are populated', async () => {
    mockedGet.mockResolvedValue({ data: SEED });
    mockedPost.mockResolvedValue({
      data: {
        id: 'p1-new',
        name: 'P1 High',
        priority: 'P1',
        first_response_minutes: 60,
        resolution_minutes: 480,
        business_hours_only: true,
        created_at: '2026-10-11T00:00:00Z',
      },
    });
    renderPage();
    const user = userEvent.setup();
    await user.type(
      await screen.findByTestId('sla-policy-create-name'),
      'P1 High',
    );
    await user.selectOptions(
      screen.getByTestId('sla-policy-create-priority'),
      'P1',
    );
    // Overwrite the default 30/240 with 60/480 via fireEvent so we don't
    // have to clear the input first (type=number under React).
    const respInput = screen.getByTestId(
      'sla-policy-create-response',
    ) as HTMLInputElement;
    const resoInput = screen.getByTestId(
      'sla-policy-create-resolution',
    ) as HTMLInputElement;
    respInput.focus();
    respInput.select();
    await user.keyboard('60');
    resoInput.focus();
    resoInput.select();
    await user.keyboard('480');
    await user.click(screen.getByTestId('sla-policy-create-hours'));

    await user.click(screen.getByTestId('sla-policy-create-submit'));

    await waitFor(() =>
      expect(mockedPost).toHaveBeenCalledWith(
        '/api/v1/admin/tenants/01HZDEMO00000000000000000/sla-policies',
        {
          name: 'P1 High',
          priority: 'P1',
          first_response_minutes: 60,
          resolution_minutes: 480,
          business_hours_only: true,
        },
      ),
    );
  });

  it('disables the save button until a row is dirty', async () => {
    mockedGet.mockResolvedValue({ data: SEED });
    renderPage();
    // Row renders + save button is initially disabled (no edits yet).
    const saveBtn = await screen.findByTestId('sla-policy-save-P0');
    expect(saveBtn).toBeDisabled();
    // Edit the first-response field via fireEvent so we don't run into
    // Radix Dialog portal issues (no dialog here, but stay consistent).
    const respInput = screen.getByTestId(
      'sla-policy-edit-response-P0',
    ) as HTMLInputElement;
    respInput.focus();
    respInput.select();
    await userEvent.setup().keyboard('20');
    await waitFor(() => expect(saveBtn).not.toBeDisabled());
  });

  it('calls PATCH with the edited fields when the save button is clicked', async () => {
    mockedGet.mockResolvedValue({ data: SEED });
    mockedPatch.mockResolvedValue({
      data: { ...SEED[0], first_response_minutes: 5 },
    });
    renderPage();
    const user = userEvent.setup();
    const respInput = await screen.findByTestId(
      'sla-policy-edit-response-P0',
    ) as HTMLInputElement;
    respInput.focus();
    respInput.select();
    await user.keyboard('5');
    await waitFor(() =>
      expect(
        screen.getByTestId('sla-policy-save-P0'),
      ).not.toBeDisabled(),
    );
    await user.click(screen.getByTestId('sla-policy-save-P0'));
    await waitFor(() =>
      expect(mockedPatch).toHaveBeenCalledWith(
        '/api/v1/admin/tenants/01HZDEMO00000000000000000/sla-policies/p0-id',
        {
          first_response_minutes: 5,
          resolution_minutes: 240,
          business_hours_only: false,
        },
      ),
    );
  });

  it('calls DELETE after window.confirm() returns true', async () => {
    mockedGet.mockResolvedValue({ data: SEED });
    mockedDelete.mockResolvedValue({ data: null });
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(true);
    renderPage();
    const user = userEvent.setup();
    await user.click(await screen.findByTestId('sla-policy-delete-P2'));
    await waitFor(() =>
      expect(mockedDelete).toHaveBeenCalledWith(
        '/api/v1/admin/tenants/01HZDEMO00000000000000000/sla-policies/p2-id',
      ),
    );
    expect(confirmSpy).toHaveBeenCalledOnce();
  });

  it('does NOT call DELETE when the user cancels the confirm dialog', async () => {
    mockedGet.mockResolvedValue({ data: SEED });
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(false);
    renderPage();
    const user = userEvent.setup();
    await user.click(await screen.findByTestId('sla-policy-delete-P2'));
    // Brief tick to let any pending mutation settle.
    await waitFor(() => expect(confirmSpy).toHaveBeenCalledOnce());
    expect(mockedDelete).not.toHaveBeenCalled();
  });
});