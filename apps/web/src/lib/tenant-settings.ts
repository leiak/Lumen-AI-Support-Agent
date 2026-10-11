import { z } from 'zod';

import { apiClient } from '@/lib/api-client';

// Tier 2 Task 2.3 — Tenant self-settings admin page.
//
// Backend (apps/api/src/admin/api.py):
//   GET    /api/v1/admin/tenants/{tid}/settings
//   PATCH  /api/v1/admin/tenants/{tid}/settings
//
// Scope: a tenant-scoped admin can view + edit their own tenant's
// name / plan / status. Listing all tenants would require a super-
// admin role that does NOT exist in the current JWT model — that's
// deliberately out of scope.

export const TenantPlanSchema = z.enum(['free', 'pro', 'enterprise']);
export type TenantPlan = z.infer<typeof TenantPlanSchema>;
export const TENANT_PLANS: TenantPlan[] = ['free', 'pro', 'enterprise'];

export const TenantStatusSchema = z.enum(['active', 'suspended']);
// 'deleted' is reserved for super-admin (not exposed via this endpoint).
export type TenantStatus = z.infer<typeof TenantStatusSchema>;
export const TENANT_STATUSES: TenantStatus[] = ['active', 'suspended'];

export const TenantSettingsSchema = z.object({
  id: z.string(),
  name: z.string(),
  plan: TenantPlanSchema,
  status: TenantStatusSchema,
  created_at: z.string(),
  updated_at: z.string(),
});
export type TenantSettings = z.infer<typeof TenantSettingsSchema>;

export const TenantSettingsUpdateInputSchema = z.object({
  name: z.string().min(1).max(200).optional(),
  plan: TenantPlanSchema.optional(),
  status: TenantStatusSchema.optional(),
});
export type TenantSettingsUpdateInput = z.infer<
  typeof TenantSettingsUpdateInputSchema
>;

/** GET /api/v1/admin/tenants/{tenant_id}/settings */
export async function fetchTenantSettings(
  tenantId: string,
): Promise<TenantSettings> {
  const { data } = await apiClient.get(
    `/api/v1/admin/tenants/${tenantId}/settings`,
  );
  return TenantSettingsSchema.parse(data);
}

/** PATCH /api/v1/admin/tenants/{tenant_id}/settings — all fields optional. */
export async function patchTenantSettings(
  tenantId: string,
  input: TenantSettingsUpdateInput,
): Promise<TenantSettings> {
  const { data } = await apiClient.patch(
    `/api/v1/admin/tenants/${tenantId}/settings`,
    input,
  );
  return TenantSettingsSchema.parse(data);
}