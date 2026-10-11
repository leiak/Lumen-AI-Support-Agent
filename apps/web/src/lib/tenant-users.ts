import { z } from 'zod';

import { apiClient } from '@/lib/api-client';

// Tech debt #20 / parallel UI task — Tenant Users (agents / admins) admin page.
//
// Tier 2 Task 2.2. Backend: apps/api/src/admin/api.py — five endpoints:
//   GET    /api/v1/admin/tenants/{tid}/users
//   POST   /api/v1/admin/tenants/{tid}/users
//   PATCH  /api/v1/admin/tenants/{tid}/users/{uid}
//   POST   /api/v1/admin/tenants/{tid}/users/{uid}/reset-password
//   DELETE /api/v1/admin/tenants/{tid}/users/{uid}
//
// PII discipline: email + full_name are PII; we mirror them straight
// from the server. The page MUST never log these fields.

export const UserRoleSchema = z.enum(['admin', 'supervisor', 'agent', 'viewer']);
export type UserRole = z.infer<typeof UserRoleSchema>;

export const USER_ROLES: UserRole[] = ['admin', 'supervisor', 'agent', 'viewer'];

export const TenantUserSchema = z.object({
  id: z.string(),
  tenant_id: z.string(),
  email: z.string(),
  full_name: z.string().nullable(),
  role: UserRoleSchema,
  is_active: z.boolean(),
  created_at: z.string(),
  updated_at: z.string(),
});
export type TenantUser = z.infer<typeof TenantUserSchema>;

export const TenantUserListSchema = z.array(TenantUserSchema);

// --- Mutation inputs ------------------------------------------------------
//
// Password is required on POST (server bcrypt-hashes before insert) and on
// the dedicated reset-password endpoint. Email is the natural key (per-
// tenant uniqueness) and cannot be edited via PATCH.

export const TenantUserCreateInputSchema = z.object({
  email: z.string().email().max(255),
  password: z.string().min(8).max(128),
  full_name: z.string().max(200).optional(),
  role: UserRoleSchema,
});
export type TenantUserCreateInput = z.infer<typeof TenantUserCreateInputSchema>;

export const TenantUserUpdateInputSchema = z.object({
  full_name: z.string().max(200).nullable().optional(),
  role: UserRoleSchema.optional(),
  is_active: z.boolean().optional(),
});
export type TenantUserUpdateInput = z.infer<typeof TenantUserUpdateInputSchema>;

export const TenantUserPasswordResetInputSchema = z.object({
  new_password: z.string().min(8).max(128),
});
export type TenantUserPasswordResetInput = z.infer<
  typeof TenantUserPasswordResetInputSchema
>;

/**
 * GET /api/v1/admin/tenants/{tenant_id}/users — list a tenant's users.
 * Returns [] (not 404) when none configured. Cross-tenant access → 404
 * (anti-enumeration, server-enforced).
 */
export async function fetchTenantUsers(tenantId: string): Promise<TenantUser[]> {
  const { data } = await apiClient.get(
    `/api/v1/admin/tenants/${tenantId}/users`,
  );
  return TenantUserListSchema.parse(data);
}

/**
 * POST /api/v1/admin/tenants/{tenant_id}/users — create. Server bcrypt-
 * hashes the password. Returns 409 on duplicate (tenant_id, email).
 */
export async function createTenantUser(
  tenantId: string,
  input: TenantUserCreateInput,
): Promise<TenantUser> {
  const { data } = await apiClient.post(
    `/api/v1/admin/tenants/${tenantId}/users`,
    input,
  );
  return TenantUserSchema.parse(data);
}

/**
 * PATCH /api/v1/admin/tenants/{tenant_id}/users/{user_id} — update
 * mutable profile fields (full_name / role / is_active).
 */
export async function patchTenantUser(
  tenantId: string,
  userId: string,
  input: TenantUserUpdateInput,
): Promise<TenantUser> {
  const { data } = await apiClient.patch(
    `/api/v1/admin/tenants/${tenantId}/users/${userId}`,
    input,
  );
  return TenantUserSchema.parse(data);
}

/**
 * POST /api/v1/admin/tenants/{tenant_id}/users/{user_id}/reset-password
 * — admin-triggered password reset. 204 No Content.
 */
export async function resetTenantUserPassword(
  tenantId: string,
  userId: string,
  input: TenantUserPasswordResetInput,
): Promise<void> {
  await apiClient.post(
    `/api/v1/admin/tenants/${tenantId}/users/${userId}/reset-password`,
    input,
  );
}

/**
 * DELETE /api/v1/admin/tenants/{tenant_id}/users/{user_id} — idempotent.
 * 400 on self-delete (the UI surfaces this as "can't delete yourself").
 */
export async function deleteTenantUser(
  tenantId: string,
  userId: string,
): Promise<void> {
  await apiClient.delete(
    `/api/v1/admin/tenants/${tenantId}/users/${userId}`,
  );
}