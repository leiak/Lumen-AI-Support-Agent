import { config } from '../shared/config.js';

export interface SuggestResponse {
  suggestion?: string;
  detail?: string;
}

export async function suggestReply(
  token: string,
  conversationId: string,
): Promise<SuggestResponse> {
  const resp = await fetch(`${config.apiBaseUrl}/api/v1/agents/suggest-reply`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      Authorization: `Bearer ${token}`,
    },
    body: JSON.stringify({ conversation_id: conversationId }),
  });
  if (!resp.ok) {
    // Read body defensively — server may return HTML on a 5xx (proxy page)
    // so guard the JSON parse with content-type.
    const ct = resp.headers.get('content-type') ?? '';
    const detail = ct.includes('application/json')
      ? JSON.stringify(await resp.json())
      : await resp.text();
    throw new Error(`suggest failed: ${resp.status} ${detail}`);
  }
  return (await resp.json()) as SuggestResponse;
}
