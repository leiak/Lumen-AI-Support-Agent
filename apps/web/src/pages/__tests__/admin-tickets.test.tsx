import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, Route, Routes, useNavigate } from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { AxiosError } from 'axios';

import { AdminTicketsPage } from '@/pages/admin-tickets';
import { apiClient, JWT_STORAGE_KEY } from '@/lib/api-client';
import type * as ApiClient from '@/lib/api-client';

vi.mock('@/lib/api-client', async () => {
  const actual = await vi.importActual<typeof ApiClient>('@/lib/api-client');
  return {
    ...actual,
    apiClient: { get: vi.fn(), post: vi.fn() },
  };
});

import type * as ReactRouterDom from 'react-router-dom';

vi.mock('react-router-dom', async () => {
  const actual = await vi.importActual<typeof ReactRouterDom>('react-router-dom');
  return {
    ...actual,
    useNavigate: vi.fn(),
  };
});

const mockedGet = vi.mocked(apiClient.get);
const mockedPost = vi.mocked(apiClient.post);

function makeQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: { retry: false, staleTime: 0, refetchOnWindowFocus: false },
    },
  });
}

function renderList(initialEntry = '/admin/tickets'): {
  client: QueryClient;
  navigate: ReturnType<typeof useNavigate>;
} {
  const client = makeQueryClient();
  const navigate = vi.fn();
  vi.mocked(useNavigate).mockReturnValue(navigate);
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[initialEntry]}>
        <Routes>
          <Route path="/admin/tickets" element={<AdminTicketsPage />} />
          <Route
            path="/admin/tickets/:id"
            element={<div data-testid="ticket-detail-stub">detail</div>}
          />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
  return { client, navigate };
}

const sampleTickets = [
  {
    id: '01HZTCK0000000000000001',
    tenant_id: 'demo',
    conversation_id: '01HZCV00000000000000001',
    subject: 'Production API returning 503s intermittently',
    category: 'outage',
    priority: 'P0' as const,
    status: 'in_progress' as const,
    assignee_agent_id: '01HZDEMO00000000000000001',
    sla_deadline_at: '2026-10-10T06:59:36.855425Z',
    first_response_at: '2026-10-10T06:49:36.855429Z',
    resolved_at: null,
    closed_at: null,
    created_at: '2026-10-10T06:44:36.855425Z',
    updated_at: '2026-10-10T06:44:36.855425Z',
  },
  {
    id: '01HZTCK0000000000000002',
    tenant_id: 'demo',
    conversation_id: '01HZCV00000000000000002',
    subject: 'Duplicate charge on invoice #INV-2026-09',
    category: 'billing',
    priority: 'P1' as const,
    status: 'waiting_customer' as const,
    assignee_agent_id: null,
    sla_deadline_at: '2026-10-10T05:59:36.855432Z',
    first_response_at: '2026-10-10T05:59:36.855433Z',
    resolved_at: null,
    closed_at: null,
    created_at: '2026-10-10T04:59:36.855432Z',
    updated_at: '2026-10-10T04:59:36.855432Z',
  },
  {
    id: '01HZTCK0000000000000003',
    tenant_id: 'demo',
    conversation_id: '01HZCV00000000000000003',
    subject: 'How to issue a partial refund',
    category: 'how-to',
    priority: 'P2' as const,
    status: 'resolved' as const,
    assignee_agent_id: '01HZDEMO00000000000000001',
    sla_deadline_at: '2026-10-07T10:59:36.855435Z',
    first_response_at: '2026-10-07T07:59:36.855436Z',
    resolved_at: '2026-10-08T06:59:36.855437Z',
    closed_at: null,
    created_at: '2026-10-07T06:59:36.855435Z',
    updated_at: '2026-10-07T06:59:36.855435Z',
  },
];

beforeEach(() => {
  window.localStorage.setItem(JWT_STORAGE_KEY, 'test-token');
  mockedGet.mockReset();
  mockedPost.mockReset();
  vi.mocked(useNavigate).mockReturnValue(vi.fn());
});

afterEach(() => {
  cleanup();
  window.localStorage.clear();
  vi.clearAllMocks();
});

