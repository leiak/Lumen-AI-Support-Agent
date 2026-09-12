import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, Route, Routes, useNavigate } from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { AxiosError } from 'axios';

import { InboxDetailPage } from '@/pages/inbox-detail';
import { apiClient, JWT_STORAGE_KEY } from '@/lib/api-client';
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

vi.mock('@/lib/use-current-user', () => ({
  useCurrentUser: vi.fn(),
}));

import { useCurrentUser } from '@/lib/use-current-user';

import type * as ReactRouterDom from 'react-router-dom';

vi.mock('react-router-dom', async () => {
  const actual = await vi.importActual<typeof ReactRouterDom>('react-router-dom');
  return {
    ...actual,
    useNavigate: vi.fn(),
  };
});

class FakeWebSocket {
  static instances: FakeWebSocket[] = [];
  readyState = 1;
  onopen: (() => void) | null = null;
  onmessage: ((e: { data: string }) => void) | null = null;
  onerror: (() => void) | null = null;
  onclose: (() => void) | null = null;
  constructor(public url: string) {
    FakeWebSocket.instances.push(this);
    queueMicrotask(() => this.onopen?.());
  }
  send(): void {}
  close(): void {
    this.readyState = 3;
    this.onclose?.();
  }
}

const sampleConversation = {
  id: '01HZX8K1M5R7N3W2Q9P0ABCDEF',
  tenant_id: 'demo',
  channel_id: '01HZX8K1M5R7N3W2Q9P0CHANXYZ',
  customer_external_id: 'cust-99',
  status: 'pending',
  assigned_agent_id: null,
  ai_handling: false,
  opened_at: '2026-09-01T10:00:00Z',
  last_activity_at: '2026-09-10T12:00:00Z',
};

function makeQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: { queries: { retry: false, staleTime: 0 } },
  });
}

function mockCurrentUser(): void {
  vi.mocked(useCurrentUser).mockReturnValue({
    user: {
      user_id: 'u_agent_1',
      email: 'agent@example.com',
      tenant_id: 'demo',
      tenant_name: 'Demo',
      role: 'agent',
    },
    isLoading: false,
    isError: false,
  });
}

function renderPage(conversationId = sampleConversation.id): {
  client: QueryClient;
  navigate: ReturnType<typeof useNavigate>;
} {
  const client = makeQueryClient();
  const navigate = vi.fn();
  vi.mocked(useNavigate).mockReturnValue(navigate);
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[`/inbox/${conversationId}`]}>
        <Routes>
          <Route path="/inbox/:id" element={<InboxDetailPage />} />
          <Route path="/inbox" element={<div data-testid="inbox-list">inbox</div>} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
  return { client, navigate };
}

beforeEach(() => {
  window.localStorage.setItem(JWT_STORAGE_KEY, 'test-token');
  vi.mocked(apiClient.get).mockReset();
  vi.mocked(apiClient.post).mockReset();
  vi.mocked(useCurrentUser).mockReset();
  mockCurrentUser();
  FakeWebSocket.instances = [];
  // @ts-expect-error -- install fake WS for the hook.
  globalThis.WebSocket = FakeWebSocket;
});

afterEach(() => {
  cleanup();
  window.localStorage.clear();
  // Restore the real WebSocket so other tests use the real one.
  delete (globalThis as { WebSocket?: unknown }).WebSocket;
});

describe('InboxDetailPage', () => {
  it('renders a loading state while the conversation is being fetched', async () => {
    let resolve!: (v: { data: unknown }) => void;
    vi.mocked(apiClient.get).mockReturnValueOnce(
      new Promise((res) => {
        resolve = res;
      }),
    );
    renderPage();
    expect(screen.getByTestId('inbox-detail-loading')).toBeInTheDocument();
    resolve({ data: { items: [] } });
    // After the empty list resolves the page falls through to its
    // "not found" branch — that's the expected behaviour when the
    // requested conversation id isn't in the agent's inbox.
    await waitFor(() =>
      expect(screen.queryByTestId('inbox-detail-loading')).not.toBeInTheDocument(),
    );
  });

  it('renders the header and three panes on success', async () => {
    vi.mocked(apiClient.get).mockImplementation((url) => {
      if (typeof url === 'string' && url.endsWith('/messages')) {
        return Promise.resolve({ data: { items: [] } });
      }
      return Promise.resolve({ data: { items: [sampleConversation] } });
    });
    renderPage();

    expect(await screen.findByTestId('conversation-header')).toBeInTheDocument();
    expect(screen.getByTestId('customer-info-pane')).toBeInTheDocument();
    // Empty messages list renders the empty-state testid rather
    // than the populated stream testid — both are valid success
    // outcomes. The populated stream is exercised in
    // message-stream.test.tsx.
    expect(screen.getByTestId('message-stream-empty')).toBeInTheDocument();
    expect(screen.getByTestId('message-composer')).toBeInTheDocument();
    expect(screen.getByTestId('ai-suggestion-pane')).toBeInTheDocument();
    expect(screen.getByTestId('header-status')).toHaveTextContent('待处理');
  });

  it('calls POST /claim when the claim button is clicked', async () => {
    vi.mocked(apiClient.get).mockImplementation((url) => {
      if (typeof url === 'string' && url.endsWith('/messages')) {
        return Promise.resolve({ data: { items: [] } });
      }
      return Promise.resolve({ data: { items: [sampleConversation] } });
    });
    vi.mocked(apiClient.post).mockResolvedValue({
      data: { ...sampleConversation, assigned_agent_id: 'u_agent_1' },
    });
    renderPage();
    const claimButton = await screen.findByTestId('action-claim');
    const user = userEvent.setup();
    await user.click(claimButton);
    await waitFor(() =>
      expect(apiClient.post).toHaveBeenCalledWith(
        `/api/v1/agents/conversations/${sampleConversation.id}/claim`,
      ),
    );
  });

  it('renders an error state with retry when the conversation is missing', async () => {
    const axiosError = new AxiosError('not found');
    axiosError.response = {
      status: 404,
      data: { detail: 'conversation not found' },
      statusText: 'Not Found',
      headers: {},
      config: {} as never,
    };
    vi.mocked(apiClient.get).mockRejectedValue(axiosError);
    renderPage();

    const error = await screen.findByTestId('inbox-detail-error');
    expect(error).toHaveTextContent('conversation not found');
    expect(screen.getByRole('button', { name: '重试' })).toBeInTheDocument();
  });
});
