import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { AxiosError } from 'axios';

import { apiClient, JWT_STORAGE_KEY } from '@/lib/api-client';
import { useCurrentUser } from '@/lib/use-current-user';
import type * as ApiClient from '@/lib/api-client';

vi.mock('@/lib/api-client', async () => {
  const actual = await vi.importActual<typeof ApiClient>('@/lib/api-client');
  return {
    ...actual,
    apiClient: {
      post: vi.fn(),
      get: vi.fn(),
    },
  };
});

function Probe(): JSX.Element {
  const { user, isLoading, isError } = useCurrentUser();
  return (
    <div>
      <span data-testid="email">{user?.email ?? ''}</span>
      <span data-testid="tenant-name">{user?.tenant_name ?? ''}</span>
      <span data-testid="is-loading">{String(isLoading)}</span>
      <span data-testid="is-error">{String(isError)}</span>
    </div>
  );
}

function renderProbe(): void {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={['/inbox']}>
        <Routes>
          <Route path="/inbox" element={<Probe />} />
          <Route path="/login" element={<div data-testid="login-page">Login</div>} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  window.localStorage.clear();
  vi.mocked(apiClient.get).mockReset();
});

afterEach(() => {
  cleanup();
});

const sampleMe = {
  user_id: 'u-1',
  email: 'agent@example.com',
  tenant_id: 'demo',
  tenant_name: 'Demo Tenant',
  role: 'agent',
};

describe('useCurrentUser', () => {
  it('test_useCurrentUser_returns_user_on_success', async () => {
    window.localStorage.setItem(JWT_STORAGE_KEY, 'good-token');
    vi.mocked(apiClient.get).mockResolvedValue({ data: sampleMe });
    renderProbe();

    await waitFor(() =>
      expect(screen.getByTestId('email')).toHaveTextContent('agent@example.com'),
    );
    expect(screen.getByTestId('tenant-name')).toHaveTextContent('Demo Tenant');
    expect(screen.getByTestId('is-loading')).toHaveTextContent('false');
    expect(screen.getByTestId('is-error')).toHaveTextContent('false');
  });

  it('test_useCurrentUser_returns_null_on_401', async () => {
    window.localStorage.setItem(JWT_STORAGE_KEY, 'expired-token');
    const err = new AxiosError('Unauthorized');
    err.response = {
      status: 401,
      data: { detail: 'expired' },
      statusText: 'Unauthorized',
      headers: {},
      config: {} as never,
    };
    vi.mocked(apiClient.get).mockRejectedValue(err);
    renderProbe();

    // useEffect on 401 should clear the JWT and navigate to /login.
    await waitFor(() =>
      expect(window.localStorage.getItem(JWT_STORAGE_KEY)).toBeNull(),
    );
    expect(await screen.findByTestId('login-page')).toBeInTheDocument();
  });

  it('test_useCurrentUser_shows_loading_state_during_fetch', async () => {
    window.localStorage.setItem(JWT_STORAGE_KEY, 'token');
    let resolve!: (value: { data: typeof sampleMe }) => void;
    vi.mocked(apiClient.get).mockReturnValue(
      new Promise((res) => {
        resolve = res;
      }),
    );
    renderProbe();

    // Before the query resolves, isLoading should be true.
    expect(screen.getByTestId('is-loading')).toHaveTextContent('true');
    expect(screen.getByTestId('email')).toHaveTextContent('');

    resolve({ data: sampleMe });
    await waitFor(() =>
      expect(screen.getByTestId('is-loading')).toHaveTextContent('false'),
    );
    expect(screen.getByTestId('email')).toHaveTextContent('agent@example.com');
  });
});