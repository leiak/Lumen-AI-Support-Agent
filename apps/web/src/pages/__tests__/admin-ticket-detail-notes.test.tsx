import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';

import { AdminTicketDetailPage } from '@/pages/admin-ticket-detail';
import { apiClient, JWT_STORAGE_KEY } from '@/lib/api-client';
import type * as ApiClient from '@/lib/api-client';
import { useCurrentUser } from '@/lib/use-current-user';

// Tier 2 Task 2.6 — internal notes panel tests.
//
// Focused on the new behaviour only: posting notes, displaying
// others' notes, deleting own notes, and confirming the server-side
// author-only restriction is mirrored by the UI (delete button
// hidden on others' notes).

vi.mock('@/lib/api-client', async () => {
  const actual = await vi.importActual<typeof ApiClient>('@/lib/api-client');
  return {
    ...actual,
    apiClient: { get: vi.fn(), post: vi.fn(), delete: vi.fn() },
  };
});

vi.mock('@/lib/use-current-user', () => ({
  useCurrentUser: vi.fn(),
}));

const mockedGet = vi.mocked(apiClient.get);
const mockedPost = vi.mocked(apiClient.post);
const mockedDelete = vi.mocked(apiClient.delete);
const mockedUseCurrentUser = vi.mocked(useCurrentUser);

const CURRENT_USER_ID = '01HZDEMO00000000000000AL';
const OTHER_USER_ID = '01HZDEMO000000000000000BB';

function makeQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: { queries: { retry: false, staleTime: 0 } },
  });
}

function renderDetail(ticketId = '01HZTCK0000000000000001'): void {
  const client = makeQueryClient();
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[`/admin/tickets/${ticketId}`]}>
        <Routes>
          <Route
            path="/admin/tickets/:id"
            element={<AdminTicketDetailPage />}
          />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

const sampleTicket = {
  id: '01HZTCK0000000000000001',
  tenant_id: 'demo',
  conversation_id: '01HZCV00000000000000001',
  subject: 'Production API returning 503s intermittently',
  category: 'outage',
  priority: 'P0' as const,
  status: 'in_progress' as const,
  assignee_agent_id: CURRENT_USER_ID,
  sla_deadline_at: '2026-10-10T06:59:36.855425Z',
  first_response_at: '2026-10-10T06:49:36.855429Z',
  resolved_at: null,
  closed_at: null,
  created_at: '2026-10-10T06:44:36.855425Z',
  updated_at: '2026-10-10T06:44:36.855425Z',
};

const emptyMessages = { items: [] };
const emptyEvents: never[] = [];

function mockTicketLoad(
  events: unknown[] = emptyEvents,
): void {
  mockedGet.mockImplementation((url) => {
    if (typeof url === 'string') {
      if (url.endsWith('/events')) {
        return Promise.resolve({ data: events });
      }
      if (url.includes('/messages')) {
        return Promise.resolve({ data: emptyMessages });
      }
    }
    return Promise.resolve({ data: sampleTicket });
  });
}

