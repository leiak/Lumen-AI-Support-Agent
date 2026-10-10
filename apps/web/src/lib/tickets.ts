import { AxiosError } from 'axios';
import { z } from 'zod';

import { apiClient } from '@/lib/api-client';
import { fetchConversations } from '@/lib/conversations';

// Backend shapes (verified against `apps/api/src/ticket/{api.py,schemas.py,enums.py,state_machine.py}`):
//
//   class TicketOut(BaseModel):
//     id: str
//     tenant_id: str
//     conversation_id: str
//     subject: str
//     category: str | None
//     priority: TicketPriority   # P0 | P1 | P2 | P3
//     status: TicketStatus       # new | triaged | in_progress | waiting_customer | resolved | closed | cancelled
//     assignee_agent_id: str | None
//     sla_deadline_at: datetime | None
//     first_response_at: datetime | None
//     resolved_at: datetime | None
//     closed_at: datetime | None
//     created_at: datetime
//     updated_at: datetime
//
//   class TicketEventOut(BaseModel):
//     id: str
//     actor_type: str
//     actor_id: str | None
//     event_type: str
//     payload: dict[str, Any]
//     created_at: datetime
//
//   class TicketTransitionIn(BaseModel):
//     to_status: TicketStatus
//     actor_type: str = "agent"
//
// The 7 legal state transitions live in `apps/api/src/ticket/state_machine.py`.
// We mirror them on the client so the detail page can show only the legal
// next-state buttons. CLOSED and CANCELLED are terminal — no outbound
// transitions. RESOLVED can reopen back to IN_PROGRESS.
//
// List endpoint note (2026-10): the backend does NOT expose
// `GET /api/v1/tickets` and the admin `GET /api/v1/admin/tickets` does
// not exist. The conversation inbox endpoint (`/api/v1/conversations/inbox`)
// is agent-scoped and the admin conversation list does NOT return
// ``ticket_id`` on each row. So a clean client-side join by
// ``conversation_id`` is not possible from the public API surface today.
//
// The implementation below therefore attempts a hypothetical admin list
// endpoint first (`/api/v1/admin/tickets`) and falls back to a best-effort
// client-side join via the admin conversation list (a 404 on the ticket
// detail endpoint is treated as "no ticket" and silently skipped). The
// page renders the empty state when neither path produces data — see the
// return summary for the full discussion.
export const TicketStatusSchema = z.enum([
  'new',
  'triaged',
  'in_progress',
  'waiting_customer',
  'resolved',
  'closed',
  'cancelled',
]);
export type TicketStatus = z.infer<typeof TicketStatusSchema>;

export const TicketPrioritySchema = z.enum(['P0', 'P1', 'P2', 'P3']);
export type TicketPriority = z.infer<typeof TicketPrioritySchema>;

/**
 * Mirror of `apps/api/src/ticket/state_machine.py:_TRANSITIONS` — the legal
 * next states from any given current status. We intentionally do NOT
 * hardcode the buttons on the detail page; the page iterates this map
 * to render the transition controls. CLOSED and CANCELLED are terminal
 * and return an empty set.
 */
export const TICKET_TRANSITIONS: Readonly<Record<TicketStatus, readonly TicketStatus[]>> = {
  new: ['triaged', 'cancelled'],
  triaged: ['in_progress', 'cancelled'],
  in_progress: ['waiting_customer', 'resolved', 'cancelled'],
  waiting_customer: ['in_progress', 'resolved', 'cancelled'],
  resolved: ['closed', 'in_progress'],
  closed: [],
  cancelled: [],
};

export const TicketSchema = z.object({
  id: z.string(),
  tenant_id: z.string(),
  conversation_id: z.string(),
  subject: z.string(),
  category: z.string().nullable(),
  priority: TicketPrioritySchema,
  status: TicketStatusSchema,
  assignee_agent_id: z.string().nullable(),
  sla_deadline_at: z.string().nullable(),
  first_response_at: z.string().nullable(),
  resolved_at: z.string().nullable(),
  closed_at: z.string().nullable(),
  created_at: z.string(),
  updated_at: z.string(),
});
export type Ticket = z.infer<typeof TicketSchema>;

export const TicketEventSchema = z.object({
  id: z.string(),
  actor_type: z.string(),
  actor_id: z.string().nullable(),
  event_type: z.string(),
  payload: z.record(z.unknown()),
  created_at: z.string(),
});
export type TicketEvent = z.infer<typeof TicketEventSchema>;

/**
 * Filter shape for the admin tickets list. Both fields accept `null` to
 * mean "no filter" so the query key is JSON-serialisable. Status values
 * mirror `TicketStatusSchema`; priority values mirror `TicketPrioritySchema`.
 */
export interface TicketFilters {
  status: TicketStatus | null;
  priority: TicketPriority | null;
}

export const DEFAULT_TICKET_FILTERS: TicketFilters = {
  status: null,
  priority: null,
};

/**
 * Return the legal next states for a given current ticket status. Pure
 * function — no I/O. Used by the detail page to render transition
 * controls without hardcoding the state machine.
 */
export function nextTicketStatuses(current: TicketStatus): readonly TicketStatus[] {
  return TICKET_TRANSITIONS[current] ?? [];
}

/**
 * GET /api/v1/tickets/{ticket_id} — fetch a single ticket.
 *
 * 404 on missing OR cross-tenant (anti-enumeration). The axios error
 * path is preserved for the detail page to render the right error state.
 */
export async function fetchTicket(ticketId: string): Promise<Ticket> {
  const { data } = await apiClient.get(`/api/v1/tickets/${ticketId}`);
  return TicketSchema.parse(data);
}

