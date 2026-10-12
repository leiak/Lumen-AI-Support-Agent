import { z } from 'zod';

import { apiClient } from '@/lib/api-client';

// Per-tenant auto-assignment policy (M5.3 follow-up).
//
// Backend (apps/api/src/admin/api.py):
//   GET    /api/v1/admin/tenants/{tid}/assignment-policy
//   PATCH  /api/v1/admin/tenants/{tid}/assignment-policy
//
// Storage: tenants.settings['assignment_policy'] (JSONB). The
// strategy enum maps 1:1 to the backend's Literal — keep the two
// in sync. Adding a new strategy is a backend enum + a new
// AssignmentPolicyService method; the frontend just needs the
// new value here for the SPA to render the option.

export const AssignmentStrategySchema = z.enum(['manual', 'least_busy']);
export type AssignmentStrategy = z.infer<typeof AssignmentStrategySchema>;

// Display labels for the strategy select. Stable order so the
// dropdown doesn't reshuffle between renders.
export const ASSIGNMENT_STRATEGIES: ReadonlyArray<{
  value: AssignmentStrategy;
  label: string;
  hint: string;
}> = [
  {
    value: 'manual',
    label: '手动分配',
    hint: 'escalate 后保留 PENDING+NULL,坐席手动认领',
  },
  {
    value: 'least_busy',
    label: '最少负载',
    hint: 'escalate 时自动分配给当前 open+pending 最少的活跃坐席',
  },
];

export const AssignmentPolicySchema = z.object({
  enabled: z.boolean(),
  strategy: AssignmentStrategySchema,
});
export type AssignmentPolicy = z.infer<typeof AssignmentPolicySchema>;

// PATCH input — both fields optional. The backend does
// read-merge-write so omitted fields preserve their current value.
export const AssignmentPolicyUpdateInputSchema = z
  .object({
    enabled: z.boolean().optional(),
    strategy: AssignmentStrategySchema.optional(),
  })
  .strict();
export type AssignmentPolicyUpdateInput = z.infer<
  typeof AssignmentPolicyUpdateInputSchema
>;

/** GET /api/v1/admin/tenants/{tenant_id}/assignment-policy */
export async function fetchAssignmentPolicy(
  tenantId: string,
): Promise<AssignmentPolicy> {
  const { data } = await apiClient.get(
    `/api/v1/admin/tenants/${tenantId}/assignment-policy`,
  );
  return AssignmentPolicySchema.parse(data);
}

/** PATCH /api/v1/admin/tenants/{tenant_id}/assignment-policy — partial update. */
export async function patchAssignmentPolicy(
  tenantId: string,
  input: AssignmentPolicyUpdateInput,
): Promise<AssignmentPolicy> {
  const { data } = await apiClient.patch(
    `/api/v1/admin/tenants/${tenantId}/assignment-policy`,
    input,
  );
  return AssignmentPolicySchema.parse(data);
}
