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
