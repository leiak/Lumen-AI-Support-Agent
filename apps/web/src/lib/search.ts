import { z } from 'zod';

import { apiClient } from '@/lib/api-client';

/**
 * Cross-domain admin search — Tier 3 Task 3.3.
 *
 * Single endpoint:
 *   GET /api/v1/admin/search?q=...&limit=10
 *
 * Returns grouped hits across KB articles, tickets, and conversations.
 * The flat shape (single ``kind`` discriminator + per-kind ``extra``
 * bag) lets us render mixed-domain tiles with one render branch.
 */

export const SEARCH_KINDS = ['article', 'ticket', 'conversation'] as const;
export type SearchKind = (typeof SEARCH_KINDS)[number];

export const SearchResultSchema = z.object({
  kind: z.enum(SEARCH_KINDS),
  id: z.string(),
  title: z.string(),
  subtitle: z.string().nullable().optional(),
  snippet: z.string().nullable().optional(),
  extra: z.record(z.unknown()).default({}),
});
export type SearchResult = z.infer<typeof SearchResultSchema>;

export const SearchResultsOutSchema = z.object({
  query: z.string(),
  total: z.number().int().nonnegative(),
  results: z.array(SearchResultSchema),
});
export type SearchResultsOut = z.infer<typeof SearchResultsOutSchema>;

/**
 * Run a cross-domain search. Throws on auth / network / schema
 * errors; the search page surfaces those via react-query state.
 */
export async function adminSearch(
  query: string,
  limit = 10,
): Promise<SearchResultsOut> {
  const params = new URLSearchParams({ q: query });
  if (limit !== 10) params.set('limit', String(limit));
  const { data } = await apiClient.get(
    `/api/v1/admin/search?${params.toString()}`,
  );
  return SearchResultsOutSchema.parse(data);
}

/**
 * Build the SPA-internal link for a search hit. Conversation hits
 * currently have no admin detail page — they're shown as read-only
 * chips so the operator can grab the ID. Ticket / KB routes match
 * the existing admin nav.
 */
export function hrefForHit(hit: SearchResult): string | null {
  switch (hit.kind) {
    case 'article':
      return `/kb/${hit.id}`;
    case 'ticket':
      return `/admin/tickets/${hit.id}`;
    case 'conversation':
      return `/inbox?conversation=${hit.id}`;
    default:
      return null;
  }
}