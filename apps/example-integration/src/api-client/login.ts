import { config } from '../shared/config.js';
import { writeTokenCache } from './token-cache.js';

export interface LoginResponse {
  access_token: string;
  // Add more fields as the backend surfaces them — current seed login
  // response is shaped { access_token, token_type: 'bearer', ... }.
}

export async function login(email: string, password: string): Promise<string> {
  const resp = await fetch(`${config.apiBaseUrl}/api/v1/auth/login`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      // Backend enforces X-Tenant-Id on /auth/login — see auth/api.py.
      // For seed creds (agent@example.com) the tenant is the demo tenant.
      'X-Tenant-Id': config.tenantId,
    },
    body: JSON.stringify({ email, password }),
  });
  if (!resp.ok) {
    const text = await resp.text();
    throw new Error(`login failed: ${resp.status} ${text}`);
  }
  const body = (await resp.json()) as LoginResponse;
  writeTokenCache({
    email,
    accessToken: body.access_token,
    cachedAt: new Date().toISOString(),
  });
  return body.access_token;
}