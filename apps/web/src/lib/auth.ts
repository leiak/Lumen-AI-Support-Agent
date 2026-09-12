import { z } from 'zod';

import { apiClient } from '@/lib/api-client';

// Backend shape (verified against `apps/api/src/auth/schemas.py`):
//
//   class UserInfo(BaseModel):
//     id: str
//     tenant_id: str
//     email: str
//     full_name: str | None
//     role: str
//
//   class LoginResponse(BaseModel):
//     access_token: str
//     token_type: str = "bearer"
//     expires_in: int
//     user: UserInfo
//
// The M1 login flow is two-step:
//   1. User types email, frontend calls GET /auth/lookup-tenant to resolve
//      the tenant_id (anti-enumeration: server returns nulls for unknown
//      emails).
//   2. User submits email + password; we POST /auth/login with the
//      X-Tenant-Id header attached ONLY for this call (per-call header
//      override — not global).
export const UserInfoSchema = z.object({
  id: z.string(),
  tenant_id: z.string(),
  email: z.string(),
  full_name: z.string().nullable(),
  role: z.string(),
});

export const LoginResponseSchema = z.object({
  access_token: z.string(),
  token_type: z.string(),
  expires_in: z.number().int(),
  user: UserInfoSchema,
});

// GET /auth/lookup-tenant response. ``null`` means "no user / no active
// tenant" — the frontend MUST treat both branches identically (the
// server intentionally does NOT distinguish to prevent email enumeration).
export const TenantHintSchema = z.object({
  tenant_id: z.string().nullable(),
  tenant_name: z.string().nullable(),
});

export type UserInfo = z.infer<typeof UserInfoSchema>;
export type LoginResponse = z.infer<typeof LoginResponseSchema>;
export type TenantHint = z.infer<typeof TenantHintSchema>;

export interface LoginInput {
  email: string;
  password: string;
  tenantId: string;
}

/**
 * GET /api/v1/auth/lookup-tenant — resolve which tenant owns ``email``.
 *
 * Anti-enumeration contract: the server always returns HTTP 200 with
 * ``{tenant_id, tenant_name}`` regardless of whether ``email`` matches a
 * registered user. A null response means "no hint available" and the
 * frontend should disable the password field (or refuse to submit)
 * without ever saying "this email is unknown".
 */
export async function lookupTenant(email: string): Promise<TenantHint> {
  const { data } = await apiClient.get('/api/v1/auth/lookup-tenant', {
    params: { email },
  });
  return TenantHintSchema.parse(data);
}

/**
 * POST /api/v1/auth/login — exchange credentials for a JWT.
 *
 * The X-Tenant-Id header is attached ONLY for this call (per-call
 * override) so other endpoints continue to use the request interceptor's
 * Authorization header without inheriting a tenant scope they shouldn't.
 */
export async function login(input: LoginInput): Promise<LoginResponse> {
  const { data } = await apiClient.post(
    '/api/v1/auth/login',
    { email: input.email, password: input.password },
    { headers: { 'X-Tenant-Id': input.tenantId } },
  );
  return LoginResponseSchema.parse(data);
}