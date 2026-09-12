import { z } from 'zod';

import { apiClient } from '@/lib/api-client';

// Backend shape (verified against `apps/api/src/agent/schemas.py`):
//
//   SuggestionTurnKind = Literal["rag_hit", "no_rag", "no_customer_message", "llm_unavailable"]
//
//   class CitationOut(BaseModel):
//     article_id: str
//     chunk_index: int
//     text: str   # truncated to 200 chars by backend
//     score: float
//
//   class SuggestionOut(BaseModel):
//     conversation_id: str
//     suggested_text: str
//     citations: list[CitationOut]
//     retrieval_score_max: float
//     warning: str | None
//     turn_kind: SuggestionTurnKind
//
// The schema is duplicated here (vs. imported from the API) so the
// frontend stays free of any build-time dep on the Python source
// tree — and to keep a single Zod source of truth for the WS test
// suite that runs against mocked responses.
export const SuggestionTurnKindSchema = z.enum([
  'rag_hit',
  'no_rag',
  'no_customer_message',
  'llm_unavailable',
]);
export type SuggestionTurnKind = z.infer<typeof SuggestionTurnKindSchema>;

export const CitationSchema = z.object({
  article_id: z.string(),
  chunk_index: z.number().int().nonnegative(),
  text: z.string(),
  score: z.number(),
});
export type Citation = z.infer<typeof CitationSchema>;

export const SuggestionSchema = z.object({
  conversation_id: z.string(),
  suggested_text: z.string(),
  citations: z.array(CitationSchema),
  retrieval_score_max: z.number(),
  warning: z.string().nullable(),
  turn_kind: SuggestionTurnKindSchema,
});
export type Suggestion = z.infer<typeof SuggestionSchema>;

/**
 * POST /api/v1/agents/conversations/{id}/suggest-reply.
 *
 * Read-only AI suggestion preview — never mutates state. Throws on
 * non-2xx so the React Query error path can surface a retry button
 * in the suggestion pane.
 */
export async function fetchSuggestion(
  conversationId: string,
): Promise<Suggestion> {
  const { data } = await apiClient.post(
    `/api/v1/agents/conversations/${conversationId}/suggest-reply`,
  );
  return SuggestionSchema.parse(data);
}
