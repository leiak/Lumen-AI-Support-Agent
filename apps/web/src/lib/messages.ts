import { z } from 'zod';

import { apiClient } from '@/lib/api-client';

// Backend shapes (verified against `apps/api/src/conversation/api.py`
// and `apps/api/src/agent/schemas.py`):
//
//   class MessageOut(BaseModel):
//     id: str
//     conversation_id: str
//     role: MessageRole   # "customer" | "agent" | "ai" | "system" | "tool"
//     content_text: str
//     sender_id: str | None
//     created_at: datetime
//
//   class AgentMessageCreate(BaseModel):
//     content_text: str   # 1..4000 chars
//
//   class MessageListOut(BaseModel):
//     items: list[MessageOut]
//
// The role enum is lowercase on the wire to match the backend's
// ``MessageRole(StrEnum)`` definitions. We re-export the union as a
// Zod schema so consumers can validate against the same source of
// truth.
export const MessageRoleSchema = z.enum([
  'customer',
  'agent',
  'ai',
  'system',
  'tool',
]);
export type MessageRole = z.infer<typeof MessageRoleSchema>;

export const MessageSchema = z.object({
  id: z.string(),
  conversation_id: z.string(),
  role: MessageRoleSchema,
  content_text: z.string(),
  sender_id: z.string().nullable(),
  created_at: z.string(),
});
export type Message = z.infer<typeof MessageSchema>;

export const MessageListSchema = z.object({
  items: z.array(MessageSchema),
});
export type MessageList = z.infer<typeof MessageListSchema>;

/**
 * GET /api/v1/conversations/{id}/messages — paginated message list.
 * Backend returns at most `limit` (default 50); we hard-code a
 * reasonable page size for the workspace detail view.
 */
export async function fetchMessages(
  conversationId: string,
  limit = 100,
): Promise<Message[]> {
  const { data } = await apiClient.get(
    `/api/v1/conversations/${conversationId}/messages`,
    { params: { limit } },
  );
  const parsed = MessageListSchema.parse(data);
  return parsed.items;
}

/**
 * POST /api/v1/conversations/{id}/messages — agent reply.
 *
 * Returns the persisted Message row (so the optimistic-update path
 * can reconcile with the canonical id / created_at instead of a
 * locally-generated placeholder).
 */
export async function sendAgentMessage(
  conversationId: string,
  contentText: string,
): Promise<Message> {
  const { data } = await apiClient.post(
    `/api/v1/conversations/${conversationId}/messages`,
    { content_text: contentText },
  );
  return MessageSchema.parse(data);
}

/**
 * Build a stable cache key for a conversation's message list.
 * Used by the WS hook to invalidate after receiving a
 * ``message.complete`` / ``message.created`` event.
 */
export function messagesQueryKey(conversationId: string): readonly unknown[] {
  return ['conversation', conversationId, 'messages'] as const;
}
