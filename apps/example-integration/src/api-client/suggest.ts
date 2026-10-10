import { config } from '../shared/config.js';

// Mirrors backend `agent.schemas.SuggestionOut` (suggestion endpoint
// response). `turn_kind` is a `Literal["rag_hit", "no_rag",
// "no_customer_message", "llm_unavailable"]` on the backend; we keep the
// narrower union here so callers see the discriminator.
export interface Citation {
  article_id: string;
  chunk_index: number;
  text: string;
  score: number;
}

export type SuggestTurnKind = 'rag_hit' | 'no_rag' | 'no_customer_message' | 'llm_unavailable';

export interface SuggestResponse {
  conversation_id: string;
  suggested_text: string;
  citations: Citation[];
  retrieval_score_max: number;
  warning: string | null;
  turn_kind: SuggestTurnKind;
}

// Mirrors backend `agent.api.suggest_reply` — POST
// /api/v1/agents/conversations/{conversation_id}/suggest-reply with no
// body. Read-only AI suggestion preview for the workspace.
export async function suggestReply(
  token: string,
  conversationId: string,
): Promise<SuggestResponse> {
  const resp = await fetch(
    `${config.apiBaseUrl}/api/v1/agents/conversations/${conversationId}/suggest-reply`,
    {
      method: 'POST',
      headers: { Authorization: `Bearer ${token}` },
    },
  );
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
