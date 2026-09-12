import { useEffect } from 'react';
import { useNavigate } from 'react-router-dom';
import { useQuery, type UseQueryResult } from '@tanstack/react-query';
import { z } from 'zod';

import { apiClient, JWT_STORAGE_KEY } from '@/lib/api-client';

// Backend shape (verified against `apps/api/src/agent/schemas.py`):
//
//   class AgentMeOut(BaseModel):
//     user_id: str
//     email: str
//     tenant_id: str
//     tenant_name: str
//     role: str
export const AgentMeOutSchema = z.object({
  user_id: z.string(),
  email: z.string(),
  tenant_id: z.string(),
  tenant_name: z.string(),
  role: z.string(),
});

export type AgentMeOut = z.infer<typeof AgentMeOutSchema>;

export const CURRENT_USER_QUERY_KEY = ['agents', 'me'] as const;

async function fetchCurrentUser(): Promise<AgentMeOut> {
  const { data } = await apiClient.get('/api/v1/agents/me');
  return AgentMeOutSchema.parse(data);
}

export interface UseCurrentUserResult {
  user: AgentMeOut | null;
  isLoading: boolean;
  isError: boolean;
}

/**
 * Read the JWT, fetch the caller's profile from GET /api/v1/agents/me.
 *
 * Returns `user: null` when there is no JWT stored, when the request
 * fails with 401 (token expired/invalid — also clears the bad token and
 * redirects to /login), or when the server returns a payload that does
 * not match the expected schema.
 */
export function useCurrentUser(): UseCurrentUserResult {
  const navigate = useNavigate();

  const token = window.localStorage.getItem(JWT_STORAGE_KEY);

  const query: UseQueryResult<AgentMeOut, Error> = useQuery({
    queryKey: [...CURRENT_USER_QUERY_KEY],
    queryFn: fetchCurrentUser,
    enabled: Boolean(token),
    staleTime: 30_000,
    retry: false,
  });

  // On 401 (expired or invalid token), the response interceptor in
  // api-client.ts already clears the JWT and triggers a navigation to
  // /login. We additionally reflect that as `user: null` here so callers
  // don't render a stale identity.
  useEffect(() => {
    if (query.isError) {
      const status = (query.error as { response?: { status?: number } } | null)
        ?.response?.status;
      if (status === 401) {
        window.localStorage.removeItem(JWT_STORAGE_KEY);
        navigate('/login', { replace: true });
      }
    }
  }, [query.isError, query.error, navigate]);

  if (!token) {
    return { user: null, isLoading: false, isError: false };
  }
  if (query.isSuccess) {
    return { user: query.data, isLoading: false, isError: false };
  }
  return {
    user: null,
    isLoading: query.isLoading,
    isError: query.isError,
  };
}