import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';

import { AdminLLMConfigsPage } from '@/pages/admin-llm-configs';
import { apiClient, JWT_STORAGE_KEY } from '@/lib/api-client';
import { useCurrentUser } from '@/lib/use-current-user';

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
        <AdminLLMConfigsPage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

const sampleConfigs = [
  {
    provider_name: 'minimax',
    base_url: null,
    enabled: true,
    created_at: '2026-10-01T10:00:00Z',
    updated_at: '2026-10-07T10:00:00Z',
  },
  {
    provider_name: 'anthropic',
    base_url: 'https://api.anthropic.com',
    enabled: false,
    created_at: '2026-09-15T10:00:00Z',
    updated_at: '2026-09-20T10:00:00Z',
  },
];

describe('AdminLLMConfigsPage (Tier 1 Task 1.3)', () => {
  it('renders the page header + empty state when no configs exist', async () => {
    mockedGet.mockResolvedValue({ data: [] });
    renderPage();
    expect(await screen.findByTestId('admin-llm-configs-page')).toBeInTheDocument();
    expect(await screen.findByTestId('llm-configs-empty')).toBeInTheDocument();
  });

  it('renders one row per provider with status badge', async () => {
    mockedGet.mockResolvedValue({ data: sampleConfigs });
    renderPage();
    expect(await screen.findByTestId('llm-configs-table')).toBeInTheDocument();
    expect(screen.getByTestId('llm-config-row-minimax')).toBeInTheDocument();
    expect(screen.getByTestId('llm-config-row-anthropic')).toBeInTheDocument();
    expect(screen.getByTestId('llm-config-status-minimax')).toHaveTextContent('启用');
    expect(screen.getByTestId('llm-config-status-anthropic')).toHaveTextContent('禁用');
  });

  it('opens the create card and disables submit until an API key is entered', async () => {
    mockedGet.mockResolvedValue({ data: [] });
    renderPage();
    const user = userEvent.setup();
    await user.click(await screen.findByTestId('llm-config-create-open'));
    expect(screen.getByTestId('llm-config-create-card')).toBeInTheDocument();
    // Submit starts disabled — no API key yet.
    expect(screen.getByTestId('llm-config-create-submit')).toBeDisabled();
  });

  it('submits create with provider + api_key + base_url', async () => {
    mockedGet.mockResolvedValue({ data: [] });
    mockedPost.mockResolvedValue({
      data: {
        provider_name: 'openai',
        base_url: 'https://api.openai.com/v1',
        enabled: true,
        created_at: '2026-10-10T12:00:00Z',
        updated_at: '2026-10-10T12:00:00Z',
      },
    });
    renderPage();
    const user = userEvent.setup();
    await user.click(await screen.findByTestId('llm-config-create-open'));
    await user.selectOptions(
      screen.getByTestId('llm-config-create-provider'),
      'openai',
    );
    await user.type(
      screen.getByTestId('llm-config-create-api-key'),
      'sk-test-openai',
    );
    await user.type(
      screen.getByTestId('llm-config-create-base-url'),
      'https://api.openai.com/v1',
    );
    await user.click(screen.getByTestId('llm-config-create-submit'));
    await waitFor(() =>
      expect(mockedPost).toHaveBeenCalledWith(
        '/api/v1/admin/tenants/01HZDEMO00000000000000000/llm-configs',
        {
          provider_name: 'openai',
          api_key: 'sk-test-openai',
          base_url: 'https://api.openai.com/v1',
          enabled: true,
        },
      ),
    );
  });

  it('toggles enabled via PATCH when clicking the toggle button', async () => {
    mockedGet.mockResolvedValue({ data: sampleConfigs });
    mockedPatch.mockResolvedValue({
      data: { ...sampleConfigs[1], enabled: true },
    });
    renderPage();
    const user = userEvent.setup();
    const toggleBtn = await screen.findByTestId('llm-config-toggle-anthropic');
    await user.click(toggleBtn);
    await waitFor(() =>
      expect(mockedPatch).toHaveBeenCalledWith(
        '/api/v1/admin/tenants/01HZDEMO00000000000000000/llm-configs/anthropic',
        { enabled: true },
      ),
    );
  });

  it('calls DELETE after window.confirm() returns true', async () => {
    mockedGet.mockResolvedValue({ data: sampleConfigs });
    mockedDelete.mockResolvedValue({ data: null });
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(true);
    renderPage();
    const user = userEvent.setup();
    await user.click(await screen.findByTestId('llm-config-delete-minimax'));
    await waitFor(() =>
      expect(mockedDelete).toHaveBeenCalledWith(
        '/api/v1/admin/tenants/01HZDEMO00000000000000000/llm-configs/minimax',
      ),
    );
    expect(confirmSpy).toHaveBeenCalledOnce();
  });
});
