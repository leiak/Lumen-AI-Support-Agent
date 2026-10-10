import { config } from '../shared/config.js';

export interface QueueItem {
  conversation_id: string;
  customer_external_id: string;
  last_message_preview?: string;
  opened_at: string;
}

export interface MessageItem {
  id: string;
  role: 'customer' | 'assistant' | 'agent';
  content_text: string;
  created_at: string;
}

export async function fetchQueue(token: string): Promise<QueueItem[]> {
  const resp = await fetch(`${config.apiBaseUrl}/api/v1/agents/queue`, {
    headers: { Authorization: `Bearer ${token}` },
  });
  if (!resp.ok) throw new Error(`queue fetch failed: ${resp.status} ${await resp.text()}`);
  return (await resp.json()) as QueueItem[];
}

export async function claimConversation(token: string, conversationId: string): Promise<void> {
  const resp = await fetch(`${config.apiBaseUrl}/api/v1/agents/claim`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      Authorization: `Bearer ${token}`,
    },
    body: JSON.stringify({ conversation_id: conversationId }),
  });
  if (!resp.ok) throw new Error(`claim failed: ${resp.status} ${await resp.text()}`);
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
  const body = (await resp.json()) as { messages?: MessageItem[] };
  return body.messages ?? [];
}
