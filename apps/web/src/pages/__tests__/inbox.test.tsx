import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, Route, Routes, useNavigate } from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { AxiosError } from 'axios';

import { apiClient, JWT_STORAGE_KEY } from '@/lib/api-client';
import { InboxPage } from '@/pages/inbox';
import type * as ApiClient from '@/lib/api-client';

// Mock the api-client module so the page never touches a real network.
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

// Stub navigate so row-click assertions can verify routing.
import type * as ReactRouterDom from 'react-router-dom';

vi.mock('react-router-dom', async () => {
  const actual = await vi.importActual<typeof ReactRouterDom>('react-router-dom');
  return {
    ...actual,
    useNavigate: vi.fn(),
  };
});

function makeQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: {
        retry: false,
        staleTime: 0,
        refetchOnWindowFocus: false,
      },
    },
  });
}

function renderInbox(initialEntry = '/inbox'): void {
  const client = makeQueryClient();
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[initialEntry]}>
        <Routes>
          <Route path="/inbox" element={<InboxPage />} />
          <Route
            path="/inbox/:id"
            element={<div data-testid="inbox-detail">detail</div>}
          />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

const sampleConversation = {
  id: '01HZX8K1M5R7N3W2Q9P0ABCDEF',
  tenant_id: 'demo',
  channel_id: '01HZX8K1M5R7N3W2Q9P0CHANXYZ',
  customer_external_id: 'cust-99',
  status: 'open',
  assigned_agent_id: 'agent-7',
  ai_handling: false,
  opened_at: '2026-09-01T10:00:00Z',
  last_activity_at: '2026-09-10T12:00:00Z',
};

beforeEach(() => {
  window.localStorage.setItem(JWT_STORAGE_KEY, 'test-token');
  vi.mocked(apiClient.get).mockReset();
  // Silence "not implemented" warnings from the navigate mock.
  vi.mocked(useNavigate).mockReturnValue(vi.fn());
});

afterEach(() => {
  cleanup();
});

describe('InboxPage', () => {
  it('renders loading skeletons while fetching', async () => {
    let resolve!: (value: { data: { items: unknown[] } }) => void;
    vi.mocked(apiClient.get).mockReturnValue(
      new Promise((res) => {
        resolve = res;
      }),
    );
    renderInbox();

    expect(screen.getByTestId('inbox-loading')).toBeInTheDocument();
    expect(apiClient.get).toHaveBeenCalledWith(
      '/api/v1/conversations/inbox',
      expect.objectContaining({ params: {} }),
    );

    resolve({ data: { items: [] } });
    await waitFor(() =>
      expect(screen.queryByTestId('inbox-loading')).not.toBeInTheDocument(),
    );
  });

  it('renders conversation rows on success', async () => {
    vi.mocked(apiClient.get).mockResolvedValue({
      data: { items: [sampleConversation] },
    });
    renderInbox();

    const row = await screen.findByTestId('conversation-row');
    expect(row).toHaveAttribute('data-conversation-id', sampleConversation.id);
    expect(screen.getByTestId('conversation-status')).toHaveTextContent('进行中');
  });

  it('renders empty state when items length is zero', async () => {
    vi.mocked(apiClient.get).mockResolvedValue({ data: { items: [] } });
    renderInbox();

    expect(await screen.findByTestId('inbox-empty')).toHaveTextContent('暂无会话');
  });

  it('renders error state with retry button on failure', async () => {
    const axiosError = new AxiosError('Request failed');
    axiosError.response = {
      status: 500,
      data: { detail: 'internal error' },
      statusText: 'Internal Server Error',
      headers: {},
      config: {} as never,
    };
    vi.mocked(apiClient.get).mockRejectedValue(axiosError);
    renderInbox();

    const errorBox = await screen.findByTestId('inbox-error');
    expect(errorBox).toHaveTextContent('internal error');

    // Retry should trigger a new GET — switch the mock to success.
    vi.mocked(apiClient.get).mockResolvedValue({
      data: { items: [sampleConversation] },
    });
    const user = userEvent.setup();
    await user.click(screen.getByRole('button', { name: '重试' }));
    await waitFor(() =>
      expect(vi.mocked(apiClient.get).mock.calls.length).toBeGreaterThanOrEqual(2),
    );
  });

  it('resets page to 1 on status filter change', async () => {
    vi.mocked(apiClient.get).mockResolvedValue({
      data: { items: [sampleConversation] },
    });
    renderInbox('/inbox?page=3&status=open');

    // Sanity: pagination shows page 3 / 1 (clamped because we only have
    // one item, pageCount floors to 1).
    expect(await screen.findByTestId('inbox-pagination')).toHaveTextContent('1');
    expect(apiClient.get).toHaveBeenLastCalledWith(
      '/api/v1/conversations/inbox',
      expect.objectContaining({ params: { status: 'open' } }),
    );

    const user = userEvent.setup();
    await user.click(screen.getByTestId('status-option-pending'));

    await waitFor(() =>
      expect(apiClient.get).toHaveBeenLastCalledWith(
        '/api/v1/conversations/inbox',
        expect.objectContaining({ params: { status: 'pending' } }),
      ),
    );
    // Page param must be dropped from the URL when reset.
    expect(screen.getByTestId('inbox-pagination')).toHaveTextContent('第 1 / 1 页');
  });

  it('sends the typed query as the q param', async () => {
    vi.mocked(apiClient.get).mockResolvedValue({
      data: { items: [sampleConversation] },
    });
    renderInbox('/inbox');

    const user = userEvent.setup();
    await user.type(screen.getByTestId('conversation-search'), 'customer-9');
    await waitFor(() =>
      expect(apiClient.get).toHaveBeenLastCalledWith(
        '/api/v1/conversations/inbox',
        expect.objectContaining({ params: { q: 'customer-9' } }),
      ),
    );
  });
});