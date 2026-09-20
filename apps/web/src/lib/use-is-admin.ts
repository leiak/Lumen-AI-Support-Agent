import { useCurrentUser } from '@/lib/use-current-user';

export interface UseIsAdminResult {
  isAdmin: boolean;
  isLoading: boolean;
}

/**
 * Derived hook: returns whether the current user has admin/owner role.
 *
 * The JWT carries `role` on `AgentMeOut` (see `use-current-user.ts`).
 * Admins and owners can review KB drafts; agents cannot. While the
 * user query is loading, `isLoading` is true and `isAdmin` is false
 * so the AdminGuard can render a spinner instead of flashing a
 * redirect.
 */
export function useIsAdmin(): UseIsAdminResult {
  const { user, isLoading } = useCurrentUser();
  const isAdmin = user?.role === 'admin' || user?.role === 'owner';
  return { isAdmin, isLoading };
}