import { z } from 'zod';

import { apiClient } from '@/lib/api-client';

/**
 * Runtime LLM provider chain admin — Tier 3 Task 3.4.
 *
 * Single endpoint:
 *   GET  /api/v1/admin/llm-chain — read the chain + provider registry
 *   PUT  /api/v1/admin/llm-chain — replace the chain atomically
 *
 * The SPA renders a reorderable list with per-row "enabled" toggles.
 * Saves hit the full chain (PUT semantics) — simpler than a PATCH
 * loop and matches the "all-or-nothing" admin UX.
 */

export const LLMChainStepSchema = z.object({
  provider: z.string().min(1).max(64),
  model: z.string().min(1).max(128),
  enabled: z.boolean(),
});
export type LLMChainStep = z.infer<typeof LLMChainStepSchema>;

export const LLMProviderInfoSchema = z.object({
  name: z.string(),
  model: z.string(),
  is_default: z.boolean(),
});
export type LLMProviderInfo = z.infer<typeof LLMProviderInfoSchema>;

export const LLMChainReadSchema = z.object({
  steps: z.array(LLMChainStepSchema),
  providers: z.array(LLMProviderInfoSchema),
});
export type LLMChainRead = z.infer<typeof LLMChainReadSchema>;

export const LLMChainUpdateSchema = z.object({
  steps: z.array(LLMChainStepSchema),
});
export type LLMChainUpdate = z.infer<typeof LLMChainUpdateSchema>;

/**
 * Fetch the current chain + provider list. Throws on auth / network
 * / schema errors; the page surfaces those via react-query state.
 */
export async function fetchLlmChain(): Promise<LLMChainRead> {
  const { data } = await apiClient.get('/api/v1/admin/llm-chain');
  return LLMChainReadSchema.parse(data);
}

/**
 * Replace the runtime chain. Returns the new chain + provider list
 * (PUT response mirrors GET so we don't need a second round-trip).
 */
export async function updateLlmChain(
  payload: LLMChainUpdate,
): Promise<LLMChainRead> {
  const { data } = await apiClient.put('/api/v1/admin/llm-chain', payload);
  return LLMChainReadSchema.parse(data);
}

/** Reorder helper — used by the up/down affordances. */
export function moveStep<T>(arr: readonly T[], from: number, to: number): T[] {
  if (from === to) return [...arr];
  const next = [...arr];
  const [item] = next.splice(from, 1);
  if (item === undefined) return next;
  next.splice(to, 0, item);
  return next;
}