import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, Route, Routes, useNavigate } from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { AxiosError } from 'axios';

import { AdminTicketDetailPage } from '@/pages/admin-ticket-detail';
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
    defaultOptions: { queries: { retry: false, staleTime: 0 } },
  });
}

function renderDetail(ticketId = '01HZTCK0000000000000001'): {
  client: QueryClient;
  navigate: ReturnType<typeof useNavigate>;
} {
  const client = makeQueryClient();
  const navigate = vi.fn();
  vi.mocked(useNavigate).mockReturnValue(navigate);
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[`/admin/tickets/${ticketId}`]}>
        <Routes>
          <Route
            path="/admin/tickets/:id"
            element={<AdminTicketDetailPage />}
          />
          <Route
            path="/admin/tickets"
            element={<div data-testid="tickets-list-stub">list</div>}
          />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
  return { client, navigate };
}

const sampleTicket = {
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
};

const sampleEvents = [
  {
    id: '01HZEV0000000000000001',
    actor_type: 'agent',
    actor_id: '01HZDEMO00000000000000001',
    event_type: 'agent_replied',
    payload: { content: 'Pulling logs now' },
    created_at: '2026-10-10T06:50:36.855425Z',
  },
  {
    id: '01HZEV0000000000000002',
    actor_type: 'system',
    actor_id: null,
    event_type: 'ticket_triaged',
    payload: { to: 'triaged' },
    created_at: '2026-10-10T06:46:36.855425Z',
  },
  {
    id: '01HZEV0000000000000003',
    actor_type: 'system',
    actor_id: null,
    event_type: 'ticket_created',
    payload: {},
    created_at: '2026-10-10T06:44:36.855425Z',
  },
];

const sampleMessages = [
  {
    id: '01HZMSG00000000000001',
    conversation_id: '01HZCV00000000000000001',
    role: 'customer',
    content_text: 'Our SDK keeps dropping websocket connections after 90 seconds.',
    sender_id: null,
    created_at: '2026-10-10T03:59:36.848883Z',
  },
  {
    id: '01HZMSG00000000000002',
    conversation_id: '01HZCV00000000000000001',
    role: 'agent',
    content_text: 'Got the logs. The 503 is from rate-limit fallback.',
    sender_id: '01HZDEMO00000000000000001',
    created_at: '2026-10-10T06:26:36.848894Z',
  },
];

function mockSuccessfulLoad(): void {
  mockedGet.mockImplementation((url) => {
    if (typeof url === 'string') {
      if (url.endsWith('/events')) {
        return Promise.resolve({ data: sampleEvents });
      }
      if (url.includes('/messages')) {
        return Promise.resolve({ data: { items: sampleMessages } });
      }
    }
    return Promise.resolve({ data: sampleTicket });
  });
}

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

