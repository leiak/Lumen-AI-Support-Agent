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

// Admin reassign needs the tenant user roster. Tests opt in to a
// canned roster by overriding the mock in their setup; the default
// returns a single agent so fetchTenantUsers is at least callable.
vi.mock('@/lib/tenant-users', async () => {
  const actual = await vi.importActual<typeof TenantUsers>('@/lib/tenant-users');
  return {
    ...actual,
    fetchTenantUsers: vi.fn(async () => [
      {
        id: 'u_agent_roster',
        tenant_id: 'demo',
        email: 'roster@example.com',
        full_name: 'Roster Agent',
        role: 'agent',
        is_active: true,
        created_at: '2026-09-01T00:00:00Z',
        updated_at: '2026-09-01T00:00:00Z',
      },
    ]),
  };
});

import type * as TenantUsers from '@/lib/tenant-users';

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

function mockCurrentUser(role: 'agent' | 'admin' = 'agent'): void {
  const userId = role === 'admin' ? 'u_admin' : 'u_agent_1';
  const email = role === 'admin' ? 'admin@example.com' : 'agent@example.com';
  vi.mocked(useCurrentUser).mockReturnValue({
    user: {
      user_id: userId,
      email,
      tenant_id: 'demo',
      tenant_name: 'Demo',
      role,
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

function stubConversationFetch(conv: object = sampleConversation): void {
  vi.mocked(apiClient.get).mockImplementation((url) => {
    if (typeof url === 'string' && url.endsWith('/messages')) {
      return Promise.resolve({ data: { items: [] } });
    }
    // Direct GET /api/v1/conversations/{id} — returns the row
    // directly (not wrapped in {items: [...]}) after the 2026-10-13
    // refactor that widened the endpoint to agent+admin.
    return Promise.resolve({ data: conv });
  });
}

beforeEach(() => {
  window.localStorage.setItem(JWT_STORAGE_KEY, 'test-token');
  vi.mocked(apiClient.get).mockReset();
  vi.mocked(apiClient.post).mockReset();
  vi.mocked(useCurrentUser).mockReset();
  mockCurrentUser('agent');
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
    resolve({ data: sampleConversation });
    await waitFor(() =>
      expect(screen.queryByTestId('inbox-detail-loading')).not.toBeInTheDocument(),
    );
  });

  it('renders the header and three panes on success', async () => {
    stubConversationFetch();
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

  it('fetches the conversation via direct GET /api/v1/conversations/{id}', async () => {
    // 2026-10-13 refactor: the page no longer fans out to the list
    // endpoint. Both agent and admin hit the single-conversation
    // endpoint directly; the backend enforces row-level access.
    stubConversationFetch();
    renderPage();

    await waitFor(() =>
      expect(apiClient.get).toHaveBeenCalledWith(
        `/api/v1/conversations/${sampleConversation.id}`,
      ),
    );
    expect(apiClient.get).not.toHaveBeenCalledWith(
      '/api/v1/conversations/inbox',
      expect.anything(),
    );
    expect(apiClient.get).not.toHaveBeenCalledWith(
      '/api/v1/conversations',
      expect.anything(),
    );
  });

  it('calls POST /claim when the claim button is clicked', async () => {
    stubConversationFetch();
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

  it('agent role opens the detail page without role-based routing', async () => {
    // Regression: before the 2026-10-13 refactor, InboxDetailPage
    // switched between ``/api/v1/conversations/inbox`` and
    // ``/api/v1/conversations`` based on ``useIsAdmin()``. When the
    // cached current-user role was stale (e.g. switching between
    // admin and agent within the 30 s TanStack staleTime), the wrong
    // list endpoint was called and the response 403'd. The new
    // single-endpoint design removes that role-coupling entirely.
    mockCurrentUser('agent');
    stubConversationFetch();
    renderPage();

    expect(await screen.findByTestId('conversation-header')).toBeInTheDocument();
    expect(apiClient.get).toHaveBeenCalledWith(
      `/api/v1/conversations/${sampleConversation.id}`,
    );
  });

  it('admin role opens the detail page on the same endpoint', async () => {
    mockCurrentUser('admin');
    stubConversationFetch();
    renderPage();

    expect(await screen.findByTestId('conversation-header')).toBeInTheDocument();
    expect(apiClient.get).toHaveBeenCalledWith(
      `/api/v1/conversations/${sampleConversation.id}`,
    );
  });

  it('admin role sees the reassign control; opening it loads the tenant roster', async () => {
    mockCurrentUser('admin');
    stubConversationFetch();
    renderPage();

    const toggle = await screen.findByTestId('action-reassign-toggle');
    expect(toggle).toBeInTheDocument();

    const user = userEvent.setup();
    await user.click(toggle);

    // Roster panel + select visible after the toggle.
    const select = await screen.findByTestId('reassign-select');
    expect(select).toBeInTheDocument();
    // The canned mock returns the roster agent; its email should be
    // rendered as an <option> once the query resolves.
    await waitFor(() =>
      expect(screen.getByRole('option', { name: /roster@example.com/ })).toBeInTheDocument(),
    );
  });

  it('agent role does not see the reassign control', async () => {
    mockCurrentUser('agent');
    stubConversationFetch();
    renderPage();

    // Wait for the detail page to render, then assert the toggle is absent.
    await screen.findByTestId('conversation-header');
    expect(screen.queryByTestId('action-reassign-toggle')).not.toBeInTheDocument();
    expect(screen.queryByTestId('reassign-panel')).not.toBeInTheDocument();
  });
});
