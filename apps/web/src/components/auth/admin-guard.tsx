import type { ReactNode } from 'react';
import { Navigate } from 'react-router-dom';

import { useIsAdmin } from '@/lib/use-is-admin';
import { FullPageSpinner } from '@/components/ui/full-page-spinner';

export interface AdminGuardProps {
  children: ReactNode;
}

/**
 * Tech debt #20 — gate a route on the JWT role being 'admin' or 'owner'.
 *
 * Mirrors `AuthGuard` but layered on top of the role check:
 * - `isLoading` → render spinner
 * - No user / non-admin role → redirect to /inbox (NOT /login — the
 *   user IS authenticated, just lacks the role; going to /login would
 *   be confusing).
 * - Admin / owner → render children.
 *
 * The matching JWT-attached `apiClient` request interceptor will
 * already redirect to /login on a 401 (token expired). A 403 from
 * `require_admin` surfaces as a generic AxiosError to the mutation
 * handler — we display the error message via `extractErrorMessage`.
 */
export function AdminGuard({ children }: AdminGuardProps): JSX.Element {
  const { isAdmin, isLoading } = useIsAdmin();
  if (isLoading) return <FullPageSpinner />;
  if (!isAdmin) return <Navigate to="/inbox" replace />;
  return <>{children}</>;
}