describe('AdminTicketDetailPage', () => {
  it('renders the subject, badges, and metadata grid on success', async () => {
    mockSuccessfulLoad();
    renderDetail();

    expect(await screen.findByTestId('admin-ticket-detail-page')).toBeInTheDocument();
    expect(screen.getByTestId('ticket-detail-subject')).toHaveTextContent(
      'Production API returning 503s intermittently',
    );
    expect(screen.getByTestId('ticket-detail-priority')).toHaveTextContent('P0 紧急');
    expect(screen.getByTestId('ticket-detail-status')).toHaveTextContent('处理中');
    expect(screen.getByTestId('ticket-detail-category')).toHaveTextContent('outage');

    // Metadata items: 工单 ID, 关联会话 ID, etc.
    const items = screen.getAllByTestId('ticket-metadata-item');
    expect(items.length).toBeGreaterThanOrEqual(10);
  });

  it('renders the events timeline with newest first', async () => {
    mockSuccessfulLoad();
    renderDetail();

    const list = await screen.findByTestId('ticket-events-list');
    const rows = withinList(list, 'ticket-event-row-');
    // Backend returns newest-first; the page renders them in that order.
    expect(rows[0]).toHaveAttribute('data-testid', 'ticket-event-row-01HZEV0000000000000001');
    expect(rows[1]).toHaveAttribute('data-testid', 'ticket-event-row-01HZEV0000000000000002');
    expect(rows[2]).toHaveAttribute('data-testid', 'ticket-event-row-01HZEV0000000000000003');
  });

  it('renders the transition buttons for the legal next states from in_progress', async () => {
    mockSuccessfulLoad();
    renderDetail();

    await screen.findByTestId('admin-ticket-detail-page');
    // in_progress → {waiting_customer, resolved, cancelled}
    expect(screen.getByTestId('ticket-transition-waiting_customer')).toBeInTheDocument();
    expect(screen.getByTestId('ticket-transition-resolved')).toBeInTheDocument();
    expect(screen.getByTestId('ticket-transition-cancelled')).toBeInTheDocument();
    // CLOSED is NOT a legal next state from in_progress → no button.
    expect(screen.queryByTestId('ticket-transition-closed')).not.toBeInTheDocument();
  });

  it('shows the terminal-state copy when the ticket is closed', async () => {
    const closed = { ...sampleTicket, status: 'closed' as const };
    mockedGet.mockImplementation((url) => {
      if (typeof url === 'string') {
        if (url.endsWith('/events')) {
          return Promise.resolve({ data: sampleEvents });
        }
        if (url.includes('/messages')) {
          return Promise.resolve({ data: { items: sampleMessages } });
        }
      }
      return Promise.resolve({ data: closed });
    });
    renderDetail();

    await screen.findByTestId('admin-ticket-detail-page');
    expect(screen.getByTestId('ticket-no-transitions')).toHaveTextContent('终态工单');
  });

  it('calls POST /transition and updates the UI when a transition button is clicked', async () => {
    mockSuccessfulLoad();
    const updated = { ...sampleTicket, status: 'resolved' as const };
    mockedPost.mockResolvedValue({ data: updated });

    renderDetail();

    await screen.findByTestId('admin-ticket-detail-page');
    const button = screen.getByTestId('ticket-transition-resolved');
    const user = userEvent.setup();
    await user.click(button);

    await waitFor(() => {
      expect(mockedPost).toHaveBeenCalledWith(
        '/api/v1/tickets/01HZTCK0000000000000001/transition',
        { to_status: 'resolved', actor_type: 'admin' },
      );
    });

    // After the mutation succeeds, the status badge updates to 已解决
    // (optimistic via setQueryData).
    await waitFor(() => {
      expect(screen.getByTestId('ticket-detail-status')).toHaveTextContent('已解决');
    });
  });

  it('renders an inline error banner when the transition call fails', async () => {
    mockSuccessfulLoad();
    const conflict = new AxiosError('Conflict');
    conflict.response = {
      status: 409,
      data: { detail: 'illegal transition' },
      statusText: 'Conflict',
      headers: {},
      config: {} as never,
    };
    mockedPost.mockRejectedValue(conflict);

    renderDetail();

    await screen.findByTestId('admin-ticket-detail-page');
    const user = userEvent.setup();
    await user.click(screen.getByTestId('ticket-transition-resolved'));

    const errorBanner = await screen.findByTestId('ticket-transition-error');
    expect(errorBanner).toHaveTextContent('illegal transition');
  });

  it('renders the linked conversation messages in the sidebar', async () => {
    mockSuccessfulLoad();
    renderDetail();

    await screen.findByTestId('admin-ticket-detail-page');
    // MessageStream's populated testid is "message-stream" (not "message-stream-empty").
    await waitFor(() => {
      expect(screen.getByTestId('message-stream')).toBeInTheDocument();
    });
    expect(screen.getByTestId('ticket-conversation-messages')).toBeInTheDocument();
  });

  it('navigates back to the list when the back button is clicked', async () => {
    mockSuccessfulLoad();

    const { navigate } = renderDetail();

    await screen.findByTestId('admin-ticket-detail-page');
    const user = userEvent.setup();
    await user.click(screen.getByTestId('back-to-tickets'));

    expect(navigate).toHaveBeenCalledWith('/admin/tickets');
  });

  it('renders an error state with retry when the ticket is missing', async () => {
    const notFound = new AxiosError('Not Found');
    notFound.response = {
      status: 404,
      data: { detail: 'ticket not found' },
      statusText: 'Not Found',
      headers: {},
      config: {} as never,
    };
    mockedGet.mockRejectedValue(notFound);

    renderDetail();

    const errorBox = await screen.findByTestId('admin-ticket-detail-error');
    expect(errorBox).toHaveTextContent('ticket not found');
    expect(screen.getByRole('button', { name: '重试' })).toBeInTheDocument();
  });
});

// Local helper — querySelector scoping is annoying for ordered testid lists.
function withinList(list: HTMLElement, prefix: string): HTMLElement[] {
  return Array.from(list.querySelectorAll<HTMLElement>(`[data-testid^="${prefix}"]`));
}
