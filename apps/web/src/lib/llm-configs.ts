import { z } from 'zod';

import { apiClient } from '@/lib/api-client';

// ---------------------------------------------------------------------------
// LLM Configs admin SPA — per-tenant BYOK provider list.
//
// Backend shape verified against
//   apps/api/src/admin/schemas/tenant_llm_config.py
// (M4.C Task 4 + Tier 1 Task 1.3) and the route handlers in
//   apps/api/src/admin/api.py
//
// Endpoints:
//   GET    /api/v1/admin/tenants/{tenant_id}/llm-configs
//   POST   /api/v1/admin/tenants/{tenant_id}/llm-configs
//   PATCH  /api/v1/admin/tenants/{tenant_id}/llm-configs/{provider_name}
//   DELETE /api/v1/admin/tenants/{tenant_id}/llm-configs/{provider_name}
//
// The response NEVER includes the plaintext or ciphertext API key —
// see TenantLLMConfigRead docstring for the rationale. The SPA
// therefore has no way to "show me the key"; rotation requires a
// fresh POST with the new plaintext.
// ---------------------------------------------------------------------------

export const LLMProviderSchema = z.enum(['minimax', 'anthropic', 'openai']);
export type LLMProvider = z.infer<typeof LLMProviderSchema>;

export const LLMConfigSchema = z.object({
  provider_name: z.string(),
  base_url: z.string().nullable(),
  enabled: z.boolean(),
  created_at: z.string(),
  updated_at: z.string(),
});
export type LLMConfig = z.infer<typeof LLMConfigSchema>;

export const LLMConfigCreateInputSchema = z.object({
  provider_name: LLMProviderSchema,
  api_key: z.string().min(1).max(512),
  base_url: z.string().max(512).nullable().optional(),
  enabled: z.boolean().optional(),
});
export type LLMConfigCreateInput = z.infer<typeof LLMConfigCreateInputSchema>;

export const LLMConfigUpdateInputSchema = z.object({
  enabled: z.boolean().optional(),
  base_url: z.string().max(512).nullable().optional(),
});
export type LLMConfigUpdateInput = z.infer<typeof LLMConfigUpdateInputSchema>;

/** GET /api/v1/admin/tenants/{tenant_id}/llm-configs */
export async function fetchLLMConfigs(tenantId: string): Promise<LLMConfig[]> {
  const { data } = await apiClient.get(
    `/api/v1/admin/tenants/${tenantId}/llm-configs`,
  );
  return z.array(LLMConfigSchema).parse(data);
}

/**
 * POST /api/v1/admin/tenants/{tenant_id}/llm-configs — upsert.
 *
 * Tier 1 Task 1.3: the SPA can now register a new provider from the
 * UI. Backend encrypts the plaintext API key with Fernet before
 * storage; the response never echoes it.
 */
export async function createLLMConfig(
  tenantId: string,
  input: LLMConfigCreateInput,
): Promise<LLMConfig> {
  const { data } = await apiClient.post(
    `/api/v1/admin/tenants/${tenantId}/llm-configs`,
    input,
  );
  return LLMConfigSchema.parse(data);
}

/**
 * PATCH /api/v1/admin/tenants/{tenant_id}/llm-configs/{provider_name}
 *
 * Tier 1 Task 1.3: toggle ``enabled`` and/or update ``base_url``
 * without rotating the plaintext API key.
 */
export async function updateLLMConfig(
  tenantId: string,
  providerName: string,
  input: LLMConfigUpdateInput,
): Promise<LLMConfig> {
  const { data } = await apiClient.patch(
    `/api/v1/admin/tenants/${tenantId}/llm-configs/${providerName}`,
    input,
  );
  return LLMConfigSchema.parse(data);
}

/**
 * DELETE /api/v1/admin/tenants/{tenant_id}/llm-configs/{provider_name}
 *
 * Tier 1 Task 1.3: remove the BYOK registration for a provider. The
 * operator can re-register later via POST.
 */
export async function deleteLLMConfig(
  tenantId: string,
  providerName: string,
): Promise<void> {
  await apiClient.delete(
    `/api/v1/admin/tenants/${tenantId}/llm-configs/${providerName}`,
  );
}