beforeEach(() => {
  mockedGet.mockReset();
  mockedPost.mockReset();
  mockedDelete.mockReset();
  window.localStorage.setItem(JWT_STORAGE_KEY, 'test-token');
  mockedUseCurrentUser.mockReturnValue({
    user: {
      user_id: CURRENT_USER_ID,
      email: 'alice@example.com',
      tenant_id: 'demo',
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

describe('AdminTicketDetailPage notes panel (Tier 2 Task 2.6)', () => {
  it('renders the empty state when the ticket has no notes', async () => {
    mockTicketLoad();
    renderDetail();
    expect(
      await screen.findByTestId('ticket-notes-card'),
    ).toBeInTheDocument();
    expect(
      screen.getByTestId('ticket-notes-empty'),
    ).toHaveTextContent('暂无备注');
  });

  it('shows a delete affordance on notes the caller authored', async () => {
    const events = [
      {
        id: 'note-self-1',
        actor_type: 'agent',
        actor_id: CURRENT_USER_ID,
        event_type: 'note',
        payload: { body: 'my own note' },
        created_at: '2026-10-10T07:00:00Z',
      },
      {
        id: 'note-other-1',
        actor_type: 'agent',
        actor_id: OTHER_USER_ID,
        event_type: 'note',
        payload: { body: "someone else's note" },
        created_at: '2026-10-10T07:01:00Z',
      },
    ];
    mockTicketLoad(events);
    renderDetail();

    expect(
      await screen.findByTestId('ticket-note-row-note-self-1'),
    ).toBeInTheDocument();
    expect(
      screen.getByTestId('ticket-note-body-note-self-1'),
    ).toHaveTextContent("my own note");
    // Author sees delete button.
    expect(
      screen.getByTestId('ticket-note-delete-note-self-1'),
    ).toBeInTheDocument();
    // Other author's note is visible (read-only) but has no delete
    // button — the server would also reject the DELETE; UI hides it
    // so the operator doesn't see a button that errors out.
    expect(
      screen.getByTestId('ticket-note-row-note-other-1'),
    ).toBeInTheDocument();
    expect(
      screen.queryByTestId('ticket-note-delete-note-other-1'),
    ).not.toBeInTheDocument();
    // Counter shows 2 notes.
    expect(screen.getByTestId('ticket-notes-count')).toHaveTextContent('2');
  });

  it('POSTs the note when the user submits the form', async () => {
    mockTicketLoad();
    mockedPost.mockResolvedValue({
      data: {
        id: 'note-new-1',
        actor_type: 'agent',
        actor_id: CURRENT_USER_ID,
        event_type: 'note',
        payload: { body: 'remember to follow up tomorrow' },
        created_at: '2026-10-10T07:30:00Z',
      },
    });
    renderDetail();

    const input = (await screen.findByTestId(
      'ticket-notes-input',
    )) as HTMLTextAreaElement;
    const user = userEvent.setup();
    await user.type(input, 'remember to follow up tomorrow');

    await waitFor(() =>
      expect(
        screen.getByTestId('ticket-notes-submit'),
      ).not.toBeDisabled(),
    );
    await user.click(screen.getByTestId('ticket-notes-submit'));

    await waitFor(() =>
      expect(mockedPost).toHaveBeenCalledWith(
        `/api/v1/tickets/${sampleTicket.id}/notes`,
        { body: 'remember to follow up tomorrow' },
      ),
    );
    // After success, the input clears and the new note shows up.
    expect(input.value).toBe('');
    expect(
      await screen.findByTestId('ticket-note-row-note-new-1'),
    ).toBeInTheDocument();
  });

  it('disables submit when the body is empty', async () => {
    mockTicketLoad();
    renderDetail();
    await screen.findByTestId('ticket-notes-card');
    expect(
      screen.getByTestId('ticket-notes-submit'),
    ).toBeDisabled();
  });

  it('calls DELETE on the note when the author clicks delete', async () => {
    const events = [
      {
        id: 'note-self-1',
        actor_type: 'agent',
        actor_id: CURRENT_USER_ID,
        event_type: 'note',
        payload: { body: 'delete me' },
        created_at: '2026-10-10T07:00:00Z',
      },
    ];
    mockTicketLoad(events);
    mockedDelete.mockResolvedValue({ data: null });
    renderDetail();

    await screen.findByTestId('ticket-note-row-note-self-1');
    const user = userEvent.setup();
    await user.click(
      screen.getByTestId('ticket-note-delete-note-self-1'),
    );

    await waitFor(() =>
      expect(mockedDelete).toHaveBeenCalledWith(
        `/api/v1/tickets/${sampleTicket.id}/notes/note-self-1`,
      ),
    );
    // After the delete, the row is gone (optimistic setQueryData update).
    await waitFor(() =>
      expect(
        screen.queryByTestId('ticket-note-row-note-self-1'),
      ).not.toBeInTheDocument(),
    );
  });

  it('surfaces the API error if the POST fails', async () => {
    mockTicketLoad();
    const err = new Error('Network Error');
    (err as unknown as { isAxiosError?: boolean }).isAxiosError = true;
    Object.defineProperty(err, 'message', { value: 'Network Error' });
    mockedPost.mockRejectedValue(err);
    renderDetail();

    const user = userEvent.setup();
    const input = (await screen.findByTestId(
      'ticket-notes-input',
    )) as HTMLTextAreaElement;
    await user.type(input, 'this should fail');
    await waitFor(() =>
      expect(
        screen.getByTestId('ticket-notes-submit'),
      ).not.toBeDisabled(),
    );
    await user.click(screen.getByTestId('ticket-notes-submit'));
    expect(
      await screen.findByTestId('ticket-notes-error'),
    ).toHaveTextContent(/Network Error|加载/);
  });
});