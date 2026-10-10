import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, Route, Routes, useNavigate } from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { AxiosError } from 'axios';

import { AdminTicketsPage } from '@/pages/admin-tickets';
import { apiClient, JWT_STORAGE_KEY } from '@/lib/api-client';
import type { Ticket } from '@/lib/tickets';
import type * as ApiClient from '@/lib/api-client';
import { useCurrentUser } from '@/lib/use-current-user';
import type * as UseCurrentUser from '@/lib/use-current-user';

vi.mock('@/lib/api-client', async () => {
  const actual = await vi.importActual<typeof ApiClient>('@/lib/api-client');
  return {
    ...actual,
    apiClient: { get: vi.fn(), post: vi.fn() },
  };
});

vi.mock('@/lib/use-current-user', async () => {
  const actual = await vi.importActual<typeof UseCurrentUser>(
    '@/lib/use-current-user',
  );
  return {
    ...actual,
    useCurrentUser: vi.fn(),
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
const mockedUseCurrentUser = vi.mocked(useCurrentUser);

// Demo tenant ULID used in the URL path of the new admin list endpoint
// (GET /api/v1/admin/tenants/{tenant_id}/tickets). Must match the
// ``tenant_id`` field below in the mock so the test exercises a
// single-tenant, authenticated happy path.
const DEMO_TENANT_ID = '01HZDEMO00000000000000000';

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
    tenant_id: DEMO_TENANT_ID,
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
    tenant_id: DEMO_TENANT_ID,
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
    tenant_id: DEMO_TENANT_ID,
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

/** Envelope wrapping ``sampleTickets`` for the new admin list endpoint. */
function envelope(items: Ticket[] = sampleTickets as Ticket[], total?: number) {
  return { data: { items, total: total ?? items.length } };
}

beforeEach(() => {
  window.localStorage.setItem(JWT_STORAGE_KEY, 'test-token');
  mockedGet.mockReset();
  mockedPost.mockReset();
  // Default useCurrentUser implementation: returns the demo tenant so
  // ``fetchTickets`` can construct the admin list URL path. Individual
  // tests can override via ``mockedUseCurrentUser.mockImplementation``.
  mockedUseCurrentUser.mockReset();
  mockedUseCurrentUser.mockReturnValue({
    user: {
      user_id: '01HZDEMO00000000000000001',
      email: 'admin@demo.test',
      tenant_id: DEMO_TENANT_ID,
      tenant_name: 'Demo Tenant',
      role: 'admin',
    },
    isLoading: false,
    isError: false,
  });
  vi.mocked(useNavigate).mockReturnValue(vi.fn());
});

afterEach(() => {
  cleanup();
  window.localStorage.clear();
  vi.clearAllMocks();
});

describe('AdminTicketsPage', () => {
  it('renders the table with rows on success', async () => {
    // Primary path: admin list endpoint returns the envelope shape
    // ``{items, total}`` (Task 1.1). All three rows render.
    mockedGet.mockResolvedValue(envelope());

    renderList();

    await waitFor(() => {
      expect(screen.getByTestId('ticket-row-01HZTCK0000000000000001')).toBeInTheDocument();
    });
    expect(screen.getByTestId('ticket-row-01HZTCK0000000000000002')).toBeInTheDocument();
    expect(screen.getByTestId('ticket-row-01HZTCK0000000000000003')).toBeInTheDocument();
    expect(screen.getByText('Production API returning 503s intermittently')).toBeInTheDocument();
  });

  it('renders 2 tickets when the endpoint returns a 2-row envelope', async () => {
    // Targeted assertion for the new endpoint: the SPA must render
    // exactly the rows the backend returns (no client-side join
    // fallback). Mirrors the live demo seed shape (1 P0 NEW + 1 P1
    // TRIAGED).
    const twoTickets: Ticket[] = sampleTickets.slice(0, 2) as Ticket[];
    mockedGet.mockResolvedValue(envelope(twoTickets, 2));

    renderList();

    await waitFor(() => {
      expect(screen.getByTestId('ticket-row-01HZTCK0000000000000001')).toBeInTheDocument();
    });
    expect(screen.getByTestId('ticket-row-01HZTCK0000000000000002')).toBeInTheDocument();
    expect(screen.queryByTestId('ticket-row-01HZTCK0000000000000003')).not.toBeInTheDocument();
  });

  it('filters by status — selecting "in_progress" leaves only the matching row', async () => {
    // First request (no filter): all 3 rows.
    mockedGet.mockResolvedValueOnce(envelope());
    // After filter change: server-side filter returns only the P0
    // in_progress row.
    const filtered: Ticket[] = sampleTickets.slice(0, 1) as Ticket[];
    mockedGet.mockResolvedValueOnce(envelope(filtered, 1));

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
    // First request: all 3 rows. After filter: only the P2 row.
    mockedGet.mockResolvedValueOnce(envelope());
    const filtered: Ticket[] = sampleTickets.slice(2, 3) as Ticket[];
    mockedGet.mockResolvedValueOnce(envelope(filtered, 1));
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
    mockedGet.mockResolvedValue(envelope());

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
    mockedGet.mockResolvedValue(envelope([], 0));

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
    resolve(envelope([], 0));
    await waitFor(() =>
      expect(screen.queryByTestId('tickets-loading')).not.toBeInTheDocument(),
    );
  });

  it('does not fetch when useCurrentUser has no user (loading skeleton stays)', async () => {
    // When the caller's JWT is missing or invalid, useCurrentUser
    // returns ``user: null`` and the page never calls fetchTickets
    // (the query is ``enabled: Boolean(tenantId)``). The page stays
    // in the loading state because no result has been resolved.
    mockedUseCurrentUser.mockReturnValue({
      user: null,
      isLoading: false,
      isError: false,
    });
    // Even if fetchTickets were called, it would reject — but with
    // ``enabled: false`` the query is never fired, so no network
    // call lands on the mock.
    mockedGet.mockRejectedValue(new Error('not authenticated'));

    renderList();

    // Loading skeleton stays; no error banner.
    expect(screen.getByTestId('tickets-loading')).toBeInTheDocument();
    expect(screen.queryByTestId('tickets-error')).not.toBeInTheDocument();
  });
});
