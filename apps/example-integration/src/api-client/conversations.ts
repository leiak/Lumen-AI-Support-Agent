import { config } from '../shared/config.js';

// Mirrors backend `agent.schemas.ConversationOut` — the agent workspace
// shape returned by GET /api/v1/agents/queue and POST .../claim. Field
// names match Pydantic serialization (snake_case).
export interface QueueItem {
  id: string;
  tenant_id: string;
  channel_id: string;
  customer_external_id: string;
  status: 'open' | 'pending' | 'closed';
  assigned_agent_id: string | null;
  ai_handling: boolean;
  opened_at: string;
  last_activity_at: string;
}

// Mirrors backend `conversation.schemas.MessageOut` — the message shape
// returned by GET /api/v1/conversations/{id}/messages and POST .../messages.
// `role` is a StrEnum on the backend with values
// `customer | agent | ai | system | tool`.
export interface MessageItem {
  id: string;
  conversation_id: string;
  role: 'customer' | 'agent' | 'ai' | 'system' | 'tool';
  content_text: string;
  sender_id: string | null;
  created_at: string;
}

export async function fetchQueue(token: string): Promise<QueueItem[]> {
  const resp = await fetch(`${config.apiBaseUrl}/api/v1/agents/queue`, {
    headers: { Authorization: `Bearer ${token}` },
  });
  if (!resp.ok) throw new Error(`queue fetch failed: ${resp.status} ${await resp.text()}`);
  const body = (await resp.json()) as { items: QueueItem[] };
  return body.items;
}

// Mirrors backend `agent.api.claim_conversation` — POST
// /api/v1/agents/conversations/{conversation_id}/claim with no body.
// Returns the updated ConversationOut so the caller can read the
// freshly-set `assigned_agent_id` without a refetch.
export async function claimConversation(
  token: string,
  conversationId: string,
): Promise<QueueItem> {
  const resp = await fetch(
    `${config.apiBaseUrl}/api/v1/agents/conversations/${conversationId}/claim`,
    {
      method: 'POST',
      headers: { Authorization: `Bearer ${token}` },
    },
  );
  if (!resp.ok) throw new Error(`claim failed: ${resp.status} ${await resp.text()}`);
  return (await resp.json()) as QueueItem;
}

export async function postReply(
  token: string,
  conversationId: string,
  contentText: string,
): Promise<MessageItem> {
  const resp = await fetch(
    `${config.apiBaseUrl}/api/v1/conversations/${conversationId}/messages`,
    {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        Authorization: `Bearer ${token}`,
      },
      body: JSON.stringify({ content_text: contentText }),
    },
  );
  if (!resp.ok) throw new Error(`reply failed: ${resp.status} ${await resp.text()}`);
  return (await resp.json()) as MessageItem;
}

export async function listMessages(
  token: string,
  conversationId: string,
): Promise<MessageItem[]> {
  const resp = await fetch(
    `${config.apiBaseUrl}/api/v1/conversations/${conversationId}/messages`,
    {
      headers: { Authorization: `Bearer ${token}` },
    },
  );
  if (!resp.ok) throw new Error(`messages fetch failed: ${resp.status} ${await resp.text()}`);
  const body = (await resp.json()) as { items: MessageItem[] };
  return body.items;
}
