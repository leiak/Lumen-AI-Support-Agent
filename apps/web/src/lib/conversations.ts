import { z } from 'zod';

import { apiClient } from '@/lib/api-client';

// Backend shape (verified against `apps/api/src/conversation/api.py`):
//
//   class ConversationOut(BaseModel):
//     id: str
//     tenant_id: str
//     channel_id: str
//     customer_external_id: str
//     status: ConversationStatus   # open | pending | closed (lowercase on the wire)
//     assigned_agent_id: str | None
//     ai_handling: bool
//     opened_at: datetime
//     last_activity_at: datetime
//
//   class ConversationListOut(BaseModel):
//     items: list[ConversationOut]
//
// NOTE: The list endpoint intentionally omits a total count (see API layer
// comment). The inbox endpoint (`/conversations/inbox`) only supports a
// `status` filter and does NOT support limit/offset — pagination happens
// client-side below.
export const ConversationStatusSchema = z.enum(['open', 'pending', 'closed']);
export type ConversationStatus = z.infer<typeof ConversationStatusSchema>;

export const ConversationSchema = z.object({
  id: z.string(),
  tenant_id: z.string(),
  channel_id: z.string(),
  customer_external_id: z.string(),
  status: ConversationStatusSchema,
  assigned_agent_id: z.string().nullable(),
  ai_handling: z.boolean(),
  opened_at: z.string(),
  last_activity_at: z.string(),
});
export type Conversation = z.infer<typeof ConversationSchema>;

export const ConversationListSchema = z.object({
  items: z.array(ConversationSchema),
});
export type ConversationList = z.infer<typeof ConversationListSchema>;

/**
 * Public-facing filter shape used by the Inbox page. The status field is
 * deliberately a union with `null` (`"全部"`) rather than `undefined` so the
 * filter object is JSON-serialisable into a TanStack Query key.
 */
export interface ConversationFilters {
  status: ConversationStatus | null;
  search: string;
}

export const DEFAULT_FILTERS: ConversationFilters = {
  status: null,
  search: '',
};

export interface ConversationListResponse {
  items: Conversation[];
  total: number;
}

/**
 * GET /api/v1/conversations/inbox — list conversations assigned to the
 * calling agent (scoped by JWT). The endpoint accepts a `status` query
 * parameter; `search` is applied client-side against `id` and
 * `customer_external_id`.
 *
 * We treat the auth surface as agent-only and let 401s/403s fall through
 * to the React Query error path so the page can render its error state.
 */
export async function fetchConversations(
  filters: ConversationFilters,
): Promise<ConversationListResponse> {
  const params: Record<string, string> = {};
  if (filters.status) {
    params.status = filters.status;
  }
  const { data } = await apiClient.get('/api/v1/conversations/inbox', { params });
  const parsed = ConversationListSchema.parse(data);
  // The backend does not return a total count; we expose `items.length`
  // as the total so pagination math stays consistent in the UI.
  return { items: parsed.items, total: parsed.items.length };
}