import { z } from 'zod';

import { apiClient } from '@/lib/api-client';

// Tech debt #20 / parallel UI task — SLA Policies admin page.
//
// Backend shape verified against `apps/api/src/admin/api.py`
// (new endpoint `GET /api/v1/admin/tenants/{tenant_id}/sla-policies`)
// and `apps/api/src/ticket/models.py` (SlaPolicy model). Endpoints
// require JWT auth; the axios interceptor in `api-client.ts` attaches
// the bearer token automatically — no manual header passing.
//
// PII discipline: SLA policies contain no customer text — just per-priority
// first-response / resolution minute targets. Free to expose wholesale.

export const SlaPrioritySchema = z.enum(['P0', 'P1', 'P2', 'P3']);
export type SlaPriority = z.infer<typeof SlaPrioritySchema>;

export const SlaPolicySchema = z.object({
  id: z.string(),
  name: z.string(),
  priority: SlaPrioritySchema,
  first_response_minutes: z.number().int().positive(),
  resolution_minutes: z.number().int().positive(),
  business_hours_only: z.boolean(),
  created_at: z.string(),
});
export type SlaPolicy = z.infer<typeof SlaPolicySchema>;

export const SlaPolicyListSchema = z.array(SlaPolicySchema);

// --- Mutation inputs ------------------------------------------------------
//
// Tier 2 Task 2.1: the SLA Policies page is no longer read-only. Three
// mutations: upsert (POST), patch (PATCH), delete (DELETE). The natural
// key is ``(tenant_id, name)`` — re-POSTing with an existing name
// overwrites the row but keeps the same id (ULID).

export const SlaPolicyCreateInputSchema = z.object({
  name: z.string().min(1).max(128),
  priority: SlaPrioritySchema,
  first_response_minutes: z.number().int().positive().max(100_000),
  resolution_minutes: z.number().int().positive().max(1_000_000),
  business_hours_only: z.boolean(),
});
export type SlaPolicyCreateInput = z.infer<typeof SlaPolicyCreateInputSchema>;

export const SlaPolicyUpdateInputSchema = z.object({
  priority: SlaPrioritySchema.optional(),
  first_response_minutes: z.number().int().positive().max(100_000).optional(),
  resolution_minutes: z.number().int().positive().max(1_000_000).optional(),
  business_hours_only: z.boolean().optional(),
});
export type SlaPolicyUpdateInput = z.infer<typeof SlaPolicyUpdateInputSchema>;

/**
 * GET /api/v1/admin/tenants/{tenant_id}/sla-policies — list a tenant's
 * SLA policies, ordered by priority (P0 → P3). Empty array when none
 * configured. Cross-tenant access returns 404 (enforced server-side,
 * anti-enumeration — same pattern as kb-drafts / llm-configs / budget).
 */
export async function fetchSlaPolicies(tenantId: string): Promise<SlaPolicy[]> {
  const { data } = await apiClient.get(
    `/api/v1/admin/tenants/${tenantId}/sla-policies`,
  );
  return SlaPolicyListSchema.parse(data);
}

/**
 * POST /api/v1/admin/tenants/{tenant_id}/sla-policies — upsert.
 *
 * Tier 2 Task 2.1. ``(tenant_id, name)`` is unique; the server
 * upserts on conflict and returns the (possibly new) row.
 */
export async function upsertSlaPolicy(
  tenantId: string,
  input: SlaPolicyCreateInput,
): Promise<SlaPolicy> {
  const { data } = await apiClient.post(
    `/api/v1/admin/tenants/${tenantId}/sla-policies`,
    input,
  );
  return SlaPolicySchema.parse(data);
}

/**
 * PATCH /api/v1/admin/tenants/{tenant_id}/sla-policies/{policy_id}
 *
 * Tier 2 Task 2.1. Update mutable fields. ``name`` cannot be changed
 * here — to rename, delete + recreate.
 */
export async function patchSlaPolicy(
  tenantId: string,
  policyId: string,
  input: SlaPolicyUpdateInput,
): Promise<SlaPolicy> {
  const { data } = await apiClient.patch(
    `/api/v1/admin/tenants/${tenantId}/sla-policies/${policyId}`,
    input,
  );
  return SlaPolicySchema.parse(data);
}

/**
 * DELETE /api/v1/admin/tenants/{tenant_id}/sla-policies/{policy_id}
 *
 * Tier 2 Task 2.1. Idempotent — returns 204 whether or not the row
 * existed.
 */
export async function deleteSlaPolicy(
  tenantId: string,
  policyId: string,
): Promise<void> {
  await apiClient.delete(
    `/api/v1/admin/tenants/${tenantId}/sla-policies/${policyId}`,
  );
}