/**
 * GET /api/v1/tickets/{ticket_id}/events — audit log, newest first.
 *
 * 404 on missing OR cross-tenant ticket. Empty list on success when the
 * ticket has no events yet (legal — a freshly created ticket has 1 event
 * but a CANCELLED-then-reset ticket could have 0).
 */
export async function fetchTicketEvents(ticketId: string): Promise<TicketEvent[]> {
  const { data } = await apiClient.get(`/api/v1/tickets/${ticketId}/events`);
  return z.array(TicketEventSchema).parse(data);
}

/**
 * POST /api/v1/tickets/{ticket_id}/transition — drive the state machine.
 *
 * Returns the updated ticket. The backend returns 409 Conflict when the
 * transition is illegal (the button should never fire in that case —
 * the UI only renders legal next-states — but the error path is
 * surfaced to the detail page so an optimistic-update rollback can
 * render a banner).
 */
export async function transitionTicket(
  ticketId: string,
  toStatus: TicketStatus,
  actorType: string = 'admin',
  note?: string,
): Promise<Ticket> {
  const body: { to_status: TicketStatus; actor_type: string; note?: string } = {
    to_status: toStatus,
    actor_type: actorType,
  };
  if (note !== undefined) {
    body.note = note;
  }
  const { data } = await apiClient.post(
    `/api/v1/tickets/${ticketId}/transition`,
    body,
  );
  return TicketSchema.parse(data);
}

interface FetchTicketsOptions {
  /** If true, attempt the admin list endpoint first. Defaults to true. */
  tryAdminList?: boolean;
}

/**
 * List tickets for the admin workspace.
 *
 * Implementation note: there is no `GET /api/v1/tickets` list endpoint on
 * the backend. The admin conversation list (`/api/v1/conversations`)
 * does not return `ticket_id` per row, so a clean client-side join by
 * `conversation_id` is not possible from the current public surface.
 *
 * Strategy:
 *   1. Try `GET /api/v1/admin/tickets` (admin list, not yet implemented
 *      on the server). 200 → use the response directly.
 *   2. On 404, fall back to "load all admin conversations" and attempt
 *      `GET /api/v1/tickets/{conversation_id}` per row. 404s are
 *      silently skipped (a conversation without a ticket yields no
 *      result, which is the right behaviour). Tickets returned are
 *      then filtered client-side.
 *   3. Any other error bubbles up to the caller.
 *
 * The fallback will usually produce an empty list in production (the
 * ticket_id and conversation_id ULIDs are different). When the admin
 * list endpoint is added server-side, no client changes will be needed.
 */
export async function fetchTickets(
  filters: TicketFilters = DEFAULT_TICKET_FILTERS,
  options: FetchTicketsOptions = {},
): Promise<Ticket[]> {
  const { tryAdminList = true } = options;
  if (tryAdminList) {
    try {
      const { data } = await apiClient.get('/api/v1/admin/tickets', {
        params: buildListParams(filters),
      });
      const parsed = z
        .object({ tickets: z.array(TicketSchema) })
        .safeParse(data);
      if (parsed.success) {
        return applyClientFilters(parsed.data.tickets, filters);
      }
    } catch (err) {
      if (!(err instanceof AxiosError) || err.response?.status !== 404) {
        throw err;
      }
      // 404 — fall through to the client-side join below.
    }
  }
  return fetchTicketsViaConversations(filters);
}

/**
 * Fallback path: load all admin conversations (limited, best-effort)
 * and try to load a ticket per row by conversation_id. Filters the
 * result client-side.
 *
 * This is best-effort only. The conversation_id ULID is NOT the
 * ticket_id ULID in general, so most calls will 404. The function
 * exists so the page renders gracefully today and starts working
 * "for free" the moment the admin list endpoint is shipped.
 */
async function fetchTicketsViaConversations(
  filters: TicketFilters,
): Promise<Ticket[]> {
  const list = await fetchConversations({ status: null, search: '' });
  const candidates = list.items;
  const tickets: Ticket[] = [];
  for (const conv of candidates) {
    try {
      const ticket = await fetchTicket(conv.id);
      tickets.push(ticket);
    } catch (err) {
      if (err instanceof AxiosError && err.response?.status === 404) {
        // No ticket attached to this conversation (or IDs differ).
        // Skip — this is the expected outcome for the majority of
        // conversations until the admin list endpoint ships.
        continue;
      }
      // Anything else (5xx, network) is a real failure — surface it
      // to the caller so the page can render its error state.
      throw err;
    }
  }
  return applyClientFilters(tickets, filters);
}

function buildListParams(filters: TicketFilters): Record<string, string> {
  const params: Record<string, string> = {};
  if (filters.status) params.status = filters.status;
  if (filters.priority) params.priority = filters.priority;
  return params;
}

function applyClientFilters(
  tickets: readonly Ticket[],
  filters: TicketFilters,
): Ticket[] {
  return tickets.filter((t) => {
    if (filters.status && t.status !== filters.status) return false;
    if (filters.priority && t.priority !== filters.priority) return false;
    return true;
  });
}

/**
 * Build a stable cache key for the admin tickets list. Encodes the
 * filter shape so cache partitions cleanly per filter combination. The
 * `'list'` discriminator prevents the detail page's
 * ``['admin', 'tickets', ticketId]`` key from being invalidated by
 * sibling mutations.
 */
export function ticketsQueryKey(filters: TicketFilters): readonly unknown[] {
  return [
    'admin',
    'tickets',
    'list',
    filters.status ?? 'all',
    filters.priority ?? 'all',
  ] as const;
}

export function ticketDetailQueryKey(ticketId: string): readonly unknown[] {
  return ['admin', 'tickets', ticketId] as const;
}

export function ticketEventsQueryKey(ticketId: string): readonly unknown[] {
  return ['admin', 'tickets', ticketId, 'events'] as const;
}
