import { z } from 'zod';

import { apiClient } from '@/lib/api-client';

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
// List endpoint note (Tier 1 Task 1.1, 2026-10-10): the admin
// `GET /api/v1/admin/tenants/{tenant_id}/tickets` endpoint now
// exists. `fetchTickets` calls it directly — the legacy client-side
// join via the admin conversation list (which usually produced an
// empty list because the conversation_id ULID is NOT the ticket_id
// ULID) has been removed.
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
 * Envelope schema for the admin ticket list endpoint
 * (GET /api/v1/admin/tenants/{tenant_id}/tickets).
 *
 * ``items`` mirrors ``TicketSchema``; ``total`` is the unpaginated
 * row count for the current filter so the SPA can render a paginator
 * without a second round-trip.
 */
export const TicketListSchema = z.object({
  items: z.array(TicketSchema),
  total: z.number().int().nonnegative(),
});

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
 * Calls `GET /api/v1/admin/tenants/{tenant_id}/tickets?status=&priority=&limit=&offset=`
 * and returns the items array from the response envelope. The
 * envelope's ``total`` field is the unpaginated row count for the
 * current filter so the SPA can render a paginator.
 *
 * Implementation note (Tier 1 Task 1.1, 2026-10-10): previously this
 * function attempted `GET /api/v1/admin/tickets` first and fell back
 * to a best-effort client-side join via the admin conversation list
 * (the conversation_id ULID is not the ticket_id ULID, so the join
 * was usually empty). The fallback was removed because the real
 * per-tenant admin list endpoint now exists server-side.
 */
export async function fetchTickets(
  tenantId: string,
  filters: TicketFilters = DEFAULT_TICKET_FILTERS,
  _options: FetchTicketsOptions = {},
): Promise<Ticket[]> {
  if (!tenantId) {
    throw new Error('not authenticated');
  }
  const params: Record<string, string> = { limit: '50', offset: '0' };
  if (filters.status) params.status = filters.status;
  if (filters.priority) params.priority = filters.priority;
  const { data } = await apiClient.get(
    `/api/v1/admin/tenants/${tenantId}/tickets`,
    { params },
  );
  const parsed = TicketListSchema.parse(data);
  return parsed.items;
}

/**
 * Build a stable cache key for the admin tickets list. Encodes the
 * filter shape so cache partitions cleanly per filter combination. The
 * ``'list'`` discriminator prevents the detail page's
 * ``['admin', 'tickets', ticketId]`` key from being invalidated by
 * sibling mutations. ``tenantId`` is included so a stale entry from
 * a previous session (different tenant) is partitioned off.
 */
export function ticketsQueryKey(
  tenantId: string,
  filters: TicketFilters,
): readonly unknown[] {
  return [
    'admin',
    'tickets',
    'list',
    tenantId,
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
