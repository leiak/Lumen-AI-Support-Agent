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
// The backend requires an `X-Tenant-Id` header on POST /auth/login; for the
// M1 single-tenant demo we read it from VITE_DEMO_TENANT_ID (default "demo").
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

export type UserInfo = z.infer<typeof UserInfoSchema>;
export type LoginResponse = z.infer<typeof LoginResponseSchema>;

const DEMO_TENANT_ID: string =
  (import.meta.env.VITE_DEMO_TENANT_ID as string | undefined) ?? 'demo';

export interface LoginInput {
  email: string;
  password: string;
}

/**
 * Call POST /api/v1/auth/login with credentials + the demo tenant header,
 * validate the response shape, and return the parsed `LoginResponse`.
 */
export async function login(input: LoginInput): Promise<LoginResponse> {
  const { data } = await apiClient.post('/api/v1/auth/login', input, {
    headers: { 'X-Tenant-Id': DEMO_TENANT_ID },
  });
  return LoginResponseSchema.parse(data);
}