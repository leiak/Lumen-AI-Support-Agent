import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { AxiosError } from 'axios';

import { SettingsPage } from '@/pages/settings';
import { apiClient, JWT_STORAGE_KEY } from '@/lib/api-client';
import type * as ApiClient from '@/lib/api-client';

// Mock the api-client module so the page never touches a real network.
vi.mock('@/lib/api-client', async () => {
  const actual = await vi.importActual<typeof ApiClient>('@/lib/api-client');
  return {
    ...actual,
    apiClient: {
      get: vi.fn(),
      post: vi.fn(),
      patch: vi.fn(),
      delete: vi.fn(),
    },
  };
});

function makeQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: { retry: false, staleTime: 0, refetchOnWindowFocus: false },
    },
  });
}

function renderPage(): void {
  const client = makeQueryClient();
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={['/settings']}>
        <Routes>
          <Route path="/settings" element={<SettingsPage />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

const sampleMe = {
  user_id: 'u-1',
  email: 'agent@example.com',
  tenant_id: '01HZX8K1M5R7N3W2Q9P0ABCDEF',
  tenant_name: 'Demo Tenant',
  role: 'agent',
};

const sampleChannels = [
  {
    id: '01HZX8K1M5R7N3W2Q9P0CHANFEISHU',
    type: 'feishu',
    name: '飞书支持',
    status: 'active',
    tenant_id: 'demo',
    created_at: '2026-09-08T10:00:00Z',
  },
  {
    id: '01HZX8K1M5R7N3W2Q9P0CHANWIDGET',
    type: 'web',
    name: 'Web Widget',
    status: 'active',
    tenant_id: 'demo',
    created_at: '2026-09-07T10:00:00Z',
  },
];

beforeEach(() => {
  window.localStorage.setItem(JWT_STORAGE_KEY, 'test-token');
  vi.mocked(apiClient.get).mockReset();
  vi.mocked(apiClient.post).mockReset();
  vi.mocked(apiClient.patch).mockReset();
  vi.mocked(apiClient.delete).mockReset();
});

afterEach(() => {
  cleanup();
  window.localStorage.clear();
});

describe('SettingsPage', () => {
  it('renders all three cards on success', async () => {
    vi.mocked(apiClient.get).mockImplementation((url) => {
      if (typeof url === 'string' && url.endsWith('/api/v1/agents/me')) {
        return Promise.resolve({ data: sampleMe });
      }
      if (typeof url === 'string' && url.endsWith('/api/v1/channels')) {
        return Promise.resolve({ data: sampleChannels });
      }
      return Promise.reject(new Error(`unexpected GET ${url}`));
    });
    renderPage();

    // Tenant info card surfaces the JWT-derived identity.
    expect(await screen.findByTestId('tenant-info-card')).toBeInTheDocument();
    expect(screen.getByTestId('tenant-name')).toHaveTextContent('Demo Tenant');

    // Channel list card renders one row per channel.
    expect(await screen.findByTestId('channel-list')).toBeInTheDocument();
    expect(screen.getAllByTestId('channel-row')).toHaveLength(2);

    // Branding placeholder is always visible.
    expect(screen.getByTestId('branding-placeholder')).toBeInTheDocument();
  });

  it('renders the channel loading skeleton while fetching', async () => {
    let resolveChannels!: (value: { data: typeof sampleChannels }) => void;
    const channelsPromise = new Promise<{ data: typeof sampleChannels }>((res) => {
      resolveChannels = res;
    });

    vi.mocked(apiClient.get).mockImplementation((url) => {
      if (typeof url === 'string' && url.endsWith('/api/v1/agents/me')) {
        return Promise.resolve({ data: sampleMe });
      }
      if (typeof url === 'string' && url.endsWith('/api/v1/channels')) {
        return channelsPromise;
      }
      return Promise.reject(new Error(`unexpected GET ${url}`));
    });
    renderPage();

    // useCurrentUser fires agents/me first; the tenant card appears once
    // that resolves. The channels call stays pending for this test, so
    // the channel list card stays in its loading skeleton state.
    expect(await screen.findByTestId('tenant-info-card')).toBeInTheDocument();
    expect(screen.getByTestId('channel-list-loading')).toBeInTheDocument();

    // Resolve so React Query doesn't warn about pending updates.
    resolveChannels({ data: sampleChannels });
    await waitFor(() =>
      expect(screen.queryByTestId('channel-list-loading')).not.toBeInTheDocument(),
    );
  });

  it('renders an error state with retry on channel fetch failure', async () => {
    const axiosError = new AxiosError('Request failed');
    axiosError.response = {
      status: 500,
      data: { detail: 'channels blew up' },
      statusText: 'Internal Server Error',
      headers: {},
      config: {} as never,
    };
    vi.mocked(apiClient.get).mockImplementation((url) => {
      if (typeof url === 'string' && url.endsWith('/api/v1/agents/me')) {
        return Promise.resolve({ data: sampleMe });
      }
      return Promise.reject(axiosError);
    });
    renderPage();

    const errorBox = await screen.findByTestId('settings-error');
    expect(errorBox).toHaveTextContent('channels blew up');

    // Retry should trigger a new GET — switch the mock to success.
    vi.mocked(apiClient.get).mockImplementation((url) => {
      if (typeof url === 'string' && url.endsWith('/api/v1/agents/me')) {
        return Promise.resolve({ data: sampleMe });
      }
      if (typeof url === 'string' && url.endsWith('/api/v1/channels')) {
        return Promise.resolve({ data: sampleChannels });
      }
      return Promise.reject(new Error(`unexpected GET ${url}`));
    });
    const user = userEvent.setup();
    await user.click(screen.getByRole('button', { name: '重试' }));
    await waitFor(() =>
      expect(screen.getByTestId('channel-list')).toBeInTheDocument(),
    );
  });
});

describe('SettingsPage — channel CRUD (Tier 1 Task 1.2)', () => {
  function setupSuccessfulChannelsFetch(): void {
    vi.mocked(apiClient.get).mockImplementation((url) => {
      if (typeof url === 'string' && url.endsWith('/api/v1/agents/me')) {
        return Promise.resolve({ data: sampleMe });
      }
      if (typeof url === 'string' && url.endsWith('/api/v1/channels')) {
        return Promise.resolve({ data: sampleChannels });
      }
      return Promise.reject(new Error(`unexpected GET ${url}`));
    });
  }

  it('opens create dialog, submits, and POSTs to /api/v1/channels', async () => {
    setupSuccessfulChannelsFetch();
    vi.mocked(apiClient.post).mockResolvedValue({
      data: { ...sampleChannels[1], id: 'ch-new', name: '新邮件', type: 'email' },
    });
    renderPage();
    await screen.findByTestId('channel-list');
    const user = userEvent.setup();
    await user.click(screen.getByTestId('channel-create-button'));
    await screen.findByTestId('channel-create-dialog');
    await user.selectOptions(screen.getByTestId('channel-create-type'), 'email');
    await user.type(screen.getByTestId('channel-create-name'), '新邮件');
    await user.click(screen.getByTestId('channel-create-submit'));
    await waitFor(() =>
      expect(apiClient.post).toHaveBeenCalledWith('/api/v1/channels', {
        type: 'email',
        name: '新邮件',
        credentials: {},
      }),
    );
  });

  it('opens edit dialog, submits, and PATCHes the channel', async () => {
    setupSuccessfulChannelsFetch();
    vi.mocked(apiClient.patch).mockResolvedValue({
      data: { ...sampleChannels[0], name: '改后的飞书' },
    });
    renderPage();
    await screen.findByTestId('channel-list');
    const user = userEvent.setup();
    const feishuId = sampleChannels[0]!.id;
    await user.click(screen.getByTestId(`channel-row-edit-${feishuId}`));
    await screen.findByTestId('channel-edit-dialog');
    await user.clear(screen.getByTestId('channel-edit-name'));
    await user.type(screen.getByTestId('channel-edit-name'), '改后的飞书');
    await user.click(screen.getByTestId('channel-edit-submit'));
    await waitFor(() =>
      expect(apiClient.patch).toHaveBeenCalledWith(
        `/api/v1/channels/${feishuId}`,
        expect.objectContaining({ name: '改后的飞书' }),
      ),
    );
  });

  it('soft-deletes a channel after window.confirm() returns true', async () => {
    setupSuccessfulChannelsFetch();
    vi.mocked(apiClient.delete).mockResolvedValue({ data: null });
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(true);
    renderPage();
    await screen.findByTestId('channel-list');
    const user = userEvent.setup();
    const feishuId = sampleChannels[0]!.id;
    await user.click(screen.getByTestId(`channel-row-delete-${feishuId}`));
    await waitFor(() =>
      expect(apiClient.delete).toHaveBeenCalledWith(`/api/v1/channels/${feishuId}`),
    );
    expect(confirmSpy).toHaveBeenCalledOnce();
  });

  it('does NOT delete when window.confirm() returns false', async () => {
    setupSuccessfulChannelsFetch();
    vi.spyOn(window, 'confirm').mockReturnValue(false);
    renderPage();
    await screen.findByTestId('channel-list');
    const user = userEvent.setup();
    const feishuId = sampleChannels[0]!.id;
    await user.click(screen.getByTestId(`channel-row-delete-${feishuId}`));
    expect(apiClient.delete).not.toHaveBeenCalled();
  });
});