describe('AdminTicketsPage', () => {
  it('renders the table with rows on success', async () => {
    // Primary path: admin list endpoint exists and returns the array.
    mockedGet.mockResolvedValue({ data: { tickets: sampleTickets } });

    renderList();

    await waitFor(() => {
      expect(screen.getByTestId('ticket-row-01HZTCK0000000000000001')).toBeInTheDocument();
    });
    expect(screen.getByTestId('ticket-row-01HZTCK0000000000000002')).toBeInTheDocument();
    expect(screen.getByTestId('ticket-row-01HZTCK0000000000000003')).toBeInTheDocument();
    expect(screen.getByText('Production API returning 503s intermittently')).toBeInTheDocument();
  });

  it('falls back to the client-side join when the admin list endpoint 404s', async () => {
    // First call: admin list endpoint 404s.
    const notFound = new AxiosError('Not Found');
    notFound.response = {
      status: 404,
      data: { detail: 'Not Found' },
      statusText: 'Not Found',
      headers: {},
      config: {} as never,
    };
    mockedGet.mockRejectedValueOnce(notFound);
    // Second call: conversation list (used by the fallback path).
    // The fallback calls fetchConversations, which validates each row
    // with ConversationSchema — so we must return FULL conversation
    // objects, not just ids.
    const convRows = sampleTickets.map((t) => ({
      id: t.conversation_id,
      tenant_id: t.tenant_id,
      channel_id: '01HZCHAN0000000000000000',
      customer_external_id: `customer-for-${t.id}`,
      status: 'open' as const,
      assigned_agent_id: t.assignee_agent_id,
      ai_handling: false,
      opened_at: t.created_at,
      last_activity_at: t.updated_at,
    }));
    mockedGet.mockResolvedValueOnce({ data: { items: convRows } });
    // Then per-conversation ticket lookups. The first two return 404
    // (no ticket under those ids) and the third returns a ticket —
    // exercising the "skip 404" branch.
    for (let i = 0; i < sampleTickets.length - 1; i++) {
      const err = new AxiosError('Not Found');
      err.response = {
        status: 404,
        data: { detail: 'ticket not found' },
        statusText: 'Not Found',
        headers: {},
        config: {} as never,
      };
      mockedGet.mockRejectedValueOnce(err);
    }
    mockedGet.mockResolvedValueOnce({ data: sampleTickets[0] });

    renderList();

    await waitFor(() => {
      expect(screen.getByTestId('ticket-row-01HZTCK0000000000000001')).toBeInTheDocument();
    });
    // The other two rows should NOT be present (skipped on 404).
    expect(screen.queryByTestId('ticket-row-01HZTCK0000000000000002')).not.toBeInTheDocument();
  });

  it('filters by status — selecting "in_progress" leaves only the matching row', async () => {
    mockedGet.mockResolvedValue({ data: { tickets: sampleTickets } });

    renderList();

    // Wait for all three rows to appear.
    await waitFor(() => {
      expect(screen.getByTestId('ticket-row-01HZTCK0000000000000001')).toBeInTheDocument();
    });

    const user = userEvent.setup();
    const statusSelect = screen.getByTestId('tickets-filter-status');
    await user.selectOptions(statusSelect, 'in_progress');

    // Wait for the filter to settle — only the P0 outage should remain.
    await waitFor(() => {
      const list = screen.getByTestId('tickets-list');
      expect(within(list).getByTestId('ticket-row-01HZTCK0000000000000001')).toBeInTheDocument();
    });
    expect(screen.queryByTestId('ticket-row-01HZTCK0000000000000002')).not.toBeInTheDocument();
    expect(screen.queryByTestId('ticket-row-01HZTCK0000000000000003')).not.toBeInTheDocument();
  });

  it('filters by priority', async () => {
    mockedGet.mockResolvedValue({ data: { tickets: sampleTickets } });
    renderList();

    await waitFor(() => {
      expect(screen.getByTestId('ticket-row-01HZTCK0000000000000001')).toBeInTheDocument();
    });

    const user = userEvent.setup();
    const prioritySelect = screen.getByTestId('tickets-filter-priority');
    await user.selectOptions(prioritySelect, 'P2');

    await waitFor(() => {
      const list = screen.getByTestId('tickets-list');
      expect(within(list).getByTestId('ticket-row-01HZTCK0000000000000003')).toBeInTheDocument();
    });
    expect(screen.queryByTestId('ticket-row-01HZTCK0000000000000001')).not.toBeInTheDocument();
    expect(screen.queryByTestId('ticket-row-01HZTCK0000000000000002')).not.toBeInTheDocument();
  });

  it('navigates to the detail page when a row is clicked', async () => {
    mockedGet.mockResolvedValue({ data: { tickets: sampleTickets } });

    const { navigate } = renderList();

    const row = await screen.findByTestId('ticket-row-01HZTCK0000000000000002');
    const user = userEvent.setup();
    await user.click(row);

    expect(navigate).toHaveBeenCalledWith('/admin/tickets/01HZTCK0000000000000002');
  });

  it('renders an error state with retry on non-404 API failure', async () => {
    const axiosError = new AxiosError('Internal Server Error');
    axiosError.response = {
      status: 500,
      data: { detail: 'internal error' },
      statusText: 'Internal Server Error',
      headers: {},
      config: {} as never,
    };
    mockedGet.mockRejectedValue(axiosError);

    renderList();

    const errorBox = await screen.findByTestId('tickets-error');
    expect(errorBox).toHaveTextContent('internal error');
    expect(screen.getByRole('button', { name: '重试' })).toBeInTheDocument();
  });

  it('renders the empty state when the list resolves to []', async () => {
    mockedGet.mockResolvedValue({ data: { tickets: [] } });

    renderList();

    expect(await screen.findByTestId('tickets-empty')).toHaveTextContent('暂无工单');
  });

  it('renders loading skeletons while the list is in flight', async () => {
    let resolve!: (v: { data: unknown }) => void;
    mockedGet.mockReturnValue(
      new Promise((res) => {
        resolve = res;
      }),
    );
    renderList();

    expect(screen.getByTestId('tickets-loading')).toBeInTheDocument();
    resolve({ data: { tickets: [] } });
    await waitFor(() =>
      expect(screen.queryByTestId('tickets-loading')).not.toBeInTheDocument(),
    );
  });
});
